"""Load a private environment file and delegate to the router CLI."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any

from .cli import main as router_main
from .util import strict_json_loads


ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _arguments(argv: list[str] | None) -> tuple[Path, list[str]]:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--env-file", required=True)
    known, remaining = parser.parse_known_args(argv)
    return Path(known.env_file).expanduser().resolve(), remaining


def _load_environment(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise ValueError("private environment file is unavailable")
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ValueError("private environment file must use mode 0600")
    try:
        document: Any = strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError("private environment file is invalid") from error
    if not isinstance(document, dict):
        raise ValueError("private environment file must contain an object")
    values: dict[str, str] = {}
    for name, value in document.items():
        if not isinstance(name, str) or ENVIRONMENT_NAME.fullmatch(name) is None:
            raise ValueError("private environment file contains an invalid variable name")
        if not isinstance(value, str) or "\x00" in value:
            raise ValueError("private environment file contains an invalid value")
        values[name] = value
    return values


def main(argv: list[str] | None = None) -> int:
    try:
        environment_file, remaining = _arguments(argv)
        values = _load_environment(environment_file)
    except (SystemExit, ValueError) as error:
        message = str(error)
        if message and message != "2":
            print(message, file=sys.stderr)
        return 2
    os.environ.update(values)
    return router_main(remaining)


if __name__ == "__main__":
    raise SystemExit(main())
