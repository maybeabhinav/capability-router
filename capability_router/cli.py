"""Capability Router command line interface."""

from __future__ import annotations

import argparse
from enum import Enum
import json
from pathlib import Path
import re
import sys
from typing import Any

from . import PRODUCT_NAME
from .catalog import load_catalog, refresh_catalog
from .config import RouterMode, load_config
from .errors import ExecutionError, InputError, RouterError, VerificationError
from .server import serve
from .util import atomic_create, atomic_write, canonical_bytes


EXIT_INPUT = 2
EXIT_EXECUTION = 4
EXIT_VERIFICATION = 5
DEFAULT_CONFIG = "~/.config/capability-router/config.json"
SKILL_ROOT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


class CliCommand(str, Enum):
    INIT = "init"
    REFRESH = "refresh"
    STATUS = "status"
    SERVE = "serve"


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog=PRODUCT_NAME)
    subcommands = root.add_subparsers(dest="command", required=True)
    command = subcommands.add_parser(
        CliCommand.INIT.value, help="create a safe starter configuration"
    )
    command.add_argument("--config", default=DEFAULT_CONFIG)
    command.add_argument(
        "--mode",
        choices=tuple(mode.value for mode in RouterMode),
        default=RouterMode.READ_ONLY.value,
    )
    command.add_argument(
        "--skill-root",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="add a skill directory; repeat for multiple roots",
    )
    command.add_argument("--force", action="store_true", help="replace an existing configuration")
    for name in (CliCommand.REFRESH, CliCommand.STATUS):
        command = subcommands.add_parser(name.value)
        command.add_argument("--config", required=True)
    command = subcommands.add_parser(CliCommand.SERVE.value)
    command.add_argument("--config", required=True)
    command.add_argument("--run-id", default="router-session")
    command.add_argument("--audit-log")
    return root


def emit(document: dict[str, Any]) -> None:
    sys.stdout.buffer.write(canonical_bytes(document) + b"\n")
    sys.stdout.buffer.flush()


def _skill_roots(values: list[str]) -> list[dict[str, str]]:
    if not values:
        return [
            {"name": "agents", "path": "~/.agents/skills"},
            {"name": "claude", "path": "~/.claude/skills"},
            {"name": "codex", "path": "~/.codex/skills"},
        ]
    roots: list[dict[str, str]] = []
    names: set[str] = set()
    for value in values:
        name, separator, path = value.partition("=")
        if not separator or SKILL_ROOT_NAME.fullmatch(name) is None or not path:
            raise InputError("skill roots must use NAME=PATH")
        if name in names:
            raise InputError("skill root names must be unique")
        names.add(name)
        roots.append({"name": name, "path": path})
    return roots


def initialize(args: argparse.Namespace) -> dict[str, Any]:
    config_path = Path(args.config).expanduser().absolute()
    document = {
        "mode": RouterMode(args.mode),
        "catalog_path": "catalog.json",
        "artifact_dir": "artifacts",
        "output_limit_bytes": 12000,
        "call_timeout_seconds": 30,
        "input_limit_bytes": 1048576,
        "skill_roots": _skill_roots(args.skill_root),
        "servers": {},
    }
    payload = (json.dumps(document, indent=2) + "\n").encode("utf-8")
    try:
        if args.force:
            atomic_write(config_path, payload, mode=0o600)
        else:
            atomic_create(config_path, payload, mode=0o600)
    except FileExistsError as exc:
        raise InputError("configuration already exists; use --force to replace it") from exc
    except OSError as exc:
        raise ExecutionError("could not write configuration") from exc
    return {
        "status": "initialized",
        "config_path": str(config_path),
        "mode": args.mode,
        "skill_root_count": len(document["skill_roots"]),
    }


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        command = CliCommand(args.command)
        if command is CliCommand.INIT:
            emit(initialize(args))
            return 0
        config = load_config(args.config)
        if command is CliCommand.REFRESH:
            emit(refresh_catalog(config))
        elif command is CliCommand.STATUS:
            catalog, digest = load_catalog(config)
            emit(
                {
                    "mode": config.mode,
                    "capability_count": len(catalog["capabilities"]),
                    "catalog_sha256": digest,
                    "catalog_path": str(config.catalog_path),
                    "generated_at": catalog.get("generated_at"),
                }
            )
        elif command is CliCommand.SERVE:
            audit = Path(args.audit_log).expanduser().resolve() if args.audit_log else None
            return serve(config, run_id=args.run_id, audit_path=audit)
        return 0
    except InputError as exc:
        emit(exc.document())
        return EXIT_INPUT
    except ExecutionError as exc:
        emit(exc.document())
        return EXIT_EXECUTION
    except VerificationError as exc:
        emit(exc.document())
        return EXIT_VERIFICATION
    except RouterError as exc:
        emit(exc.document())
        return EXIT_EXECUTION


if __name__ == "__main__":
    raise SystemExit(main())
