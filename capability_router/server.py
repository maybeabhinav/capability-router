"""Newline-delimited MCP server with one public tool."""

from __future__ import annotations

import os
from pathlib import Path
import select
import signal
import sys
import threading
import time
from typing import Any, BinaryIO

from . import PRODUCT_NAME, __version__
from .catalog import load_catalog
from .config import Config
from .errors import ErrorCode, InputError, RouterError
from .executor import Executor
from .protocol import (
    ACTION_FIELDS,
    AuditAction,
    AuditOutcome,
    CAPABILITY_KINDS,
    MAX_SEARCH_QUERY_LENGTH,
    McpMethod,
    PREFERRED_PROTOCOL_VERSION,
    RouterAction,
    SUPPORTED_PROTOCOL_VERSIONS,
)
from .util import canonical_bytes, read_bounded_line, strict_json_loads


MAX_INFLIGHT_REQUESTS = 8
TOOL_ARGUMENT_PROPERTIES = {
    "query": {
        "type": "string",
        "minLength": 1,
        "maxLength": MAX_SEARCH_QUERY_LENGTH,
    },
    "capability_id": {"type": "string", "minLength": 1},
    "arguments": {"type": "object"},
    "kinds": {
        "type": "array",
        "items": {"enum": list(CAPABILITY_KINDS)},
        "minItems": 1,
        "uniqueItems": True,
    },
    "limit": {
        "type": "integer",
        "minimum": 1,
        "maximum": 5,
        "description": "Maximum matches. Use an integer from 1 through 5.",
    },
}


def _action_input_schema(action: RouterAction) -> dict[str, Any]:
    required, optional = ACTION_FIELDS[action]
    fields = required | optional
    properties = {
        field: (
            {"const": action.value}
            if field == "action"
            else TOOL_ARGUMENT_PROPERTIES[field]
        )
        for field in sorted(fields)
    }
    return {
        "type": "object",
        "required": sorted(required),
        "properties": properties,
        "additionalProperties": False,
    }


CAPABILITY_TOOL = {
    "name": "capability",
    "description": (
        "Search, inspect, load, or call an optional capability. Search for one need "
        "per call with limit 1 through 5; do not combine unrelated needs. Search "
        "returns metadata only. A skill is not loaded until load_skill succeeds. "
        "Describe an MCP tool before calling it."
    ),
    "inputSchema": {
        "type": "object",
        "oneOf": [_action_input_schema(action) for action in RouterAction],
    },
}


def _valid_request_id(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (str, int))


class ShutdownRequested(Exception):
    def __init__(self, signum: int) -> None:
        self.signum = signum


class Server:
    def __init__(self, config: Config, *, run_id: str, audit_path: Path | None) -> None:
        catalog, _ = load_catalog(config)
        self.config = config
        self.executor = Executor(config, catalog, run_id=run_id, audit_path=audit_path)
        self.write_lock = threading.Lock()
        self.inflight_lock = threading.Lock()
        self.inflight: set[Any] = set()
        self.worker_lock = threading.Lock()
        self.workers: set[threading.Thread] = set()
        self.worker_slots = threading.BoundedSemaphore(MAX_INFLIGHT_REQUESTS)
        self.shutdown_event = threading.Event()
        self.shutdown_signal_received = False
        self.shutdown_signal_enabled = False
        self.audit_warning_lock = threading.Lock()
        self.audit_warning_emitted = False
        self.output: BinaryIO = sys.stdout.buffer

    def _output_failed(self) -> None:
        if self.shutdown_event.is_set():
            return
        self.shutdown_event.set()
        if self.shutdown_signal_enabled:
            main_ident = threading.main_thread().ident
            try:
                if main_ident is None or not hasattr(signal, "pthread_kill"):
                    raise OSError("main thread signal delivery is unavailable")
                signal.pthread_kill(main_ident, signal.SIGTERM)
            except (OSError, ValueError):
                os.kill(os.getpid(), signal.SIGTERM)

    def write(self, frame: dict[str, Any]) -> None:
        payload = memoryview(canonical_bytes(frame) + b"\n")
        with self.write_lock:
            try:
                descriptor = self.output.fileno()
            except (AttributeError, OSError):
                try:
                    self.output.write(payload)
                    self.output.flush()
                except (BrokenPipeError, OSError, ValueError):
                    self._output_failed()
                return
            while payload and not self.shutdown_event.is_set():
                try:
                    _, writable, _ = select.select([], [descriptor], [], 0.05)
                    if not writable:
                        continue
                    written = os.write(descriptor, payload)
                except (BrokenPipeError, OSError, ValueError):
                    self._output_failed()
                    return
                if written <= 0:
                    self._output_failed()
                    return
                payload = payload[written:]

    def protocol_error(self, request_id: Any, code: int, message: str) -> None:
        self.write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": code, "message": message},
            }
        )

    def serve(self) -> int:
        previous_handlers: dict[signal.Signals, Any] = {}
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGTERM, signal.SIGINT):
                previous_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, self._request_shutdown)
            self.shutdown_signal_enabled = True
        try:
            os.set_blocking(self.output.fileno(), False)
        except (AttributeError, OSError):
            pass
        try:
            return self._serve_requests()
        except ShutdownRequested as exc:
            return 128 + exc.signum
        finally:
            self.shutdown_signal_enabled = False
            self.executor.close_active_clients()
            self._join_workers()
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)

    def _request_shutdown(self, signum: int, frame: Any) -> None:
        self.shutdown_event.set()
        if self.shutdown_signal_received:
            return
        self.shutdown_signal_received = True
        raise ShutdownRequested(signum)

    def _join_workers(self) -> None:
        while True:
            with self.worker_lock:
                workers = tuple(self.workers)
            if not workers:
                return
            for worker in workers:
                worker.join()

    def _serve_requests(self) -> int:
        while not self.shutdown_event.is_set():
            record = read_bounded_line(sys.stdin.buffer, self.config.input_limit_bytes)
            if record is None:
                break
            if self.shutdown_event.is_set():
                break
            raw, oversized = record
            if oversized:
                self.protocol_error(None, -32700, "input too large")
                continue
            try:
                request = strict_json_loads(raw)
            except (UnicodeError, ValueError):
                self.protocol_error(None, -32700, "parse error")
                continue
            if (
                not isinstance(request, dict)
                or request.get("jsonrpc") != "2.0"
                or not isinstance(request.get("method"), str)
            ):
                candidate_id = request.get("id") if isinstance(request, dict) else None
                request_id = candidate_id if _valid_request_id(candidate_id) else None
                self.protocol_error(request_id, -32600, "invalid request")
                continue
            if "id" not in request:
                continue
            request_id = request["id"]
            if not _valid_request_id(request_id):
                self.protocol_error(None, -32600, "invalid request id")
                continue
            if not self.worker_slots.acquire(blocking=False):
                self.protocol_error(request_id, -32000, "server busy")
                continue
            with self.inflight_lock:
                if request_id in self.inflight:
                    self.worker_slots.release()
                    self.protocol_error(request_id, -32600, "duplicate in-flight request id")
                    continue
                self.inflight.add(request_id)
            worker = threading.Thread(target=self._run_worker, args=(request,), daemon=False)
            with self.worker_lock:
                self.workers.add(worker)
            worker.start()
        self._join_workers()
        return 0

    def _run_worker(self, request: dict[str, Any]) -> None:
        try:
            self._handle(request)
        finally:
            self.worker_slots.release()
            with self.worker_lock:
                self.workers.discard(threading.current_thread())

    def _handle(self, request: dict[str, Any]) -> None:
        request_id = request["id"]
        try:
            params = request.get("params", {})
            if not isinstance(params, dict):
                self.protocol_error(request_id, -32602, "invalid params")
            else:
                try:
                    method = McpMethod(request["method"])
                except ValueError:
                    method = None
                handler = self._method_handlers().get(method)
                if handler is None:
                    self.protocol_error(request_id, -32601, "method not found")
                else:
                    handler(request_id, params)
        except Exception:
            self.protocol_error(request_id, -32603, "internal error")
        finally:
            with self.inflight_lock:
                self.inflight.discard(request_id)

    def _method_handlers(self) -> dict[McpMethod, Any]:
        return {
            McpMethod.INITIALIZE: self._initialize,
            McpMethod.PING: self._ping,
            McpMethod.LIST_TOOLS: self._list_tools,
            McpMethod.CALL_TOOL: self._tool_call,
        }

    def _initialize(self, request_id: Any, params: dict[str, Any]) -> None:
        requested = params.get("protocolVersion")
        version = (
            requested
            if isinstance(requested, str) and requested in SUPPORTED_PROTOCOL_VERSIONS
            else PREFERRED_PROTOCOL_VERSION
        )
        result = {
            "protocolVersion": version,
            "serverInfo": {"name": PRODUCT_NAME, "version": __version__},
            "capabilities": {"tools": {"listChanged": False}},
        }
        self.write({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _ping(self, request_id: Any, params: dict[str, Any]) -> None:
        self.write({"jsonrpc": "2.0", "id": request_id, "result": {}})

    def _list_tools(self, request_id: Any, params: dict[str, Any]) -> None:
        result = {"tools": [CAPABILITY_TOOL]}
        self.write({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _tool_audit_context(
        self, raw_arguments: Any
    ) -> tuple[RouterAction | None, str, str | None]:
        raw_action = raw_arguments.get("action") if isinstance(raw_arguments, dict) else None
        try:
            action = RouterAction(raw_action)
        except (TypeError, ValueError):
            action = None
        audit_action = action or AuditAction.INVALID
        raw_capability_id = (
            raw_arguments.get("capability_id") if isinstance(raw_arguments, dict) else None
        )
        capability_id = (
            self.executor.known_capability_id(raw_capability_id)
            if action in {
                RouterAction.DESCRIBE,
                RouterAction.LOAD_SKILL,
                RouterAction.CALL,
            }
            else None
        )
        return action, audit_action, capability_id

    def _write_tool_error(
        self,
        request_id: Any,
        error: RouterError,
        *,
        audit_action: str,
        capability_id: str | None,
        started: float,
    ) -> None:
        result = self._bounded_error_result(error.document())
        outcome = (
            AuditOutcome.DENIED
            if error.code == ErrorCode.POLICY_DENIED.value
            else AuditOutcome.ERROR
        )
        self._audit(
            audit_action,
            outcome,
            capability_id,
            error.code,
            len(canonical_bytes(result)),
            time.monotonic() - started,
        )
        self.write({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _write_tool_success(
        self,
        request_id: Any,
        document: dict[str, Any],
        *,
        action: RouterAction | None,
        audit_action: str,
        capability_id: str | None,
        started: float,
    ) -> None:
        downstream = document.get("downstream")
        downstream_error = isinstance(downstream, dict) and downstream.get("isError") is True
        result = self._bounded_result(document, is_error=downstream_error)
        candidates = None
        if action is RouterAction.SEARCH:
            candidates = [
                item["id"]
                for item in document.get("matches", [])
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            ]
        self._audit(
            audit_action,
            AuditOutcome.ERROR if result.get("isError") else AuditOutcome.ALLOWED,
            capability_id,
            output_bytes=len(canonical_bytes(result)),
            latency_seconds=time.monotonic() - started,
            search_candidates=candidates,
        )
        self.write({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _tool_call(self, request_id: Any, params: dict[str, Any]) -> None:
        if params.get("name") != CAPABILITY_TOOL["name"]:
            self.protocol_error(request_id, -32602, "unknown tool")
            return

        raw_arguments = params.get("arguments")
        action, audit_action, capability_id = self._tool_audit_context(raw_arguments)
        started = time.monotonic()
        try:
            document = self.executor.execute(raw_arguments)
        except Exception as caught:
            error = (
                caught
                if isinstance(caught, RouterError)
                else RouterError(ErrorCode.INTERNAL_ERROR, "request execution failed")
            )
            self._write_tool_error(
                request_id,
                error,
                audit_action=audit_action,
                capability_id=capability_id,
                started=started,
            )
            return
        self._write_tool_success(
            request_id,
            document,
            action=action,
            audit_action=audit_action,
            capability_id=capability_id,
            started=started,
        )

    def _audit(self, *args: Any, **kwargs: Any) -> None:
        try:
            self.executor.audit(*args, **kwargs)
        except (OSError, ValueError):
            self._warn_audit_failure()

    def _warn_audit_failure(self) -> None:
        with self.audit_warning_lock:
            if self.audit_warning_emitted:
                return
            self.audit_warning_emitted = True
        try:
            print(f"{PRODUCT_NAME}: audit log write failed", file=sys.stderr, flush=True)
        except (OSError, ValueError):
            pass

    def _bounded_result(
        self, document: dict[str, Any], *, is_error: bool = False
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "content": [{"type": "text", "text": canonical_bytes(document).decode("utf-8")}],
            "structuredContent": document,
        }
        if is_error:
            result["isError"] = True
        if len(canonical_bytes(result)) <= self.config.output_limit_bytes:
            return result
        try:
            metadata = self.executor.store_artifact(document)
        except OSError:
            return self._result_unavailable(is_error=is_error)
        artifact_path = metadata["artifact_path"]
        result = {
            "content": [
                {
                    "type": "text",
                    "text": f"Result stored as router artifact at {artifact_path}.",
                }
            ],
            "structuredContent": metadata,
        }
        if is_error:
            result["isError"] = True
        if len(canonical_bytes(result)) <= self.config.output_limit_bytes:
            return result
        metadata["preview"] = ""
        result["structuredContent"] = metadata
        if len(canonical_bytes(result)) > self.config.output_limit_bytes:
            return self._result_unavailable(is_error=is_error)
        return result

    def _result_unavailable(self, *, is_error: bool) -> dict[str, Any]:
        document = {
            "completed": True,
            "result_unavailable": True,
            "message": (
                "Capability completed, but its result could not be returned or stored. "
                "Do not retry automatically."
            ),
        }
        result: dict[str, Any] = {
            "content": [{"type": "text", "text": canonical_bytes(document).decode("utf-8")}],
            "structuredContent": document,
        }
        if is_error:
            result["isError"] = True
        return result

    def _bounded_error_result(self, document: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {
            "isError": True,
            "content": [{"type": "text", "text": canonical_bytes(document).decode("utf-8")}],
            "structuredContent": document,
        }
        if len(canonical_bytes(result)) <= self.config.output_limit_bytes:
            return result
        error = document.get("error", {})
        compact = {
            "error": {
                "code": error.get("code", ErrorCode.INTERNAL_ERROR.value),
                "message": "request failed",
                "details": {"truncated": True},
            }
        }
        return {
            "isError": True,
            "content": [{"type": "text", "text": canonical_bytes(compact).decode("utf-8")}],
            "structuredContent": compact,
        }


def serve(config: Config, *, run_id: str, audit_path: Path | None) -> int:
    return Server(config, run_id=run_id, audit_path=audit_path).serve()
