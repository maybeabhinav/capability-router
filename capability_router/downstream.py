"""Bounded stdio MCP client for one owned child process."""

from __future__ import annotations

import os
import queue
import select
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import PRODUCT_NAME, __version__
from .config import Config, Transport
from .errors import ErrorCode, ExecutionError, RouterError
from .protocol import (
    McpMethod,
    McpNotification,
    PREFERRED_PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
)
from .util import canonical_bytes, strict_json_loads


MAX_DOWNSTREAM_FRAME_BYTES = 4 * 1024 * 1024
MAX_QUEUED_FRAMES = 32


class StdioClient:
    def __init__(self, config: Config, server_name: str) -> None:
        self.config = config
        self.server_name = server_name
        self.definition = config.servers[server_name]
        self.process: subprocess.Popen[bytes] | None = None
        self.stdout_queue: queue.Queue[tuple[bytes, bool] | None] = queue.Queue(
            maxsize=MAX_QUEUED_FRAMES
        )
        self.next_id = 1
        self.stop_event = threading.Event()
        self.stdout_thread: threading.Thread | None = None
        self.stderr_thread: threading.Thread | None = None
        self.closed = False
        self.cleanup_in_progress = False
        self.termination_signal_received = False
        self.termination_signum: int | None = None
        self.previous_signal_handlers: dict[signal.Signals, Any] = {}

    def _child_environment(self) -> dict[str, str]:
        environment: dict[str, str] = {}
        overrides = self.config.environment_overrides

        def parent_value(name: str) -> str | None:
            return overrides.get(name, os.environ.get(name))

        for name in self.definition["inherit_environment"]:
            value = parent_value(name)
            if value is not None:
                environment[name] = value
        for child_name, parent_name in self.definition[
            "environment_from_parent"
        ].items():
            value = parent_value(parent_name)
            if value is not None:
                environment[child_name] = value
        return environment

    def _resolve_command(self, environment: dict[str, str]) -> str:
        command = self.definition["command"]
        if "/" in command:
            return command
        child_cwd = Path(self.definition["cwd"])
        path_entries = []
        for entry in environment.get("PATH", "").split(os.pathsep):
            candidate = Path(entry) if entry else child_cwd
            path_entries.append(
                str(candidate if candidate.is_absolute() else child_cwd / candidate)
            )
        resolved = shutil.which(command, path=os.pathsep.join(path_entries))
        if not resolved:
            raise ExecutionError(f"server {self.server_name} executable was not found")
        return resolved

    def _spawn(self, command: str, environment: dict[str, str]) -> None:
        try:
            self.process = subprocess.Popen(
                [command, *self.definition["args"]],
                cwd=self.definition["cwd"],
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            raise ExecutionError(f"could not start server {self.server_name}") from exc

    def _start_io_threads(self) -> None:
        assert self.process is not None
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        assert self.process.stderr is not None
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            os.set_blocking(stream.fileno(), False)
        self.stdout_thread = threading.Thread(target=self._drain_stdout, daemon=True)
        self.stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self.stdout_thread.start()
        self.stderr_thread.start()

    def _install_signal_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for signum in (signal.SIGTERM, signal.SIGINT):
            self.previous_signal_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, self._handle_termination)

    def __enter__(self) -> "StdioClient":
        environment = self._child_environment()
        command = self._resolve_command(environment)
        self._spawn(command, environment)
        try:
            self._start_io_threads()
            self._install_signal_handlers()
        except Exception as exc:
            self.close()
            raise ExecutionError(f"could not initialize server {self.server_name}") from exc
        return self

    def _handle_termination(self, signum: int, frame: Any) -> None:
        if self.termination_signal_received:
            return
        self.termination_signal_received = True
        self.termination_signum = signum
        if self.cleanup_in_progress:
            return
        self.close()
        raise SystemExit(128 + signum)

    def _restore_signal_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for signum, handler in self.previous_signal_handlers.items():
            signal.signal(signum, handler)
        self.previous_signal_handlers.clear()

    def _enqueue(self, record: tuple[bytes, bool] | None) -> bool:
        while not self.stop_event.is_set():
            try:
                self.stdout_queue.put(record, timeout=0.05)
                return True
            except queue.Full:
                continue
        return False

    def _drain_stdout(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        descriptor = self.process.stdout.fileno()
        line = bytearray()
        oversized = False
        while not self.stop_event.is_set():
            try:
                readable, _, _ = select.select([descriptor], [], [], 0.05)
                if not readable:
                    continue
                chunk = os.read(descriptor, 65536)
            except (OSError, ValueError):
                return
            if not chunk:
                break
            start = 0
            while start < len(chunk):
                newline = chunk.find(b"\n", start)
                end = len(chunk) if newline < 0 else newline + 1
                segment = chunk[start:end]
                if not oversized:
                    if len(line) + len(segment) > MAX_DOWNSTREAM_FRAME_BYTES:
                        line.clear()
                        oversized = True
                    else:
                        line.extend(segment)
                if newline >= 0:
                    if not self._enqueue((bytes(line), oversized)):
                        return
                    line.clear()
                    oversized = False
                start = end
        if line or oversized:
            self._enqueue((bytes(line), oversized))
        self._enqueue(None)

    def _drain_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        descriptor = self.process.stderr.fileno()
        while not self.stop_event.is_set():
            try:
                readable, _, _ = select.select([descriptor], [], [], 0.05)
                if not readable:
                    continue
                chunk = os.read(descriptor, 4096)
            except (OSError, ValueError):
                return
            if not chunk:
                return

    def _send(self, frame: dict[str, Any], *, deadline: float) -> None:
        assert self.process is not None and self.process.stdin is not None
        descriptor = self.process.stdin.fileno()
        remaining_payload = memoryview(canonical_bytes(frame) + b"\n")
        while remaining_payload:
            remaining_time = deadline - time.monotonic()
            if remaining_time <= 0:
                raise RouterError(ErrorCode.TIMEOUT, "downstream call timed out")
            try:
                _, writable, _ = select.select([], [descriptor], [], remaining_time)
            except (OSError, ValueError) as exc:
                raise RouterError(
                    ErrorCode.DOWNSTREAM_PROTOCOL_ERROR,
                    "downstream server closed its input",
                ) from exc
            if not writable:
                raise RouterError(ErrorCode.TIMEOUT, "downstream call timed out")
            try:
                written = os.write(descriptor, remaining_payload)
            except BlockingIOError:
                continue
            except (BrokenPipeError, OSError) as exc:
                raise RouterError(
                    ErrorCode.DOWNSTREAM_PROTOCOL_ERROR,
                    "downstream server closed its input",
                ) from exc
            if written <= 0:
                raise RouterError(
                    ErrorCode.DOWNSTREAM_PROTOCOL_ERROR,
                    "downstream server closed its input",
                )
            remaining_payload = remaining_payload[written:]

    def _answer_request(self, request: dict[str, Any], *, deadline: float) -> bool:
        method = request.get("method")
        if not isinstance(method, str) or "id" not in request:
            return False
        request_id = request["id"]
        if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
            raise RouterError(
                ErrorCode.DOWNSTREAM_PROTOCOL_ERROR,
                "downstream request id is invalid",
            )
        if method == McpMethod.PING:
            response = {"jsonrpc": "2.0", "id": request_id, "result": {}}
        else:
            response = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": "method not found"},
            }
        self._send(response, deadline=deadline)
        return True

    def request(
        self,
        method: McpMethod,
        params: dict[str, Any] | None = None,
        *,
        deadline: float,
    ) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        frame: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            frame["params"] = params
        self._send(frame, deadline=deadline)
        while True:
            response = self._receive_response(deadline)
            if self._answer_request(response, deadline=deadline):
                continue
            if not self._is_expected_response(response, request_id):
                continue
            return self._response_result(response)

    def _receive_response(self, deadline: float) -> dict[str, Any]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RouterError(ErrorCode.TIMEOUT, "downstream call timed out")
        try:
            record = self.stdout_queue.get(timeout=remaining)
        except queue.Empty as exc:
            raise RouterError(ErrorCode.TIMEOUT, "downstream call timed out") from exc
        if record is None:
            raise RouterError(
                ErrorCode.DOWNSTREAM_PROTOCOL_ERROR,
                "downstream server closed without a response",
            )
        raw, oversized = record
        if oversized:
            raise RouterError(
                ErrorCode.DOWNSTREAM_PROTOCOL_ERROR,
                "downstream response exceeded the frame limit",
            )
        try:
            response = strict_json_loads(raw)
        except (UnicodeError, ValueError) as exc:
            raise RouterError(
                ErrorCode.DOWNSTREAM_PROTOCOL_ERROR,
                "downstream returned invalid JSON",
            ) from exc
        if not isinstance(response, dict):
            raise RouterError(
                ErrorCode.DOWNSTREAM_PROTOCOL_ERROR,
                "downstream returned an invalid response",
            )
        if response.get("jsonrpc") != "2.0":
            raise RouterError(
                ErrorCode.DOWNSTREAM_PROTOCOL_ERROR,
                "downstream JSON-RPC version is invalid",
            )
        return response

    def _is_expected_response(self, response: dict[str, Any], request_id: int) -> bool:
        if "id" not in response:
            if isinstance(response.get("method"), str):
                return False
            raise RouterError(
                ErrorCode.DOWNSTREAM_PROTOCOL_ERROR,
                "downstream returned an invalid notification",
            )
        response_id = response.get("id")
        if response_id is None:
            return False
        if (
            isinstance(response_id, bool)
            or not isinstance(response_id, (str, int))
            or type(response_id) is not type(request_id)
            or response_id != request_id
        ):
            raise RouterError(
                ErrorCode.DOWNSTREAM_PROTOCOL_ERROR,
                "downstream returned a mismatched response",
            )
        return True

    @staticmethod
    def _response_result(response: dict[str, Any]) -> dict[str, Any]:
        if "error" in response:
            raise RouterError(
                ErrorCode.DOWNSTREAM_PROTOCOL_ERROR,
                "downstream returned a protocol error",
            )
        result = response.get("result")
        if not isinstance(result, dict):
            raise RouterError(
                ErrorCode.DOWNSTREAM_PROTOCOL_ERROR,
                "downstream result is invalid",
            )
        return result

    def initialize(self, *, deadline: float) -> None:
        result = self.request(
            McpMethod.INITIALIZE,
            {
                "protocolVersion": PREFERRED_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": PRODUCT_NAME, "version": __version__},
            },
            deadline=deadline,
        )
        server_info = result.get("serverInfo")
        if (
            not isinstance(result.get("protocolVersion"), str)
            or result["protocolVersion"] not in SUPPORTED_PROTOCOL_VERSIONS
            or not isinstance(result.get("capabilities"), dict)
            or not isinstance(server_info, dict)
            or not isinstance(server_info.get("name"), str)
            or not isinstance(server_info.get("version"), str)
        ):
            raise RouterError(
                ErrorCode.DOWNSTREAM_PROTOCOL_ERROR,
                "downstream initialization result is invalid",
            )
        self._send(
            {
                "jsonrpc": "2.0",
                "method": McpNotification.INITIALIZED,
                "params": {},
            },
            deadline=deadline,
        )

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        deadline: float,
    ) -> dict[str, Any]:
        result = self.request(
            McpMethod.CALL_TOOL,
            {"name": name, "arguments": arguments},
            deadline=deadline,
        )
        if (
            not isinstance(result.get("content"), list)
            or ("isError" in result and not isinstance(result["isError"], bool))
            or (
                "structuredContent" in result
                and not isinstance(result["structuredContent"], dict)
            )
        ):
            raise RouterError(
                ErrorCode.DOWNSTREAM_PROTOCOL_ERROR,
                "downstream tool result is invalid",
            )
        return result

    def list_tools(self, *, deadline: float, wanted: str | None = None) -> list[dict[str, Any]]:
        cursor: str | None = None
        seen: set[str] = set()
        found: list[dict[str, Any]] = []
        for _ in range(100):
            params = {} if cursor is None else {"cursor": cursor}
            page = self.request(McpMethod.LIST_TOOLS, params, deadline=deadline)
            tools = page.get("tools")
            if not isinstance(tools, list) or not all(isinstance(item, dict) for item in tools):
                raise RouterError(
                    ErrorCode.DOWNSTREAM_PROTOCOL_ERROR,
                    "downstream tools/list result is invalid",
                )
            found.extend(tools)
            if wanted and any(item.get("name") == wanted for item in tools):
                return found
            next_cursor = page.get("nextCursor")
            if next_cursor is None:
                return found
            if not isinstance(next_cursor, str) or next_cursor in seen:
                raise RouterError(
                    ErrorCode.DOWNSTREAM_PROTOCOL_ERROR,
                    "downstream pagination cursor is invalid",
                )
            seen.add(next_cursor)
            cursor = next_cursor
        raise RouterError(
            ErrorCode.DOWNSTREAM_PROTOCOL_ERROR,
            "downstream pagination limit exceeded",
        )

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def _notify_stdout_shutdown(self) -> None:
        try:
            self.stdout_queue.put_nowait(None)
        except queue.Full:
            try:
                self.stdout_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self.stdout_queue.put_nowait(None)
            except queue.Full:
                pass

    def _cleanup_process(self, process: subprocess.Popen[bytes]) -> None:
        group_id = process.pid
        group_exists = True
        try:
            os.killpg(group_id, signal.SIGTERM)
        except OSError:
            group_exists = False
        try:
            process.wait(timeout=0.25)
        except subprocess.TimeoutExpired:
            pass
        if group_exists:
            try:
                os.killpg(group_id, 0)
            except OSError:
                group_exists = False
        if group_exists:
            try:
                os.killpg(group_id, signal.SIGKILL)
            except OSError:
                pass
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
        for thread in (self.stdout_thread, self.stderr_thread):
            if thread is not None and thread.ident is not None:
                thread.join(timeout=0.5)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.cleanup_in_progress = True
        try:
            self.stop_event.set()
            self._notify_stdout_shutdown()
            if self.process is not None:
                self._cleanup_process(self.process)
            while True:
                try:
                    self.stdout_queue.get_nowait()
                except queue.Empty:
                    break
        finally:
            self.cleanup_in_progress = False
            self._restore_signal_handlers()
        if self.termination_signum is not None:
            raise SystemExit(128 + self.termination_signum)


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(
        self,
        request: Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> None:
        return None


class HttpClient:
    """Bounded MCP Streamable HTTP client."""

    def __init__(self, config: Config, server_name: str) -> None:
        self.config = config
        self.server_name = server_name
        self.definition = config.servers[server_name]
        self.next_id = 1
        self.session_id: str | None = None
        self.protocol_version = PREFERRED_PROTOCOL_VERSION
        self.opener = build_opener(_NoRedirects())

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        return

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": self.protocol_version,
        }
        overrides = self.config.environment_overrides
        for header, parent_name in self.definition["headers_from_parent"].items():
            value = overrides.get(parent_name, os.environ.get(parent_name))
            if value is not None:
                if any(character in value for character in "\r\n\0"):
                    raise RouterError(
                        ErrorCode.DOWNSTREAM_PROTOCOL_ERROR,
                        "downstream HTTP header value is invalid",
                    )
                headers[header] = value
        if self.session_id is not None:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    @staticmethod
    def _sse_payload(payload: bytes) -> bytes:
        data: list[bytes] = []
        for line in payload.splitlines():
            if line.startswith(b"data:"):
                data.append(line[5:].lstrip())
            elif not line and data:
                break
        if not data:
            raise RouterError(
                ErrorCode.DOWNSTREAM_PROTOCOL_ERROR,
                "downstream returned invalid event stream",
            )
        return b"\n".join(data)

    def _post(
        self,
        frame: dict[str, Any],
        *,
        deadline: float,
        notification: bool = False,
    ) -> dict[str, Any] | None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RouterError(ErrorCode.TIMEOUT, "downstream call timed out")
        request = Request(
            self.definition["url"],
            data=canonical_bytes(frame),
            headers=self._headers(),
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=remaining) as response:
                session_id = response.headers.get("Mcp-Session-Id")
                if session_id is not None:
                    if not session_id or "\x00" in session_id:
                        raise RouterError(
                            ErrorCode.DOWNSTREAM_PROTOCOL_ERROR,
                            "downstream session identifier is invalid",
                        )
                    self.session_id = session_id
                payload = response.read(MAX_DOWNSTREAM_FRAME_BYTES + 1)
                content_type = response.headers.get_content_type()
                status = response.status
        except HTTPError as error:
            raise RouterError(
                ErrorCode.DOWNSTREAM_PROTOCOL_ERROR,
                "downstream HTTP request failed",
                {"status": error.code},
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            if time.monotonic() >= deadline:
                raise RouterError(
                    ErrorCode.TIMEOUT,
                    "downstream call timed out",
                ) from error
            raise RouterError(
                ErrorCode.DOWNSTREAM_PROTOCOL_ERROR,
                "downstream HTTP request failed",
            ) from error
        if len(payload) > MAX_DOWNSTREAM_FRAME_BYTES:
            raise RouterError(
                ErrorCode.DOWNSTREAM_PROTOCOL_ERROR,
                "downstream response exceeded the frame limit",
            )
        if notification and status in {202, 204}:
            return None
        if not payload:
            raise RouterError(
                ErrorCode.DOWNSTREAM_PROTOCOL_ERROR,
                "downstream returned an empty response",
            )
        raw = self._sse_payload(payload) if content_type == "text/event-stream" else payload
        try:
            response_document = strict_json_loads(raw)
        except (UnicodeError, ValueError) as error:
            raise RouterError(
                ErrorCode.DOWNSTREAM_PROTOCOL_ERROR,
                "downstream returned invalid JSON",
            ) from error
        if (
            not isinstance(response_document, dict)
            or response_document.get("jsonrpc") != "2.0"
        ):
            raise RouterError(
                ErrorCode.DOWNSTREAM_PROTOCOL_ERROR,
                "downstream returned an invalid response",
            )
        return response_document

    def request(
        self,
        method: McpMethod,
        params: dict[str, Any] | None = None,
        *,
        deadline: float,
    ) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        frame: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params is not None:
            frame["params"] = params
        response = self._post(frame, deadline=deadline)
        assert response is not None
        response_id = response.get("id")
        if (
            isinstance(response_id, bool)
            or not isinstance(response_id, int)
            or response_id != request_id
        ):
            raise RouterError(
                ErrorCode.DOWNSTREAM_PROTOCOL_ERROR,
                "downstream returned a mismatched response",
            )
        return StdioClient._response_result(response)

    def initialize(self, *, deadline: float) -> None:
        result = self.request(
            McpMethod.INITIALIZE,
            {
                "protocolVersion": PREFERRED_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": PRODUCT_NAME, "version": __version__},
            },
            deadline=deadline,
        )
        server_info = result.get("serverInfo")
        version = result.get("protocolVersion")
        if (
            not isinstance(version, str)
            or version not in SUPPORTED_PROTOCOL_VERSIONS
            or not isinstance(result.get("capabilities"), dict)
            or not isinstance(server_info, dict)
            or not isinstance(server_info.get("name"), str)
            or not isinstance(server_info.get("version"), str)
        ):
            raise RouterError(
                ErrorCode.DOWNSTREAM_PROTOCOL_ERROR,
                "downstream initialization result is invalid",
            )
        self.protocol_version = version
        self._post(
            {
                "jsonrpc": "2.0",
                "method": McpNotification.INITIALIZED,
                "params": {},
            },
            deadline=deadline,
            notification=True,
        )

    def list_tools(
        self,
        *,
        deadline: float,
        wanted: str | None = None,
    ) -> list[dict[str, Any]]:
        cursor: str | None = None
        seen: set[str] = set()
        found: list[dict[str, Any]] = []
        for _ in range(100):
            params = {} if cursor is None else {"cursor": cursor}
            page = self.request(McpMethod.LIST_TOOLS, params, deadline=deadline)
            tools = page.get("tools")
            if not isinstance(tools, list) or not all(
                isinstance(item, dict) for item in tools
            ):
                raise RouterError(
                    ErrorCode.DOWNSTREAM_PROTOCOL_ERROR,
                    "downstream tools/list result is invalid",
                )
            found.extend(tools)
            if wanted and any(item.get("name") == wanted for item in tools):
                return found
            next_cursor = page.get("nextCursor")
            if next_cursor is None:
                return found
            if not isinstance(next_cursor, str) or next_cursor in seen:
                raise RouterError(
                    ErrorCode.DOWNSTREAM_PROTOCOL_ERROR,
                    "downstream pagination cursor is invalid",
                )
            seen.add(next_cursor)
            cursor = next_cursor
        raise RouterError(
            ErrorCode.DOWNSTREAM_PROTOCOL_ERROR,
            "downstream pagination limit exceeded",
        )

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        deadline: float,
    ) -> dict[str, Any]:
        result = self.request(
            McpMethod.CALL_TOOL,
            {"name": name, "arguments": arguments},
            deadline=deadline,
        )
        if (
            not isinstance(result.get("content"), list)
            or ("isError" in result and not isinstance(result["isError"], bool))
            or (
                "structuredContent" in result
                and not isinstance(result["structuredContent"], dict)
            )
        ):
            raise RouterError(
                ErrorCode.DOWNSTREAM_PROTOCOL_ERROR,
                "downstream tool result is invalid",
            )
        return result


def downstream_client(
    config: Config,
    server_name: str,
) -> StdioClient | HttpClient:
    transport = config.servers[server_name]["transport"]
    clients = {
        Transport.STDIO: StdioClient,
        Transport.HTTP: HttpClient,
    }
    return clients[transport](config, server_name)
