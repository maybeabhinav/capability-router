"""Load a private environment file and delegate to the router CLI."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

from .cli import main as router_main
from .environment import load_private_environment


def _arguments(argv: list[str] | None) -> tuple[Path, list[str]]:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--env-file", required=True)
    known, remaining = parser.parse_known_args(argv)
    return Path(known.env_file).expanduser().resolve(), remaining


def main(argv: list[str] | None = None) -> int:
    try:
        environment_file, remaining = _arguments(argv)
        values = load_private_environment(environment_file)
    except (SystemExit, ValueError) as error:
        message = str(error)
        if message and message != "2":
            print(message, file=sys.stderr)
        return 2
    os.environ.update(values)
    return router_main(remaining)


if __name__ == "__main__":
    raise SystemExit(main())
