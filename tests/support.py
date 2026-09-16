"""Public-process helpers for capability router contract tests."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterator
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_SERVER = PROJECT_ROOT / "tests" / "fixture_mcp.py"
FIXTURE_SKILLS = PROJECT_ROOT / "tests" / "fixtures" / "skills"
CLI = [sys.executable, "-m", "capability_router.cli"]


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def read_json_lines(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def nested_values(value: Any, key: str) -> list[Any]:
    found: list[Any] = []
    if isinstance(value, dict):
        for candidate, child in value.items():
            if candidate == key:
                found.append(child)
            found.extend(nested_values(child, key))
    elif isinstance(value, list):
        for child in value:
            found.extend(nested_values(child, key))
    return found


def first_nested(value: Any, key: str) -> Any:
    values = nested_values(value, key)
    if not values:
        raise AssertionError(f"missing key {key!r} in {value!r}")
    return values[0]


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_process_gone(pid: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_exists(pid):
            return True
        time.sleep(0.03)
    return not process_exists(pid)


class Workspace:
    def __init__(self, *, output_limit: int = 12_000, timeout: float = 1.0) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="capability-router-test-")
        self.root = Path(self._temporary.name)
        self.state = self.root / "fixture-state"
        self.state.mkdir()
        shutil.copytree(FIXTURE_SKILLS, self.root / "skills")
        self.config_path = self.root / "config.json"
        self.catalog_path = self.root / "runtime" / "catalog.json"
        self.artifact_dir = self.root / "artifacts"
        self.audit_path = self.root / "router-audit.jsonl"
        self.output_limit = output_limit
        self.timeout = timeout
        self.environment = os.environ.copy()
        self.environment.update(
            {
                "ROUTER_TEST_FIXTURE_STATE_DIR": str(self.state),
                "ROUTER_TEST_FIXTURE_MODE": "normal",
                "ROUTER_TEST_FIXTURE_NAME": "fixture",
            }
        )
        self.write_config()

    def close(self) -> None:
        self._temporary.cleanup()

    def server(self, name: str = "fixture") -> dict[str, Any]:
        upper = name.upper().replace("-", "_")
        self.environment.setdefault(f"ROUTER_TEST_{upper}_MODE", "normal")
        self.environment.setdefault(f"ROUTER_TEST_{upper}_NAME", name)
        return {
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(FIXTURE_SERVER)],
            "cwd": ".",
            "inherit_environment": ["PATH"],
            "environment_from_parent": {
                "FIXTURE_STATE_DIR": "ROUTER_TEST_FIXTURE_STATE_DIR",
                "FIXTURE_MODE": f"ROUTER_TEST_{upper}_MODE",
                "FIXTURE_SERVER_NAME": f"ROUTER_TEST_{upper}_NAME",
                "FIXTURE_SECRET": "ROUTER_TEST_SECRET",
            },
            "default_access": "read",
            "access_overrides": {
                "send_message": "external_write",
                "large_write": "write",
                "unsupported_schema": "read",
            },
        }

    def config(self, servers: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "mode": "read-only",
            "catalog_path": "runtime/catalog.json",
            "artifact_dir": "artifacts",
            "output_limit_bytes": self.output_limit,
            "call_timeout_seconds": self.timeout,
            "input_limit_bytes": 1_048_576,
            "skill_roots": [{"name": "fixture", "path": "skills"}],
            "servers": servers or {"fixture": self.server()},
        }

    def write_config(self, servers: dict[str, Any] | None = None) -> None:
        self.config_path.write_text(json.dumps(self.config(servers), indent=2), encoding="utf-8")

    def run_cli(self, command: str, *arguments: str, timeout: float = 10) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*CLI, command, "--config", str(self.config_path), *arguments],
            cwd=PROJECT_ROOT,
            env=self.environment,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )

    def refresh(self) -> dict[str, Any]:
        completed = self.run_cli("refresh")
        if completed.returncode != 0:
            raise AssertionError(
                "refresh failed\n"
                f"exit={completed.returncode}\nstdout={completed.stdout}\nstderr={completed.stderr}"
            )
        return json.loads(completed.stdout)

    def clear_fixture_observation(self) -> None:
        for name in (
            "starts.jsonl",
            "calls.jsonl",
            "process.json",
            "hanging-pids.json",
            "stubborn-pids.json",
            "exited-parent-pids.json",
            "detached-pipe-pids.json",
            "ping-responses.jsonl",
        ):
            (self.state / name).unlink(missing_ok=True)

    @contextlib.contextmanager
    def session(self, *, run_id: str = "contract-run") -> Iterator["McpSession"]:
        session = McpSession(self, run_id=run_id)
        try:
            session.start()
            yield session
        finally:
            session.close()


class McpSession:
    def __init__(self, workspace: Workspace, *, run_id: str, audit_path: Path | None = None) -> None:
        self.workspace = workspace
        self.run_id = run_id
        self.audit_path = workspace.audit_path if audit_path is None else audit_path
        self.process: subprocess.Popen[bytes] | None = None
        self._next_id = 1
        self._selector = selectors.DefaultSelector()
        self.stderr_bytes = b""

    def start(self) -> None:
        self.process = subprocess.Popen(
            [
                *CLI,
                "serve",
                "--config",
                str(self.workspace.config_path),
                "--run-id",
                self.run_id,
                "--audit-log",
                str(self.audit_path),
            ],
            cwd=PROJECT_ROOT,
            env=self.workspace.environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        assert self.process.stdout is not None
        self._selector.register(self.process.stdout, selectors.EVENT_READ)

    def close(self) -> None:
        if self.process is None:
            return
        if self.process.stdin:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=2)
        self._selector.close()
        if self.process.stderr is not None:
            self.stderr_bytes = self.process.stderr.read()
        for stream in (self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()

    def send(self, value: dict[str, Any]) -> None:
        assert self.process is not None and self.process.stdin is not None
        self.process.stdin.write(canonical_bytes(value) + b"\n")
        self.process.stdin.flush()

    def send_raw(self, value: bytes) -> None:
        assert self.process is not None and self.process.stdin is not None
        self.process.stdin.write(value + b"\n")
        self.process.stdin.flush()

    def receive(self, timeout: float = 3) -> dict[str, Any]:
        assert self.process is not None and self.process.stdout is not None
        ready = self._selector.select(timeout)
        if not ready:
            stderr = b""
            if self.process.poll() is not None and self.process.stderr:
                stderr = self.process.stderr.read()
            raise AssertionError(f"no MCP frame received; exit={self.process.poll()} stderr={stderr!r}")
        line = self.process.stdout.readline()
        if not line:
            stderr = self.process.stderr.read() if self.process.stderr else b""
            raise AssertionError(f"router stdout closed; exit={self.process.poll()} stderr={stderr!r}")
        return json.loads(line)

    def request(self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 3) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        frame: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            frame["params"] = params
        self.send(frame)
        response = self.receive(timeout)
        if response.get("id") != request_id:
            raise AssertionError(f"expected response id {request_id}, got {response!r}")
        return response

    def initialize(self, version: str = "2025-11-25") -> dict[str, Any]:
        return self.request(
            "initialize",
            {
                "protocolVersion": version,
                "capabilities": {},
                "clientInfo": {"name": "contract-test", "version": "1"},
            },
        )

    def notify_initialized(self) -> None:
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    def call(self, arguments: dict[str, Any], *, timeout: float = 3) -> dict[str, Any]:
        return self.request(
            "tools/call",
            {"name": "capability", "arguments": arguments},
            timeout=timeout,
        )

    def tool_document(self, arguments: dict[str, Any], *, timeout: float = 3) -> tuple[dict[str, Any], dict[str, Any]]:
        response = self.call(arguments, timeout=timeout)
        result = response.get("result", {})
        if "structuredContent" in result:
            return response, result["structuredContent"]
        content = result.get("content", [])
        if len(content) != 1 or content[0].get("type") != "text":
            raise AssertionError(f"expected one text content block, got {response!r}")
        return response, json.loads(content[0]["text"])

    def assert_tool_error(self, case: unittest.TestCase, arguments: dict[str, Any], code: str) -> dict[str, Any]:
        response, document = self.tool_document(arguments)
        case.assertIs(response["result"].get("isError"), True, response)
        case.assertEqual(first_nested(document, "code"), code, document)
        return document


class RouterTestCase(unittest.TestCase):
    output_limit = 12_000
    call_timeout = 1.0

    def setUp(self) -> None:
        self.workspace = Workspace(output_limit=self.output_limit, timeout=self.call_timeout)

    def tearDown(self) -> None:
        self.workspace.close()

    @contextlib.contextmanager
    def ready_session(self, *, run_id: str = "contract-run") -> Iterator[McpSession]:
        self.workspace.refresh()
        with self.workspace.session(run_id=run_id) as session:
            initialized = session.initialize()
            self.assertIn("result", initialized, initialized)
            session.notify_initialized()
            yield session
