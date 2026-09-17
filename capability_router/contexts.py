"""Context registry and per-session context selection."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
import fcntl
import os
from pathlib import Path
import re
import secrets
from typing import Any, Callable

from .catalog import load_catalog
from .config import load_config
from .environment import load_private_environment
from .errors import ExecutionError, InputError, RouterError
from .util import atomic_write, canonical_bytes, strict_json_loads, utc_now


REGISTRY_VERSION = 1
MAX_HISTORY = 128
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}")


class ContextCommand(str, Enum):
    CREATE = "create"
    LIST = "list"
    USE = "use"
    CURRENT = "current"
    MOVE = "move"
    EXPLAIN = "explain"
    UNDO = "undo"
    SHARE = "share"
    UNASSIGN = "unassign"
    HISTORY = "history"
    DOCTOR = "doctor"
    EXEC = "exec"


class ChangeOperation(str, Enum):
    CREATE = "create"
    MOVE = "move"
    UNDO = "undo"
    SHARE = "share"
    UNASSIGN = "unassign"


class AvailabilityReason(str, Enum):
    SOURCE_INCLUDED = "source_included"
    CAPABILITY_INCLUDED = "capability_included"
    CAPABILITY_EXCLUDED = "capability_excluded"
    NO_INCLUDE_MATCH = "no_include_match"
    DEFAULT_INCLUDE = "default_include"


@dataclass(frozen=True)
class ContextView:
    identifier: str
    config_path: Path
    include_sources: frozenset[str]
    include_capabilities: frozenset[str]
    exclude_capabilities: frozenset[str]
    environment_file: Path | None

    @classmethod
    def from_document(cls, identifier: str, document: dict[str, Any]) -> "ContextView":
        return cls(
            identifier=identifier,
            config_path=Path(document["config"]).expanduser().absolute(),
            include_sources=frozenset(document["include_sources"]),
            include_capabilities=frozenset(document["include_capabilities"]),
            exclude_capabilities=frozenset(document["exclude_capabilities"]),
            environment_file=(
                Path(document["environment_file"]).expanduser().absolute()
                if document["environment_file"] is not None
                else None
            ),
        )


def _identifier(value: str, field: str) -> str:
    if IDENTIFIER.fullmatch(value) is None or ".." in value:
        raise InputError(f"{field} is invalid")
    return value


def _empty_registry() -> dict[str, Any]:
    return {
        "version": REGISTRY_VERSION,
        "revision": 0,
        "contexts": {},
        "history": [],
    }


def _string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise InputError(f"{field} must be an array of strings")
    if len(value) != len(set(value)):
        raise InputError(f"{field} must not contain duplicates")
    return value


def _validate_context(identifier: str, value: Any) -> dict[str, Any]:
    _identifier(identifier, "context identifier")
    required = {
        "config",
        "include_sources",
        "include_capabilities",
        "exclude_capabilities",
    }
    allowed = required | {"environment_file"}
    if (
        not isinstance(value, dict)
        or not required <= set(value)
        or not set(value) <= allowed
    ):
        raise InputError(f"context {identifier} has an invalid shape")
    config = value["config"]
    environment_file = value.get("environment_file")
    if not isinstance(config, str) or not config or "\0" in config:
        raise InputError(f"context {identifier} config is invalid")
    if environment_file is not None and (
        not isinstance(environment_file, str)
        or not environment_file
        or "\0" in environment_file
    ):
        raise InputError(f"context {identifier} environment_file is invalid")
    return {
        "config": config,
        "environment_file": environment_file,
        "include_sources": _string_list(
            value["include_sources"], f"context {identifier} include_sources"
        ),
        "include_capabilities": _string_list(
            value["include_capabilities"],
            f"context {identifier} include_capabilities",
        ),
        "exclude_capabilities": _string_list(
            value["exclude_capabilities"],
            f"context {identifier} exclude_capabilities",
        ),
    }


def _validate_contexts(value: Any, field: str) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        raise InputError(f"{field} must be an object")
    return {
        identifier: _validate_context(identifier, document)
        for identifier, document in value.items()
    }


def _validate_history(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_HISTORY:
        raise InputError("registry history must be a bounded array")
    normalized: list[dict[str, Any]] = []
    change_ids: set[str] = set()
    for item in value:
        valid = (
            isinstance(item, dict)
            and set(item)
            == {
                "change_id",
                "operation",
                "created_at",
                "before",
                "after",
                "base_revision",
                "revision",
            }
            and isinstance(item["change_id"], str)
            and bool(item["change_id"])
            and item["change_id"] not in change_ids
            and item["operation"] in {operation.value for operation in ChangeOperation}
            and isinstance(item["created_at"], str)
            and not isinstance(item["base_revision"], bool)
            and isinstance(item["base_revision"], int)
            and item["base_revision"] >= 0
            and not isinstance(item["revision"], bool)
            and isinstance(item["revision"], int)
            and item["revision"] > item["base_revision"]
        )
        if not valid:
            raise InputError("registry history contains an invalid change")
        change_ids.add(item["change_id"])
        normalized.append(
            {
                **item,
                "before": _validate_contexts(
                    item["before"],
                    "registry history before",
                ),
                "after": _validate_contexts(
                    item["after"],
                    "registry history after",
                ),
            }
        )
    return normalized


def _validate_registry(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "version",
        "revision",
        "contexts",
        "history",
    }:
        raise InputError("context registry has an invalid shape")
    if value["version"] != REGISTRY_VERSION:
        raise InputError("context registry version is unsupported")
    revision = value["revision"]
    contexts = value["contexts"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise InputError("context registry revision is invalid")
    normalized_contexts = _validate_contexts(
        contexts,
        "context registry contexts",
    )
    return {
        "version": REGISTRY_VERSION,
        "revision": revision,
        "contexts": normalized_contexts,
        "history": _validate_history(value["history"]),
    }


class RegistryStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().absolute()
        self.lock_path = self.path.with_name(f"{self.path.name}.lock")

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return _empty_registry()
        try:
            document = strict_json_loads(self.path.read_bytes())
        except (OSError, UnicodeError, ValueError) as exc:
            raise InputError("context registry cannot be read") from exc
        return _validate_registry(document)

    def read(self) -> dict[str, Any]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            with self.lock_path.open("a+b") as lock:
                os.fchmod(lock.fileno(), 0o600)
                fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
                return self._load()
        except OSError as exc:
            raise ExecutionError("context registry lock failed") from exc

    def mutate(
        self,
        operation: ChangeOperation,
        change: Callable[[dict[str, Any]], None],
        *,
        expected_revision: int | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            with self.lock_path.open("a+b") as lock:
                os.fchmod(lock.fileno(), 0o600)
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                registry = self._load()
                base_revision = registry["revision"]
                if (
                    expected_revision is not None
                    and expected_revision != base_revision
                ):
                    raise InputError(
                        "context registry revision is stale",
                        {
                            "expected_revision": expected_revision,
                            "current_revision": base_revision,
                        },
                    )
                before = deepcopy(registry["contexts"])
                change(registry)
                after = deepcopy(registry["contexts"])
                revision = base_revision + 1
                record = {
                    "change_id": secrets.token_hex(12),
                    "operation": operation.value,
                    "created_at": utc_now(),
                    "before": before,
                    "after": after,
                    "base_revision": base_revision,
                    "revision": revision,
                }
                registry["revision"] = revision
                registry["history"] = [*registry["history"], record][-MAX_HISTORY:]
                atomic_write(self.path, canonical_bytes(registry), mode=0o600)
                return registry, record
        except OSError as exc:
            raise ExecutionError("context registry write failed") from exc


def _view(registry: dict[str, Any], identifier: str) -> ContextView:
    document = registry["contexts"].get(identifier)
    if document is None:
        raise InputError(f"context {identifier} does not exist")
    return ContextView.from_document(identifier, document)


def _catalog(view: ContextView) -> list[dict[str, Any]]:
    config = load_config(view.config_path)
    catalog, _ = load_catalog(config)
    return catalog["capabilities"]


def _capability(view: ContextView, capability_id: str) -> dict[str, Any]:
    item = next(
        (entry for entry in _catalog(view) if entry["id"] == capability_id),
        None,
    )
    if item is None:
        raise InputError(
            f"capability {capability_id} is absent from context {view.identifier} catalog"
        )
    return item


def explain_availability(
    view: ContextView, capability: dict[str, Any]
) -> tuple[bool, AvailabilityReason]:
    capability_id = capability["id"]
    if capability_id in view.exclude_capabilities:
        return False, AvailabilityReason.CAPABILITY_EXCLUDED
    if capability_id in view.include_capabilities:
        return True, AvailabilityReason.CAPABILITY_INCLUDED
    if capability.get("source") in view.include_sources:
        return True, AvailabilityReason.SOURCE_INCLUDED
    if view.include_sources or view.include_capabilities:
        return False, AvailabilityReason.NO_INCLUDE_MATCH
    return True, AvailabilityReason.DEFAULT_INCLUDE


def create_context(
    store: RegistryStore,
    identifier: str,
    config_path: str,
    sources: list[str],
    environment_file: str | None = None,
) -> dict[str, Any]:
    identifier = _identifier(identifier, "context identifier")
    config = load_config(config_path)
    catalog, _ = load_catalog(config)
    resolved_environment_file: str | None = None
    if environment_file is not None:
        path = Path(environment_file).expanduser().resolve()
        try:
            load_private_environment(path)
        except ValueError as error:
            raise InputError(str(error)) from error
        resolved_environment_file = str(path)
    known_sources = {
        item.get("source")
        for item in catalog["capabilities"]
        if isinstance(item.get("source"), str)
    }
    unknown = sorted(set(sources) - known_sources)
    if unknown:
        raise InputError("context sources are absent from the catalog", {"sources": unknown})

    def add(registry: dict[str, Any]) -> None:
        if identifier in registry["contexts"]:
            raise InputError(f"context {identifier} already exists")
        registry["contexts"][identifier] = {
            "config": str(config.path),
            "include_sources": sorted(set(sources)),
            "include_capabilities": [],
            "exclude_capabilities": [],
            "environment_file": resolved_environment_file,
        }

    registry, record = store.mutate(ChangeOperation.CREATE, add)
    return {
        "status": "created",
        "context": identifier,
        "revision": registry["revision"],
        "change_id": record["change_id"],
    }


def list_contexts(store: RegistryStore) -> dict[str, Any]:
    registry = store.read()
    contexts = [
        {
            "id": identifier,
            "config": document["config"],
            "include_sources": document["include_sources"],
            "include_capabilities": document["include_capabilities"],
            "exclude_capabilities": document["exclude_capabilities"],
            "environment_file": document["environment_file"],
        }
        for identifier, document in sorted(registry["contexts"].items())
    ]
    return {"revision": registry["revision"], "contexts": contexts}


def _session_path(store: RegistryStore, session: str) -> Path:
    session = _identifier(session, "session identifier")
    return store.path.parent / "sessions" / f"{session}.json"


def context_environment(
    store: RegistryStore,
    identifier: str,
) -> dict[str, str]:
    registry = store.read()
    view = _view(registry, identifier)
    if view.environment_file is None:
        return {}
    try:
        return load_private_environment(view.environment_file)
    except ValueError as error:
        raise InputError(str(error)) from error


def select_context(store: RegistryStore, identifier: str, session: str) -> dict[str, Any]:
    registry = store.read()
    _view(registry, identifier)
    path = _session_path(store, session)
    document = {
        "context": identifier,
        "registry_revision": registry["revision"],
        "updated_at": utc_now(),
    }
    try:
        atomic_write(path, canonical_bytes(document), mode=0o600)
    except OSError as exc:
        raise ExecutionError("session context could not be written") from exc
    return {
        "status": "selected",
        "context": identifier,
        "session": session,
        "registry_revision": registry["revision"],
    }


def current_context(store: RegistryStore, session: str) -> dict[str, Any]:
    path = _session_path(store, session)
    try:
        value = strict_json_loads(path.read_bytes())
    except FileNotFoundError as exc:
        raise InputError(f"session {session} has no selected context") from exc
    except (OSError, UnicodeError, ValueError) as exc:
        raise InputError("session context cannot be read") from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"context", "registry_revision", "updated_at"}
        or not isinstance(value["context"], str)
        or isinstance(value["registry_revision"], bool)
        or not isinstance(value["registry_revision"], int)
        or value["registry_revision"] < 0
        or not isinstance(value["updated_at"], str)
    ):
        raise InputError("session context has an invalid shape")
    registry = store.read()
    _view(registry, value["context"])
    return {
        "context": value["context"],
        "session": session,
        "registry_revision": value["registry_revision"],
        "current_registry_revision": registry["revision"],
        "updated_at": value["updated_at"],
    }


def move_capability(
    store: RegistryStore,
    capability_id: str,
    source_id: str,
    target_id: str,
    expected_revision: int | None,
) -> dict[str, Any]:
    if source_id == target_id:
        raise InputError("source and target contexts must differ")

    def move(registry: dict[str, Any]) -> None:
        source = _view(registry, source_id)
        target = _view(registry, target_id)
        source_capability = _capability(source, capability_id)
        target_capability = _capability(target, capability_id)
        available, _ = explain_availability(source, source_capability)
        if not available:
            raise InputError(
                f"capability {capability_id} is not available in context {source_id}"
            )
        source_document = registry["contexts"][source_id]
        target_document = registry["contexts"][target_id]
        source_document["include_capabilities"] = sorted(
            set(source_document["include_capabilities"]) - {capability_id}
        )
        source_document["exclude_capabilities"] = sorted(
            set(source_document["exclude_capabilities"]) | {capability_id}
        )
        target_document["include_capabilities"] = sorted(
            set(target_document["include_capabilities"]) | {capability_id}
        )
        target_document["exclude_capabilities"] = sorted(
            set(target_document["exclude_capabilities"]) - {capability_id}
        )
        if target_capability["id"] != source_capability["id"]:
            raise InputError("source and target capability identifiers differ")

    registry, record = store.mutate(
        ChangeOperation.MOVE,
        move,
        expected_revision=expected_revision,
    )
    return {
        "status": "moved",
        "capability": capability_id,
        "from": source_id,
        "to": target_id,
        "revision": registry["revision"],
        "change_id": record["change_id"],
    }


def explain_context(
    store: RegistryStore, capability_id: str, context_id: str
) -> dict[str, Any]:
    registry = store.read()
    view = _view(registry, context_id)
    capability = _capability(view, capability_id)
    available, reason = explain_availability(view, capability)
    return {
        "context": context_id,
        "capability": capability_id,
        "available": available,
        "reason": reason.value,
        "revision": registry["revision"],
    }


def share_capability(
    store: RegistryStore,
    capability_id: str,
    target_id: str,
    expected_revision: int | None,
) -> dict[str, Any]:
    def share(registry: dict[str, Any]) -> None:
        target = _view(registry, target_id)
        _capability(target, capability_id)
        document = registry["contexts"][target_id]
        document["include_capabilities"] = sorted(
            set(document["include_capabilities"]) | {capability_id}
        )
        document["exclude_capabilities"] = sorted(
            set(document["exclude_capabilities"]) - {capability_id}
        )

    registry, record = store.mutate(
        ChangeOperation.SHARE,
        share,
        expected_revision=expected_revision,
    )
    return {
        "status": "shared",
        "capability": capability_id,
        "context": target_id,
        "revision": registry["revision"],
        "change_id": record["change_id"],
    }


def unassign_capability(
    store: RegistryStore,
    capability_id: str,
    context_id: str,
    expected_revision: int | None,
) -> dict[str, Any]:
    def unassign(registry: dict[str, Any]) -> None:
        context = _view(registry, context_id)
        capability = _capability(context, capability_id)
        available, _ = explain_availability(context, capability)
        if not available:
            raise InputError(
                f"capability {capability_id} is not available in context {context_id}"
            )
        document = registry["contexts"][context_id]
        document["include_capabilities"] = sorted(
            set(document["include_capabilities"]) - {capability_id}
        )
        document["exclude_capabilities"] = sorted(
            set(document["exclude_capabilities"]) | {capability_id}
        )

    registry, record = store.mutate(
        ChangeOperation.UNASSIGN,
        unassign,
        expected_revision=expected_revision,
    )
    return {
        "status": "unassigned",
        "capability": capability_id,
        "context": context_id,
        "revision": registry["revision"],
        "change_id": record["change_id"],
    }


def context_history(store: RegistryStore, limit: int) -> dict[str, Any]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_HISTORY:
        raise InputError(f"history limit must be from 1 through {MAX_HISTORY}")
    registry = store.read()
    changes = [
        {
            "change_id": item["change_id"],
            "operation": item["operation"],
            "created_at": item["created_at"],
            "base_revision": item["base_revision"],
            "revision": item["revision"],
        }
        for item in registry["history"][-limit:]
    ]
    return {"revision": registry["revision"], "changes": changes}


def doctor_contexts(store: RegistryStore) -> dict[str, Any]:
    registry = store.read()
    results: list[dict[str, Any]] = []
    healthy = True
    for identifier in sorted(registry["contexts"]):
        view = _view(registry, identifier)
        try:
            if view.environment_file is not None:
                load_private_environment(view.environment_file)
            capabilities = _catalog(view)
            visible = sum(
                1
                for capability in capabilities
                if explain_availability(view, capability)[0]
            )
            result = {
                "context": identifier,
                "healthy": True,
                "capability_count": visible,
            }
        except (RouterError, ValueError) as error:
            healthy = False
            result = {
                "context": identifier,
                "healthy": False,
                "error_code": (
                    error.code
                    if isinstance(error, RouterError)
                    else "invalid_environment"
                ),
            }
        results.append(result)
    return {
        "healthy": healthy,
        "revision": registry["revision"],
        "contexts": results,
    }


def undo_change(store: RegistryStore, change_id: str) -> dict[str, Any]:
    target: dict[str, Any] | None = None

    def undo(registry: dict[str, Any]) -> None:
        nonlocal target
        target = next(
            (
                item
                for item in reversed(registry["history"])
                if item["change_id"] == change_id
            ),
            None,
        )
        if target is None:
            raise InputError(f"change {change_id} does not exist")
        if target["operation"] == ChangeOperation.UNDO.value:
            raise InputError("an undo change cannot be undone")
        if registry["contexts"] != target["after"]:
            raise InputError(
                "change cannot be undone after a later registry change",
                {"current_revision": registry["revision"]},
            )
        registry["contexts"] = deepcopy(target["before"])

    registry, record = store.mutate(ChangeOperation.UNDO, undo)
    return {
        "status": "undone",
        "undone_change_id": change_id,
        "change_id": record["change_id"],
        "revision": registry["revision"],
    }
