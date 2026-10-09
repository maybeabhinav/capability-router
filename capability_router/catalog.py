"""Catalog generation, verification, and compact ranking."""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
import fcntl
import math
import os
from pathlib import Path
import re
import time
from typing import Any, Iterator

from .config import AccessLevel, Config
from .downstream import HttpClient, StdioClient, downstream_client
from .errors import ErrorCode, ExecutionError, InputError, RouterError, VerificationError
from .protocol import CapabilityAvailability, CapabilityKind, TaskSupport
from .schema import schema_supported
from .util import atomic_write, canonical_bytes, sha256_bytes, strict_json_loads, utc_now


def _parse_skill(path: Path) -> tuple[str, str, bytes]:
    payload = path.read_bytes()
    text = payload.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    name = path.parent.name
    description = ""
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end >= 0:
            lines = text[4:end].splitlines()
            index = 0
            while index < len(lines):
                line = lines[index]
                if line[:1].isspace():
                    index += 1
                    continue
                key, separator, value = line.partition(":")
                key = key.strip()
                value = value.strip()
                if separator and key in {"name", "description"}:
                    if value in {">", ">-", "|", "|-"}:
                        folded: list[str] = []
                        index += 1
                        while index < len(lines) and (
                            not lines[index].strip() or lines[index][0].isspace()
                        ):
                            folded.append(lines[index].strip())
                            index += 1
                        separator_text = " " if value.startswith(">") else "\n"
                        value = separator_text.join(part for part in folded if part)
                        index -= 1
                    elif len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                        value = value[1:-1]
                    if key == "name" and value:
                        name = value
                    elif key == "description":
                        description = value
                index += 1
    return name, description, payload


def _skill_entries(config: Config) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for root in config.skill_roots:
        root_path: Path = root["path"]
        if not root_path.is_dir():
            continue
        for skill_path in sorted(root_path.glob("*/SKILL.md")):
            try:
                resolved = skill_path.resolve(strict=True)
                if (
                    not resolved.is_relative_to(root_path.resolve())
                    or not resolved.is_file()
                    or skill_path.is_symlink()
                ):
                    continue
                name, description, payload = _parse_skill(skill_path)
            except (OSError, UnicodeError):
                continue
            entries.append(
                {
                    "id": f"{root['name']}.{name}",
                    "kind": CapabilityKind.SKILL,
                    "name": name,
                    "description": description,
                    "source": root["name"],
                    "access": AccessLevel.READ,
                    "availability": CapabilityAvailability.AVAILABLE,
                    "skill_path": str(resolved),
                    "skill_root": str(root_path.resolve()),
                    "source_sha256": sha256_bytes(payload),
                    "source_size": len(payload),
                    "schema_sha256": None,
                    "input_schema": None,
                    "adapter": None,
                    "search_terms": sorted(set(_terms(payload.decode("utf-8")))),
                    "stale": False,
                }
            )
    return entries


def _validate_catalog(catalog: Any) -> None:
    if (
        not isinstance(catalog, dict)
        or catalog.get("version") != 1
        or not isinstance(catalog.get("capabilities"), list)
    ):
        raise VerificationError("catalog shape is invalid")
    ids: set[str] = set()
    for item in catalog["capabilities"]:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or item["id"] in ids
        ):
            raise VerificationError("catalog contains an invalid or duplicate capability")
        ids.add(item["id"])
        if (
            item.get("kind") == CapabilityKind.MCP_TOOL
            and item.get("availability") == CapabilityAvailability.AVAILABLE
        ):
            schema = item.get("input_schema")
            if not schema_supported(schema):
                raise VerificationError("catalog contains an unsupported schema")
            expected = sha256_bytes(canonical_bytes(schema))
            if item.get("schema_sha256") != expected:
                raise VerificationError("catalog schema digest does not match")


def load_catalog(config: Config) -> tuple[dict[str, Any], str]:
    try:
        payload = config.catalog_path.read_bytes()
        catalog = strict_json_loads(payload)
    except (OSError, UnicodeError, ValueError) as exc:
        raise VerificationError("catalog cannot be read") from exc
    _validate_catalog(catalog)
    return catalog, sha256_bytes(payload)


def _tool_entry(
    server_name: str,
    definition: dict[str, Any],
    tool: dict[str, Any],
) -> dict[str, Any] | None:
    name = tool.get("name")
    description = tool.get("description", "")
    schema = tool.get("inputSchema")
    if (
        not isinstance(name, str)
        or not isinstance(description, str)
        or not isinstance(schema, dict)
    ):
        return None
    supported = tool_supported(tool)
    return {
        "id": f"{server_name}.{name}",
        "kind": CapabilityKind.MCP_TOOL,
        "name": name,
        "description": description,
        "source": server_name,
        "access": definition["access_overrides"].get(name, definition["default_access"]),
        "availability": (
            CapabilityAvailability.AVAILABLE
            if supported
            else CapabilityAvailability.UNAVAILABLE
        ),
        "input_schema": schema,
        "schema_sha256": sha256_bytes(canonical_bytes(schema)),
        "adapter": {"server": server_name},
        "search_terms": [],
        "source_sha256": None,
        "stale": False,
    }


def tool_supported(tool: dict[str, Any]) -> bool:
    schema = tool.get("inputSchema")
    if not isinstance(schema, dict):
        return False
    execution = tool.get("execution", {})
    try:
        task_support = TaskSupport(execution.get("taskSupport", TaskSupport.FORBIDDEN))
    except (AttributeError, TypeError, ValueError):
        task_support = TaskSupport.REQUIRED
    return schema_supported(schema) and task_support is not TaskSupport.REQUIRED


def _discover_server(
    config: Config,
    server_name: str,
    definition: dict[str, Any],
    *,
    client: StdioClient | HttpClient | None = None,
    deadline: float | None = None,
) -> list[dict[str, Any]]:
    if deadline is None:
        deadline = time.monotonic() + config.call_timeout_seconds
    if client is not None:
        tools = client.list_tools(deadline=deadline)
    else:
        with downstream_client(config, server_name) as discovery_client:
            discovery_client.initialize(deadline=deadline)
            tools = discovery_client.list_tools(deadline=deadline)
    entries = (_tool_entry(server_name, definition, tool) for tool in tools)
    return [entry for entry in entries if entry is not None]


def _stale_server_entries(
    old: dict[str, Any],
    server_name: str,
    definition: dict[str, Any],
) -> list[dict[str, Any]]:
    preserved_entries: list[dict[str, Any]] = []
    for item in old.get("capabilities", []):
        if (
            item.get("kind") != CapabilityKind.MCP_TOOL
            or item.get("source") != server_name
        ):
            continue
        preserved = dict(item)
        preserved["stale"] = True
        tool_name = preserved.get("name", "")
        preserved["access"] = definition["access_overrides"].get(
            tool_name, definition["default_access"]
        )
        preserved_entries.append(preserved)
    return preserved_entries


@contextmanager
def _refresh_lock(config: Config, deadline: float) -> Iterator[None]:
    lock_path = config.catalog_path.with_name(f"{config.catalog_path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with lock_path.open("a+b") as lock:
        os.fchmod(lock.fileno(), 0o600)
        while True:
            if time.monotonic() >= deadline:
                raise RouterError(ErrorCode.TIMEOUT, "catalog refresh lock timed out")
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def refresh_catalog(
    config: Config,
    *,
    server_name: str | None = None,
    client: StdioClient | HttpClient | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Refresh all sources, or one server using an optional initialized client."""
    if server_name is not None and server_name not in config.servers:
        raise InputError("refresh server is not configured")
    if client is not None and server_name is None:
        raise InputError("refresh client requires a single server")
    servers = config.servers if server_name is None else {
        server_name: config.servers[server_name]
    }
    discovered: dict[str, list[dict[str, Any]]] = {}
    errors: list[dict[str, str]] = []
    for name, definition in servers.items():
        try:
            discovered[name] = _discover_server(
                config, name, definition, client=client, deadline=deadline
            )
        except (RouterError, OSError) as exc:
            error = {"server": name, "error": type(exc).__name__}
            if isinstance(exc, RouterError):
                error["code"] = exc.code
            elif exc.errno is not None:
                error["code"] = f"os_error_{exc.errno}"
            errors.append(error)
    lock_deadline = deadline if deadline is not None else (
        time.monotonic() + config.call_timeout_seconds
    )
    try:
        with _refresh_lock(config, lock_deadline):
            return _write_refreshed_catalog(config, server_name, servers, discovered, errors)
    except OSError as exc:
        raise ExecutionError("could not lock or write catalog") from exc


def _write_refreshed_catalog(
    config: Config,
    server_name: str | None,
    servers: dict[str, dict[str, Any]],
    discovered: dict[str, list[dict[str, Any]]],
    errors: list[dict[str, str]],
) -> dict[str, Any]:
    old: dict[str, Any] = {"capabilities": []}
    if config.catalog_path.exists():
        try:
            old, _ = load_catalog(config)
        except VerificationError:
            if server_name is not None:
                raise
            old = {"capabilities": []}
    if server_name is None:
        entries = _skill_entries(config)
    else:
        entries = [
            item for item in old["capabilities"]
            if not (
                item.get("kind") == CapabilityKind.MCP_TOOL
                and item.get("source") == server_name
            )
        ]
    for name, definition in servers.items():
        if name in discovered:
            entries.extend(discovered[name])
        else:
            entries.extend(_stale_server_entries(old, name, definition))
    ids: set[str] = set()
    for item in entries:
        if item["id"] in ids:
            raise VerificationError("refresh produced a duplicate capability ID")
        ids.add(item["id"])
    if servers and not discovered and not any(
        item.get("kind") == CapabilityKind.MCP_TOOL and item.get("stale")
        for item in entries
    ):
        raise ExecutionError("no downstream server could be refreshed", {"errors": errors})
    catalog = {
        "version": 1,
        "generated_at": utc_now(),
        "capabilities": sorted(entries, key=lambda item: item["id"]),
    }
    _validate_catalog(catalog)
    payload = canonical_bytes(catalog)
    try:
        atomic_write(config.catalog_path, payload)
    except OSError as exc:
        raise ExecutionError("could not write catalog") from exc
    return {
        "status": "refreshed",
        "catalog_path": str(config.catalog_path),
        "capability_count": len(entries),
        "catalog_sha256": sha256_bytes(payload),
        "errors": errors,
    }


def _terms(value: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", value.lower().replace("_", " ").replace("-", " "))
    normalized: list[str] = []
    for word in words:
        if len(word) > 5 and word.endswith("ing"):
            word = word[:-3]
            if len(word) > 2 and word[-1] == word[-2]:
                word = word[:-1]
        elif len(word) > 3 and word.endswith("s"):
            word = word[:-1]
        normalized.append(word)
    return normalized


def _document_terms(item: dict[str, Any]) -> set[str]:
    raw_text = " ".join(
        [
            item["id"],
            item.get("name", ""),
            item.get("description", ""),
        ]
    )
    indexed_terms = {
        term for term in item.get("search_terms", []) if isinstance(term, str)
    }
    return set(_terms(raw_text)) | indexed_terms


def search(
    catalog: dict[str, Any],
    query: str,
    kinds: list[str] | None,
    limit: int,
) -> list[dict[str, Any]]:
    candidates = [
        item
        for item in catalog["capabilities"]
        if not kinds or item.get("kind") in kinds
    ]
    query_terms = Counter(_terms(query))
    document_frequency: Counter[str] = Counter()
    documents: dict[str, set[str]] = {}
    for item in candidates:
        terms = _document_terms(item)
        documents[item["id"]] = terms
        document_frequency.update(terms)
    ranked: list[tuple[float, str, dict[str, Any]]] = []
    count = max(1, len(candidates))
    for item in candidates:
        score = 0.0
        name_terms = set(_terms(item.get("name", "")))
        id_terms = set(_terms(item["id"]))
        description_terms = set(_terms(item.get("description", "")))
        for term, frequency in query_terms.items():
            inverse = math.log((count + 1) / (document_frequency[term] + 1)) + 1
            if item.get("name", "").lower() == term:
                score += 20 * inverse * frequency
            elif term in name_terms:
                score += 12 * inverse * frequency
            elif term in id_terms:
                score += 8 * inverse * frequency
            elif term in description_terms or term in documents[item["id"]]:
                score += 2 * inverse * frequency
        if query.lower() == item["id"].lower() or query.lower() == item.get("name", "").lower():
            score += 20
        if score > 0:
            public = {
                key: item.get(key)
                for key in (
                    "id",
                    "kind",
                    "description",
                    "source",
                    "access",
                    "availability",
                )
            }
            public["score"] = round(score, 6)
            ranked.append((-score, item["id"], public))
    ranked.sort(key=lambda row: (row[0], row[1]))
    return [row[2] for row in ranked[:limit]]
