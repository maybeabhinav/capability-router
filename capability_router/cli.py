"""Capability Router command line interface."""

from __future__ import annotations

import argparse
from enum import Enum
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable

from . import PRODUCT_NAME
from .catalog import load_catalog, refresh_catalog
from .config import RouterMode, load_config
from .contexts import (
    ContextCommand,
    RegistryStore,
    context_environment,
    context_history,
    create_context,
    current_context,
    doctor_contexts,
    explain_context,
    list_contexts,
    move_capability,
    select_context,
    share_capability,
    unassign_capability,
    undo_change,
)
from .errors import ExecutionError, InputError, RouterError, VerificationError
from .server import serve, serve_context
from .util import atomic_create, atomic_write, canonical_bytes


EXIT_INPUT = 2
EXIT_EXECUTION = 4
EXIT_VERIFICATION = 5
DEFAULT_CONFIG = "~/.config/capability-router/config.json"
DEFAULT_REGISTRY = "~/.config/capability-router/contexts.json"
SKILL_ROOT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


class CliCommand(str, Enum):
    INIT = "init"
    REFRESH = "refresh"
    STATUS = "status"
    SERVE = "serve"
    CONTEXT = "context"


def _context_parser(subcommands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    root = subcommands.add_parser(
        CliCommand.CONTEXT.value,
        help="manage isolated capability contexts",
    )
    root.add_argument("--registry", default=DEFAULT_REGISTRY)
    commands = root.add_subparsers(dest="context_command", required=True)

    command = commands.add_parser(ContextCommand.CREATE.value)
    command.add_argument("context")
    command.add_argument("--config", required=True)
    command.add_argument("--source", action="append", default=[])
    command.add_argument("--environment-file")

    commands.add_parser(ContextCommand.LIST.value)

    command = commands.add_parser(ContextCommand.USE.value)
    command.add_argument("context")
    command.add_argument("--session", required=True)

    command = commands.add_parser(ContextCommand.CURRENT.value)
    command.add_argument("--session", required=True)

    command = commands.add_parser(ContextCommand.MOVE.value)
    command.add_argument("capability")
    command.add_argument("--from", dest="source", required=True)
    command.add_argument("--to", dest="target", required=True)
    command.add_argument("--expected-revision", type=int)

    command = commands.add_parser(ContextCommand.EXPLAIN.value)
    command.add_argument("capability")
    command.add_argument("--context", required=True)

    command = commands.add_parser(ContextCommand.UNDO.value)
    command.add_argument("change_id")

    command = commands.add_parser(ContextCommand.SHARE.value)
    command.add_argument("capability")
    command.add_argument("--to", dest="target", required=True)
    command.add_argument("--expected-revision", type=int)

    command = commands.add_parser(ContextCommand.UNASSIGN.value)
    command.add_argument("capability")
    command.add_argument("--context", required=True)
    command.add_argument("--expected-revision", type=int)

    command = commands.add_parser(ContextCommand.HISTORY.value)
    command.add_argument("--limit", type=int, default=20)

    commands.add_parser(ContextCommand.DOCTOR.value)

    command = commands.add_parser(ContextCommand.EXEC.value)
    command.add_argument("context")
    command.add_argument("program", nargs=argparse.REMAINDER)


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
    source = command.add_mutually_exclusive_group(required=True)
    source.add_argument("--config")
    source.add_argument("--registry")
    command.add_argument("--session")
    command.add_argument("--run-id", default="router-session")
    command.add_argument("--audit-log")
    _context_parser(subcommands)
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


def _context_exec(args: argparse.Namespace) -> int:
    command = list(args.program)
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        raise InputError("context exec requires a command")
    environment = os.environ.copy()
    environment.update(
        context_environment(RegistryStore(args.registry), args.context)
    )
    try:
        return subprocess.run(
            command,
            env=environment,
            check=False,
        ).returncode
    except OSError as error:
        raise ExecutionError("context command could not start") from error


def _context(args: argparse.Namespace) -> dict[str, Any]:
    store = RegistryStore(args.registry)
    command = ContextCommand(args.context_command)
    handlers: dict[ContextCommand, Callable[[], dict[str, Any]]] = {
        ContextCommand.CREATE: lambda: create_context(
            store,
            args.context,
            args.config,
            args.source,
            args.environment_file,
        ),
        ContextCommand.LIST: lambda: list_contexts(store),
        ContextCommand.USE: lambda: select_context(
            store, args.context, args.session
        ),
        ContextCommand.CURRENT: lambda: current_context(store, args.session),
        ContextCommand.MOVE: lambda: move_capability(
            store,
            args.capability,
            args.source,
            args.target,
            args.expected_revision,
        ),
        ContextCommand.EXPLAIN: lambda: explain_context(
            store, args.capability, args.context
        ),
        ContextCommand.UNDO: lambda: undo_change(store, args.change_id),
        ContextCommand.SHARE: lambda: share_capability(
            store,
            args.capability,
            args.target,
            args.expected_revision,
        ),
        ContextCommand.UNASSIGN: lambda: unassign_capability(
            store,
            args.capability,
            args.context,
            args.expected_revision,
        ),
        ContextCommand.HISTORY: lambda: context_history(store, args.limit),
        ContextCommand.DOCTOR: lambda: doctor_contexts(store),
    }
    return handlers[command]()


def _configured(args: argparse.Namespace, command: CliCommand) -> int:
    config = load_config(args.config)
    if command is CliCommand.REFRESH:
        emit(refresh_catalog(config))
        return 0
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
    return 0


def _serve(args: argparse.Namespace) -> int:
    audit = Path(args.audit_log).expanduser().resolve() if args.audit_log else None
    if args.registry:
        if not args.session:
            raise InputError("--session is required with --registry")
        return serve_context(
            args.registry,
            args.session,
            run_id=args.run_id,
            audit_path=audit,
        )
    if args.session:
        raise InputError("--session requires --registry")
    return serve(
        load_config(args.config),
        run_id=args.run_id,
        audit_path=audit,
    )


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        command = CliCommand(args.command)
        if command is CliCommand.INIT:
            emit(initialize(args))
            return 0
        if command is CliCommand.CONTEXT:
            if args.context_command == ContextCommand.EXEC.value:
                return _context_exec(args)
            emit(_context(args))
            return 0
        if command is CliCommand.SERVE:
            return _serve(args)
        return _configured(args, command)
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
