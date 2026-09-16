"""Strict router configuration loader."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
import os
from pathlib import Path
from typing import Any

from .errors import InputError
from .util import strict_json_loads


class RouterMode(str, Enum):
    READ_ONLY = "read-only"
    UNRESTRICTED = "unrestricted"


class AccessLevel(str, Enum):
    READ = "read"
    WRITE = "write"
    EXTERNAL_WRITE = "external_write"
    DESTRUCTIVE = "destructive"
    UNKNOWN = "unknown"


ACCESS_VALUES = frozenset(item.value for item in AccessLevel)


def _valid_environment_name(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and "=" not in value and "\0" not in value


@dataclass(frozen=True)
class Config:
    path: Path
    root: Path
    mode: RouterMode
    catalog_path: Path
    artifact_dir: Path
    output_limit_bytes: int
    call_timeout_seconds: float
    input_limit_bytes: int
    skill_roots: tuple[dict[str, Any], ...]
    server_sources: tuple[dict[str, Any], ...]
    servers: dict[str, dict[str, Any]]


def _resolve(root: Path, raw: Any, field: str) -> Path:
    if not isinstance(raw, str) or not raw or "\0" in raw:
        raise InputError(f"{field} must be a non-empty string")
    expanded = Path(os.path.expanduser(raw))
    return (root / expanded).absolute() if not expanded.is_absolute() else expanded.absolute()


def _normalize_servers(
    servers: Any,
    *,
    root: Path,
    normalized: dict[str, dict[str, Any]],
) -> None:
    if not isinstance(servers, dict):
        raise InputError("servers must be an object")
    for name, value in servers.items():
        if name in normalized:
            raise InputError(f"duplicate server {name}")
        normalized[name] = _normalize_server(name, value, root)


def _normalize_server(name: Any, value: Any, root: Path) -> dict[str, Any]:
    if not isinstance(name, str) or not name or not isinstance(value, dict):
        raise InputError("server names and values are invalid")
    if value.get("transport") != "stdio":
        raise InputError(f"server {name} uses an unsupported transport")

    command = value.get("command")
    args = value.get("args", [])
    inherit = value.get("inherit_environment", [])
    mappings = value.get("environment_from_parent", {})
    default_access = value.get("default_access", AccessLevel.UNKNOWN.value)
    overrides = value.get("access_overrides", {})
    validators = (
        (isinstance(command, str) and bool(command) and "\0" not in command, "command"),
        (
            isinstance(args, list)
            and all(isinstance(argument, str) and "\0" not in argument for argument in args),
            "args",
        ),
        (
            isinstance(inherit, list)
            and all(_valid_environment_name(item) for item in inherit),
            "inherit_environment",
        ),
        (
            isinstance(mappings, dict)
            and all(
                _valid_environment_name(child) and _valid_environment_name(parent)
                for child, parent in mappings.items()
            ),
            "environment_from_parent",
        ),
        (isinstance(default_access, str) and default_access in ACCESS_VALUES, "default_access"),
        (
            isinstance(overrides, dict)
            and all(
                isinstance(tool, str)
                and isinstance(access, str)
                and access in ACCESS_VALUES
                for tool, access in overrides.items()
            ),
            "access_overrides",
        ),
    )
    invalid = next((field for valid, field in validators if not valid), None)
    if invalid is not None:
        raise InputError(f"server {name} {invalid} is invalid")

    return {
        "transport": "stdio",
        "command": command,
        "args": list(args),
        "cwd": _resolve(root, value.get("cwd", "."), f"server {name} cwd"),
        "inherit_environment": list(inherit),
        "environment_from_parent": dict(mappings),
        "default_access": AccessLevel(default_access),
        "access_overrides": {
            tool: AccessLevel(access) for tool, access in overrides.items()
        },
    }


def _load_json_object(path: Path, read_message: str, shape_message: str) -> dict[str, Any]:
    try:
        value = strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise InputError(read_message) from exc
    if not isinstance(value, dict):
        raise InputError(shape_message)
    return value


def _normalize_limit(value: Any, minimum: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise InputError(f"{field} must be an integer of at least {minimum}")
    return value


def _normalize_timeout(value: Any) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError, OverflowError):
        timeout = math.nan
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputError("call_timeout_seconds must be positive")
    if not math.isfinite(timeout) or timeout <= 0:
        raise InputError("call_timeout_seconds must be positive")
    return timeout


def _normalize_skill_roots(value: Any, root: Path) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list):
        raise InputError("skill_roots must be an array")
    normalized: list[dict[str, Any]] = []
    names: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"name", "path"}:
            raise InputError("each skill root must contain only name and path")
        name = item["name"]
        if not isinstance(name, str) or not name or name in names:
            raise InputError("skill root names must be unique non-empty strings")
        names.add(name)
        normalized.append(
            {"name": name, "path": _resolve(root, item["path"], "skill root path")}
        )
    return tuple(normalized)


def _load_server_sources(
    raw: dict[str, Any], *, root: Path
) -> tuple[tuple[dict[str, Any], ...], dict[str, dict[str, Any]]]:
    sources = raw.get("server_sources", [])
    if not isinstance(sources, list):
        raise InputError("server_sources must be an array")
    normalized_sources: list[dict[str, Any]] = []
    normalized_servers: dict[str, dict[str, Any]] = {}
    source_names: set[str] = set()
    for item in sources:
        if (
            not isinstance(item, dict)
            or not {"name", "path"} <= set(item)
            or not set(item) <= {"name", "path", "optional"}
        ):
            raise InputError(
                "each server source must contain name and path, with optional as "
                "the only optional field"
            )
        name = item["name"]
        optional = item.get("optional", False)
        if not isinstance(name, str) or not name or name in source_names:
            raise InputError("server source names must be unique non-empty strings")
        if not isinstance(optional, bool):
            raise InputError(f"server source {name} optional must be a boolean")
        source_names.add(name)
        source_path = _resolve(root, item["path"], f"server source {name} path")
        if optional and not source_path.exists():
            normalized_sources.append(
                {
                    "name": name,
                    "path": source_path,
                    "optional": True,
                    "loaded": False,
                }
            )
            continue
        try:
            source_raw = strict_json_loads(source_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise InputError(f"cannot read server source {name}") from exc
        if not isinstance(source_raw, dict) or set(source_raw) != {"servers"}:
            raise InputError(f"server source {name} must contain only servers")
        _normalize_servers(
            source_raw["servers"],
            root=source_path.parent,
            normalized=normalized_servers,
        )
        normalized_sources.append(
            {
                "name": name,
                "path": source_path,
                "optional": optional,
                "loaded": True,
            }
        )
    return tuple(normalized_sources), normalized_servers


def load_config(path: str | Path) -> Config:
    config_path = Path(path).expanduser().absolute()
    raw = _load_json_object(
        config_path, "cannot read configuration", "configuration must be an object"
    )
    root = config_path.parent
    try:
        mode = RouterMode(raw.get("mode"))
    except (TypeError, ValueError):
        raise InputError("mode must be read-only or unrestricted")
    output_limit = _normalize_limit(raw.get("output_limit_bytes"), 512, "output_limit_bytes")
    input_limit = _normalize_limit(raw.get("input_limit_bytes"), 1024, "input_limit_bytes")
    timeout = _normalize_timeout(raw.get("call_timeout_seconds"))
    skill_roots = _normalize_skill_roots(raw.get("skill_roots", []), root)
    server_sources, normalized_servers = _load_server_sources(raw, root=root)
    _normalize_servers(raw.get("servers", {}), root=root, normalized=normalized_servers)
    return Config(
        path=config_path,
        root=root,
        mode=mode,
        catalog_path=_resolve(root, raw.get("catalog_path"), "catalog_path"),
        artifact_dir=_resolve(root, raw.get("artifact_dir"), "artifact_dir"),
        output_limit_bytes=output_limit,
        call_timeout_seconds=timeout,
        input_limit_bytes=input_limit,
        skill_roots=skill_roots,
        server_sources=server_sources,
        servers=normalized_servers,
    )
