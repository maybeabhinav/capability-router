"""Router action execution and result limits."""

from __future__ import annotations

import os
from pathlib import Path
import threading
import time
from typing import Any
import uuid

from .catalog import search, tool_supported
from .config import AccessLevel, Config, RouterMode
from .downstream import HttpClient, StdioClient, downstream_client
from .errors import ErrorCode, InputError, RouterError
from .protocol import (
    ACTION_FIELDS,
    CapabilityAvailability,
    CapabilityKind,
    MAX_SEARCH_QUERY_LENGTH,
    RouterAction,
    TOOL_ARGUMENT_FIELDS,
)
from .schema import validate
from .util import atomic_write, canonical_bytes, safe_component, sha256_bytes, utc_now


def _find(catalog: dict[str, Any], capability_id: str) -> dict[str, Any]:
    for item in catalog["capabilities"]:
        if item.get("id") == capability_id:
            return item
    raise RouterError(
        ErrorCode.NOT_FOUND,
        "capability was not found",
        {"capability_id": capability_id},
    )


def _validate_action_shape(arguments: dict[str, Any], action: RouterAction) -> None:
    fields = set(arguments)
    required, optional = ACTION_FIELDS[action]
    if not required.issubset(fields) or not fields.issubset(required | optional):
        raise InputError(f"fields are invalid for {action.value}")


def _validate_query(arguments: dict[str, Any]) -> None:
    if "query" not in arguments:
        return
    query = arguments["query"]
    if not isinstance(query, str) or not query:
        raise InputError("query must be a non-empty string")
    if len(query) > MAX_SEARCH_QUERY_LENGTH:
        raise InputError(f"query must not exceed {MAX_SEARCH_QUERY_LENGTH} characters")


def _validate_capability_id(arguments: dict[str, Any]) -> None:
    if "capability_id" in arguments and not (
        isinstance(arguments["capability_id"], str) and arguments["capability_id"]
    ):
        raise InputError("capability_id must be a non-empty string")


def _validate_call_arguments(arguments: dict[str, Any]) -> None:
    if "arguments" in arguments and not isinstance(arguments["arguments"], dict):
        raise InputError("arguments must be an object")


def _validate_kinds(arguments: dict[str, Any]) -> None:
    if "kinds" in arguments:
        kinds = arguments["kinds"]
        valid = (
            isinstance(kinds, list)
            and bool(kinds)
            and all(isinstance(kind, str) for kind in kinds)
            and len(set(kinds)) == len(kinds)
            and all(kind in CapabilityKind.values() for kind in kinds)
        )
        if not valid:
            raise InputError("kinds must contain unique known values")


def _validate_context_fields(arguments: dict[str, Any]) -> None:
    for field in ("context", "source_context", "target_context", "change_id"):
        if field in arguments and not (
            isinstance(arguments[field], str) and arguments[field]
        ):
            raise InputError(f"{field} must be a non-empty string")
    if "expected_revision" in arguments:
        revision = arguments["expected_revision"]
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 0
        ):
            raise InputError("expected_revision must be a non-negative integer")


def _validate_limit(arguments: dict[str, Any]) -> None:
    if "limit" in arguments:
        limit = arguments["limit"]
        integral = isinstance(limit, int) or (
            isinstance(limit, float) and limit.is_integer()
        )
        if (
            isinstance(limit, bool)
            or not integral
            or not 1 <= limit <= 5
        ):
            raise InputError("limit must be an integer from 1 through 5")


ACTION_VALIDATORS = (
    _validate_query,
    _validate_capability_id,
    _validate_call_arguments,
    _validate_kinds,
    _validate_context_fields,
    _validate_limit,
)


def validate_action(arguments: Any) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise InputError("tool arguments must be an object")
    if set(arguments) - TOOL_ARGUMENT_FIELDS:
        raise InputError("unknown action field")
    try:
        action = RouterAction(arguments.get("action"))
    except (TypeError, ValueError):
        raise InputError("action is invalid")
    _validate_action_shape(arguments, action)
    for validator in ACTION_VALIDATORS:
        validator(arguments)
    normalized = dict(arguments)
    normalized["action"] = action
    if "limit" in normalized:
        normalized["limit"] = int(normalized["limit"])
    if "kinds" in normalized:
        normalized["kinds"] = [CapabilityKind(kind) for kind in normalized["kinds"]]
    return normalized


class Executor:
    def __init__(
        self,
        config: Config,
        catalog: dict[str, Any],
        *,
        run_id: str,
        audit_path: Path | None,
    ) -> None:
        self.config = config
        self.catalog = catalog
        self.run_id = run_id
        self.audit_path = audit_path
        self.active_clients: set[StdioClient | HttpClient] = set()
        self.active_clients_lock = threading.Lock()
        self.shutting_down = threading.Event()

    def close_active_clients(self) -> None:
        self.shutting_down.set()
        with self.active_clients_lock:
            clients = tuple(self.active_clients)
        for client in clients:
            client.close()

    def known_capability_id(self, value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        known = any(item.get("id") == value for item in self.catalog["capabilities"])
        return value if known else None

    def _register_client(self, client: StdioClient | HttpClient) -> None:
        with self.active_clients_lock:
            if self.shutting_down.is_set():
                rejected = True
            else:
                self.active_clients.add(client)
                rejected = False
        if rejected:
            client.close()
            raise RouterError(ErrorCode.SHUTDOWN, "router is shutting down")

    def audit(
        self,
        action: str,
        outcome: str,
        capability_id: str | None = None,
        code: str | None = None,
        output_bytes: int | None = None,
        latency_seconds: float | None = None,
        search_candidates: list[str] | None = None,
    ) -> None:
        if self.audit_path is None:
            return
        parent_existed = self.audit_path.parent.exists()
        self.audit_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not parent_existed:
            self.audit_path.parent.chmod(0o700)
        record: dict[str, Any] = {
            "timestamp": utc_now(),
            "run_id": self.run_id,
            "action": action,
            "outcome": outcome,
        }
        if capability_id:
            record["capability_id"] = capability_id
        if code:
            record["code"] = code
        if output_bytes is not None:
            record["output_bytes"] = output_bytes
        if latency_seconds is not None:
            record["latency_seconds"] = round(latency_seconds, 6)
        if search_candidates is not None:
            record["search_candidates"] = search_candidates
        descriptor = os.open(self.audit_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        descriptor_owned = True
        try:
            os.fchmod(descriptor, 0o600)
            stream = os.fdopen(descriptor, "ab")
            descriptor_owned = False
            with stream:
                stream.write(canonical_bytes(record) + b"\n")
        finally:
            if descriptor_owned:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def execute(self, raw: Any) -> dict[str, Any]:
        arguments = validate_action(raw)
        action = arguments["action"]
        handlers = {
            RouterAction.SEARCH: self._search,
            RouterAction.DESCRIBE: self._describe,
            RouterAction.LOAD_SKILL: self._load_skill,
            RouterAction.CALL: self._call,
            RouterAction.STATUS: lambda _: self._status(),
            RouterAction.CONTEXT_LIST: self._context_unavailable,
            RouterAction.CONTEXT_CURRENT: self._context_unavailable,
            RouterAction.CONTEXT_USE: self._context_unavailable,
            RouterAction.CONTEXT_EXPLAIN: self._context_unavailable,
            RouterAction.CONTEXT_MOVE: self._context_unavailable,
            RouterAction.CONTEXT_UNDO: self._context_unavailable,
            RouterAction.CONTEXT_SHARE: self._context_unavailable,
            RouterAction.CONTEXT_UNASSIGN: self._context_unavailable,
        }
        return handlers[action](arguments)

    def _search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        matches = search(
            self.catalog,
            arguments["query"],
            arguments.get("kinds"),
            arguments.get("limit", 5),
        )
        return {"matches": matches} if matches else {"matches": [], "status": "no_match"}

    def _describe(self, arguments: dict[str, Any]) -> dict[str, Any]:
        item = _find(self.catalog, arguments["capability_id"])
        if item["kind"] == CapabilityKind.MCP_TOOL:
            keys = (
                "id",
                "kind",
                "name",
                "description",
                "source",
                "access",
                "availability",
                "input_schema",
                "schema_sha256",
                "stale",
            )
        else:
            keys = (
                "id",
                "kind",
                "name",
                "description",
                "source",
                "access",
                "availability",
                "source_sha256",
                "source_size",
                "stale",
            )
        return {"capability": {key: item.get(key) for key in keys}}

    def _load_skill(self, arguments: dict[str, Any]) -> dict[str, Any]:
        item = _find(self.catalog, arguments["capability_id"])
        if item.get("kind") != CapabilityKind.SKILL:
            raise InputError("load_skill requires a skill capability")
        path = Path(item["skill_path"])
        root = Path(item["skill_root"]).resolve()
        try:
            resolved = path.resolve(strict=True)
            if path.is_symlink() or not resolved.is_relative_to(root) or not resolved.is_file():
                raise OSError("unsafe skill path")
            payload = resolved.read_bytes()
        except OSError as exc:
            raise RouterError(
                ErrorCode.REFRESH_REQUIRED, "skill source changed after refresh"
            ) from exc
        if len(payload) != item["source_size"] or sha256_bytes(payload) != item["source_sha256"]:
            raise RouterError(
                ErrorCode.REFRESH_REQUIRED, "skill source changed after refresh"
            )
        try:
            body = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RouterError(
                ErrorCode.REFRESH_REQUIRED, "skill source is no longer UTF-8"
            ) from exc
        return {"capability_id": item["id"], "untrusted": True, "body": body}

    def _call(self, arguments: dict[str, Any]) -> dict[str, Any]:
        item = _find(self.catalog, arguments["capability_id"])
        if item.get("kind") != CapabilityKind.MCP_TOOL:
            raise InputError("call requires an MCP tool capability")
        if item.get("availability") != CapabilityAvailability.AVAILABLE:
            raise RouterError(ErrorCode.UNAVAILABLE, "capability is unavailable")
        server_name = item["adapter"]["server"]
        definition = self.config.servers.get(server_name)
        if definition is None:
            raise RouterError(
                ErrorCode.REFRESH_REQUIRED,
                "capability server changed after refresh",
            )
        current_access = definition["access_overrides"].get(
            item["name"], definition["default_access"]
        )
        if (
            self.config.mode is RouterMode.READ_ONLY
            and current_access is not AccessLevel.READ
        ):
            raise RouterError(
                ErrorCode.POLICY_DENIED,
                "read-only mode denied this capability",
                {"access": current_access},
            )
        if current_access != item.get("access"):
            raise RouterError(
                ErrorCode.REFRESH_REQUIRED,
                "capability access changed after refresh",
            )
        deadline = time.monotonic() + self.config.call_timeout_seconds
        validation_errors = validate(
            item["input_schema"], arguments["arguments"], _deadline=deadline
        )
        if validation_errors:
            raise RouterError(
                ErrorCode.SCHEMA_VALIDATION_FAILED,
                "arguments do not match the capability schema",
                {"errors": validation_errors[:8]},
            )
        client = downstream_client(self.config, server_name)
        try:
            client.__enter__()
            self._register_client(client)
            client.initialize(deadline=deadline)
            tools = client.list_tools(deadline=deadline, wanted=item["name"])
            runtime = next((tool for tool in tools if tool.get("name") == item["name"]), None)
            if runtime is None:
                raise RouterError(
                    ErrorCode.REFRESH_REQUIRED,
                    "capability is missing from the downstream server",
                )
            if not tool_supported(runtime):
                raise RouterError(
                    ErrorCode.REFRESH_REQUIRED,
                    "capability support changed after refresh",
                )
            runtime_schema = runtime.get("inputSchema")
            if sha256_bytes(canonical_bytes(runtime_schema)) != item["schema_sha256"]:
                raise RouterError(
                    ErrorCode.REFRESH_REQUIRED,
                    "capability schema changed after refresh",
                )
            downstream = client.call_tool(
                item["name"], arguments["arguments"], deadline=deadline
            )
        except RouterError:
            raise
        except Exception as exc:
            raise RouterError(
                ErrorCode.DOWNSTREAM_PROTOCOL_ERROR, "downstream execution failed"
            ) from exc
        finally:
            client.close()
            with self.active_clients_lock:
                self.active_clients.discard(client)
        return {"capability_id": item["id"], "downstream": downstream}

    def _context_unavailable(self, arguments: dict[str, Any]) -> dict[str, Any]:
        raise InputError("context actions require registry session mode")


    def _status(self) -> dict[str, Any]:
        return {
            "mode": self.config.mode,
            "capability_count": len(self.catalog["capabilities"]),
            "catalog_path": str(self.config.catalog_path),
            "generated_at": self.catalog.get("generated_at"),
        }

    def store_artifact(self, document: dict[str, Any]) -> dict[str, Any]:
        payload = canonical_bytes(document)
        artifact_dir_existed = self.config.artifact_dir.exists()
        self.config.artifact_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not artifact_dir_existed:
            self.config.artifact_dir.chmod(0o700)
        name = f"{safe_component(self.run_id, prefix='run')}-{uuid.uuid4().hex}.json"
        path = self.config.artifact_dir / name
        atomic_write(path, payload, mode=0o600)
        try:
            relative = path.relative_to(self.config.root)
        except ValueError:
            relative = path.resolve()
        preview_bytes = payload[: min(512, max(0, self.config.output_limit_bytes // 4))]
        preview = preview_bytes.decode("utf-8", errors="ignore")
        return {
            "truncated": True,
            "artifact_path": str(relative),
            "byte_count": len(payload),
            "sha256": sha256_bytes(payload),
            "media_type": "application/json; charset=utf-8",
            "preview": preview,
        }
