"""Load private environment mappings without exposing their values."""

from __future__ import annotations

import re
import stat
from pathlib import Path
from typing import Any

from .util import strict_json_loads


ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def load_private_environment(path: str | Path) -> dict[str, str]:
    environment_path = Path(path).expanduser().resolve()
    if not environment_path.is_file():
        raise ValueError("private environment file is unavailable")
    if stat.S_IMODE(environment_path.stat().st_mode) != 0o600:
        raise ValueError("private environment file must use mode 0600")
    try:
        document: Any = strict_json_loads(
            environment_path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError("private environment file is invalid") from error
    if not isinstance(document, dict):
        raise ValueError("private environment file must contain an object")
    values: dict[str, str] = {}
    for name, value in document.items():
        if not isinstance(name, str) or ENVIRONMENT_NAME.fullmatch(name) is None:
            raise ValueError(
                "private environment file contains an invalid variable name"
            )
        if not isinstance(value, str) or "\x00" in value:
            raise ValueError("private environment file contains an invalid value")
        values[name] = value
    return values
