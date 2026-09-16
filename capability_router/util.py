"""Shared deterministic file and JSON helpers."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any, BinaryIO
import re


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def strict_json_loads(payload: str | bytes) -> Any:
    """Parse interoperable JSON and reject non-finite numbers."""

    text = payload.decode("utf-8") if isinstance(payload, bytes) else payload
    depth = 0
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
        elif character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > 128:
                raise ValueError("JSON nesting is too deep")
        elif character in "]}":
            depth -= 1

    def finite_float(raw: str) -> float:
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError("non-finite JSON number")
        return value

    def reject_constant(raw: str) -> None:
        raise ValueError(f"invalid JSON constant {raw}")

    try:
        result = json.loads(text, parse_float=finite_float, parse_constant=reject_constant)
    except RecursionError as exc:
        raise ValueError("JSON nesting is too deep") from exc
    pending = [result]
    while pending:
        value = pending.pop()
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValueError("JSON contains an invalid Unicode string") from exc
        elif isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
    return result


def read_bounded_line(stream: BinaryIO, limit: int) -> tuple[bytes, bool] | None:
    """Read one line without retaining more than limit bytes in memory."""
    raw = stream.readline(limit + 1)
    if not raw:
        return None
    if len(raw) <= limit:
        return raw, False
    if not raw.endswith(b"\n"):
        while True:
            tail = stream.readline(limit + 1)
            if not tail or tail.endswith(b"\n"):
                break
    return b"", True


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _stage_file(path: Path, payload: bytes, mode: int) -> Path:
    parent_existed = path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not parent_existed:
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    descriptor_owned = True
    try:
        os.fchmod(descriptor, mode)
        stream = os.fdopen(descriptor, "wb")
        descriptor_owned = False
        with stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        if descriptor_owned:
            try:
                os.close(descriptor)
            except OSError:
                pass
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def atomic_write(path: Path, payload: bytes, mode: int = 0o600) -> None:
    temporary = _stage_file(path, payload, mode)
    try:
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def atomic_create(path: Path, payload: bytes, mode: int = 0o600) -> None:
    """Publish a complete file only when the destination does not exist."""
    temporary = _stage_file(path, payload, mode)
    try:
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def safe_component(value: str, *, prefix: str = "run") -> str:
    """Return a bounded path component without trusting caller text."""
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", value) and ".." not in value:
        return value
    return f"{prefix}-{sha256_bytes(value.encode('utf-8'))[:20]}"
