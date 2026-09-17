#!/usr/bin/env python3
"""Deterministic newline-delimited MCP server used only by contract tests."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any


STATE_DIR = Path(os.environ.get("FIXTURE_STATE_DIR", "."))
STATE_DIR.mkdir(parents=True, exist_ok=True)
SERVER_NAME = os.environ.get("FIXTURE_SERVER_NAME", "fixture")
MAX_STATE_NAME_BYTES = 200


def state_path(suffix: str) -> Path:
    filename = f"{SERVER_NAME}.{suffix}"
    if len(os.fsencode(filename)) > MAX_STATE_NAME_BYTES:
        digest = hashlib.sha256(os.fsencode(SERVER_NAME)).hexdigest()
        filename = f"server-{digest}.{suffix}"
    return STATE_DIR / filename


def append(name: str, value: dict[str, Any]) -> None:
    with (STATE_DIR / name).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


def schema_mode() -> str:
    path = state_path("schema-mode")
    return path.read_text(encoding="utf-8").strip() if path.exists() else "normal"


def mode() -> str:
    path = state_path("mode")
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return os.environ.get("FIXTURE_MODE", "normal")


def search_schema() -> dict[str, Any]:
    maximum = 51 if schema_mode() == "changed" else 50
    return {
        "type": "object",
        "required": ["query"],
        "properties": {"query": {"type": "string", "minLength": 1, "maxLength": maximum}},
        "additionalProperties": False,
    }


def branching_schema(levels: int = 24) -> dict[str, Any]:
    definitions: dict[str, Any] = {
        "level0": {
            "type": "object",
            "required": ["value"],
            "properties": {"value": {"type": "string"}},
            "additionalProperties": False,
        }
    }
    for level in range(1, levels + 1):
        reference = {"$ref": f"#/$defs/level{level - 1}"}
        definitions[f"level{level}"] = {"allOf": [reference, dict(reference)]}
    return {"$defs": definitions, "$ref": f"#/$defs/level{levels}"}


def budget_one_of_schema() -> dict[str, Any]:
    return {
        "oneOf": [
            {"type": "object"},
            {
                "type": "object",
                "required": ["values"],
                "properties": {
                    "values": {"type": "array", "items": {"type": "integer"}}
                },
                "additionalProperties": False,
            },
        ]
    }


def long_reference_schema(levels: int = 300) -> dict[str, Any]:
    definitions: dict[str, Any] = {"level0": {"type": "string"}}
    for level in range(1, levels + 1):
        definitions[f"level{level}"] = {"$ref": f"#/$defs/level{level - 1}"}
    return {"$defs": definitions, "$ref": f"#/$defs/level{levels}"}


def mixed_reference_schema(levels: int = 60, nesting: int = 8) -> dict[str, Any]:
    definitions: dict[str, Any] = {}
    for level in range(levels):
        child: dict[str, Any] = (
            {"$ref": f"#/$defs/level{level + 1}"}
            if level + 1 < levels
            else {"type": "string"}
        )
        for _ in range(nesting):
            child = {"type": "object", "properties": {"value": child}}
        definitions[f"level{level}"] = child
    return {"$defs": definitions, "$ref": "#/$defs/level0"}


def ordered_reference_schema(levels: int = 1500) -> dict[str, Any]:
    definitions: dict[str, Any] = {"level0000": {"type": "string"}}
    for level in range(1, levels + 1):
        definitions[f"level{level:04d}"] = {
            "$ref": f"#/$defs/level{level - 1:04d}"
        }
    return {
        "type": "object",
        "$defs": definitions,
        "properties": {"value": {"$ref": f"#/$defs/level{levels:04d}"}},
    }


def tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "search_messages",
            "description": "Search messages without creating or changing them.",
            "inputSchema": search_schema(),
        },
        {
            "name": "send_message",
            "description": "Send a message and change external state.",
            "inputSchema": {
                "type": "object",
                "required": ["channel", "text"],
                "properties": {
                    "channel": {"type": "string", "minLength": 1},
                    "text": {"type": "string", "minLength": 1},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "paginated_lookup",
            "description": "Read one fixture record from the second discovery page.",
            "inputSchema": {
                "type": "object",
                "required": ["id"],
                "properties": {"id": {"type": "integer", "minimum": 1}},
                "additionalProperties": False,
            },
        },
        {
            "name": "validate_payload",
            "description": "Validate every supported schema value without changing state.",
            "inputSchema": {
                "type": "object",
                "required": ["name", "count", "ratio", "active", "nothing", "tags", "kind"],
                "properties": {
                    "name": {"type": "string", "minLength": 2, "maxLength": 8, "pattern": "^[a-z]+$"},
                    "count": {"type": "integer", "minimum": 1, "maximum": 3},
                    "ratio": {"type": "number", "minimum": 0, "maximum": 1},
                    "active": {"type": "boolean"},
                    "nothing": {"type": "null"},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["red", "blue"]},
                        "minItems": 1,
                        "maxItems": 2,
                        "uniqueItems": True,
                    },
                    "kind": {"const": "fixture"},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "json_number_semantics",
            "description": "Validate JSON numeric equality and integer semantics.",
            "inputSchema": {
                "type": "object",
                "required": ["integer", "choice", "fixed", "values"],
                "properties": {
                    "integer": {"type": "integer"},
                    "choice": {"enum": [1]},
                    "fixed": {"const": 1},
                    "large_choice": {"enum": [1000000000000000000000000000000]},
                    "large_fixed": {"const": 1000000000000000000000000000000},
                    "values": {
                        "type": "array",
                        "items": {"type": "number"},
                        "uniqueItems": True,
                    },
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "branching_schema",
            "description": "Validate a schema with repeated references within a fixed work budget.",
            "inputSchema": branching_schema(),
        },
        {
            "name": "budget_one_of",
            "description": "Reject overlapping oneOf branches even when validation reaches its work budget.",
            "inputSchema": budget_one_of_schema(),
        },
        {
            "name": "large_enum",
            "description": "Reject a large non-enum value within the validation deadline.",
            "inputSchema": {
                "type": "object",
                "required": ["value"],
                "properties": {"value": {"enum": list(range(1000))}},
                "additionalProperties": False,
            },
        },
        {
            "name": "long_reference_schema",
            "description": "Expose a reference chain beyond the router recursion limit.",
            "inputSchema": long_reference_schema(),
        },
        {
            "name": "malformed_null_type",
            "description": "Expose an explicit null type keyword.",
            "inputSchema": {
                "type": "object",
                "properties": {"value": {"type": None}},
                "additionalProperties": False,
            },
        },
        {
            "name": "mixed_reference_schema",
            "description": "Expose mixed nesting and references beyond the traversal limit.",
            "inputSchema": mixed_reference_schema(),
        },
        {
            "name": "ordered_reference_schema",
            "description": "Expose an ordered reference chain beyond the traversal limit.",
            "inputSchema": ordered_reference_schema(),
        },
        {
            "name": "draft_seven_schema",
            "description": "Expose a schema dialect that the router does not implement.",
            "inputSchema": {
                "$schema": "http://json-schema.org/draft-07/schema#",
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "value": {"$ref": "#/properties/source", "type": "integer"},
                },
            },
        },
        {
            "name": "wildcard_pattern",
            "description": "Validate one ECMAScript wildcard character.",
            "inputSchema": {
                "type": "object",
                "required": ["value"],
                "properties": {"value": {"type": "string", "pattern": "^.$"}},
                "additionalProperties": False,
            },
        },
        {
            "name": "encoded_reference",
            "description": "Resolve a percent-encoded JSON Pointer fragment.",
            "inputSchema": {
                "type": "object",
                "$defs": {
                    "a b": {"type": "integer"},
                    "a%20b": {"type": "string"},
                },
                "required": ["value"],
                "properties": {"value": {"$ref": "#/$defs/a%20b"}},
                "additionalProperties": False,
            },
        },
        {
            "name": "malformed_annotations",
            "description": "Expose annotation keywords with invalid types.",
            "inputSchema": {"type": "object", "title": [], "description": 9},
        },
        {
            "name": "floating_limits",
            "description": "Accept integral JSON numbers for integer-valued limits.",
            "inputSchema": {
                "type": "object",
                "required": ["value"],
                "properties": {"value": {"type": "string", "minLength": 1.0}},
                "additionalProperties": False,
            },
        },
        {
            "name": "malformed_character_class",
            "description": "Expose a character class outside the shared regex subset.",
            "inputSchema": {
                "type": "object",
                "properties": {"value": {"type": "string", "pattern": "^[]a]$"}},
            },
        },
        {
            "name": "nullable_lookup",
            "description": "Read a fixture record with a nullable cursor.",
            "inputSchema": {
                "type": "object",
                "required": ["query", "cursor"],
                "properties": {
                    "query": {"type": "string", "minLength": 1},
                    "cursor": {"type": ["string", "null"]},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "structured_lookup",
            "description": "Read a fixture record through composed standard JSON Schema.",
            "inputSchema": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$defs": {
                    "identifier": {
                        "allOf": [
                            {"type": "string"},
                            {"pattern": "^[a-z]+$", "minLength": 2},
                        ]
                    }
                },
                "type": "object",
                "required": ["target", "filters", "page", "strict"],
                "properties": {
                    "target": {"$ref": "#/$defs/identifier"},
                    "filters": {
                        "type": "object",
                        "propertyNames": {"pattern": "^[a-z]+$"},
                        "additionalProperties": {
                            "anyOf": [{"type": "string"}, {"type": "null"}]
                        },
                    },
                    "page": {
                        "anyOf": [
                            {"type": "integer", "exclusiveMinimum": 0},
                            {"type": "null"},
                        ]
                    },
                    "strict": {"oneOf": [{"type": "integer"}, {"type": "number"}]},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "large_result",
            "description": "Read a deterministic result larger than the router output limit.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "large_write",
            "description": "Record a write and return a deterministic large result.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "task_required",
            "description": "Require task-augmented execution.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            "execution": {"taskSupport": "required"},
        },
        {
            "name": "near_boundary_result",
            "description": "Read a caller-sized result near the router output boundary.",
            "inputSchema": {
                "type": "object",
                "required": ["size"],
                "properties": {"size": {"type": "integer", "minimum": 1, "maximum": 2000}},
                "additionalProperties": False,
            },
        },
        {
            "name": "fail_once",
            "description": "Record one read attempt and return a deterministic tool failure.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "sized_error",
            "description": "Return an error at the router output boundary.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "hang_with_child",
            "description": "Start a grandchild and wait so timeout cleanup can be measured.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "slow_lookup",
            "description": "Wait so router request concurrency can be measured.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "backpressure",
            "description": "Accept a large payload after the server stops reading.",
            "inputSchema": {
                "type": "object",
                "required": ["text"],
                "properties": {"text": {"type": "string", "maxLength": 900000}},
                "additionalProperties": False,
            },
        },
        {
            "name": "oversized_frame",
            "description": "Return a response larger than the downstream frame limit.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "hang_with_stubborn_child",
            "description": "Start a SIGTERM-ignoring grandchild and wait for owned-group cleanup.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "inspect_transport",
            "description": "Record process arguments to prove tool arguments arrived on stdin.",
            "inputSchema": {
                "type": "object",
                "required": ["credential"],
                "properties": {"credential": {"type": "string", "minLength": 1}},
                "additionalProperties": False,
            },
        },
        {
            "name": "unsupported_schema",
            "description": "Expose an unsupported composition for refresh validation.",
            "inputSchema": {"not": {"type": "string"}},
        },
        {
            "name": "unicode_reference_schema",
            "description": "Expose a malformed Unicode array reference.",
            "inputSchema": {
                "type": "object",
                "anyOf": [{}],
                "$ref": "#/anyOf/²",
            },
        },
        {
            "name": "unsafe_pattern",
            "description": "Expose a backtracking pattern that the router must reject.",
            "inputSchema": {
                "type": "object",
                "required": ["value"],
                "properties": {"value": {"type": "string", "pattern": "^(a+)+$"}},
                "additionalProperties": False,
            },
        },
        {
            "name": "unanchored_pattern",
            "description": "Expose an unanchored pattern with excessive search cost.",
            "inputSchema": {
                "type": "object",
                "required": ["value"],
                "properties": {"value": {"type": "string", "pattern": "[a-z]+$"}},
                "additionalProperties": False,
            },
        },
        {
            "name": "malformed_required",
            "description": "Expose a required keyword with the wrong JSON type.",
            "inputSchema": {
                "type": "object",
                "required": "name",
                "properties": {"name": {"type": "string"}},
                "additionalProperties": False,
            },
        },
        {
            "name": "malformed_min_length",
            "description": "Expose a minLength keyword with the wrong JSON type.",
            "inputSchema": {
                "type": "object",
                "required": ["value"],
                "properties": {"value": {"type": "string", "minLength": "2"}},
                "additionalProperties": False,
            },
        },
        {
            "name": "malformed_unique_items",
            "description": "Expose a uniqueItems keyword with the wrong JSON type.",
            "inputSchema": {
                "type": "object",
                "required": ["values"],
                "properties": {
                    "values": {"type": "array", "items": {"type": "string"}, "uniqueItems": "yes"}
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "malformed_pattern",
            "description": "Expose a pattern keyword with the wrong JSON type.",
            "inputSchema": {
                "type": "object",
                "required": ["value"],
                "properties": {"value": {"type": "string", "pattern": 7}},
                "additionalProperties": False,
            },
        },
    ]


def write_frame(value: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def result(request_id: Any, value: dict[str, Any]) -> None:
    if mode() == "notify-before-response":
        write_frame(
            {
                "jsonrpc": "2.0",
                "method": "notifications/progress",
                "params": {"progressToken": "fixture", "progress": 1},
            }
        )
    if mode() == "ping-before-response":
        write_frame({"jsonrpc": "2.0", "id": 900, "method": "ping"})
        raw_response = sys.stdin.buffer.readline()
        try:
            ping_response = json.loads(raw_response)
        except (UnicodeError, json.JSONDecodeError):
            ping_response = {"invalid": raw_response.decode("utf-8", errors="replace")}
        append("ping-responses.jsonl", ping_response)
    version = "WRONG" if mode() == "wrong-jsonrpc" else "2.0"
    response_id = True if mode() == "boolean-response-id" and request_id == 1 else request_id
    write_frame({"jsonrpc": version, "id": response_id, "result": value})


def error(request_id: Any, code: int, message: str) -> None:
    write_frame({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def call_tool(request_id: Any, params: dict[str, Any]) -> None:
    name = params.get("name")
    arguments = params.get("arguments", {})
    logged_arguments = {"credential": "[REDACTED]"} if name == "inspect_transport" else arguments
    append("calls.jsonl", {"server": SERVER_NAME, "tool": name, "arguments": logged_arguments})

    if name == "search_messages":
        payload: dict[str, Any] = {
            "content": [{"type": "text", "text": f"matches:{arguments['query']}"}]
        }
        if mode() == "malformed-call-result":
            payload["isError"] = "true"
        result(request_id, payload)
    elif name == "send_message":
        result(request_id, {"content": [{"type": "text", "text": "sent"}]})
    elif name == "paginated_lookup":
        result(request_id, {"content": [{"type": "text", "text": f"record:{arguments['id']}"}]})
    elif name == "validate_payload":
        result(request_id, {"content": [{"type": "text", "text": "valid"}]})
    elif name == "json_number_semantics":
        result(request_id, {"content": [{"type": "text", "text": "numeric semantics valid"}]})
    elif name == "branching_schema":
        result(request_id, {"content": [{"type": "text", "text": "branching schema valid"}]})
    elif name in {"budget_one_of", "large_enum"}:
        result(request_id, {"content": [{"type": "text", "text": "unexpected validation pass"}]})
    elif name == "nullable_lookup":
        result(
            request_id,
            {"content": [{"type": "text", "text": f"nullable:{arguments['query']}:{arguments['cursor']}"}]},
        )
    elif name == "structured_lookup":
        result(request_id, {"content": [{"type": "text", "text": "structured"}]})
    elif name == "wildcard_pattern":
        result(request_id, {"content": [{"type": "text", "text": "wildcard valid"}]})
    elif name in {"encoded_reference", "floating_limits"}:
        result(request_id, {"content": [{"type": "text", "text": "schema valid"}]})
    elif name in {"large_result", "large_write"}:
        payload = "FULL-PAYLOAD-BEGIN:" + ("λ" * 16000) + ":FULL-PAYLOAD-END"
        result(request_id, {"content": [{"type": "text", "text": payload}]})
    elif name == "near_boundary_result":
        result(request_id, {"content": [{"type": "text", "text": "N" * arguments["size"]}]})
    elif name == "fail_once":
        result(request_id, {"isError": True, "content": [{"type": "text", "text": "fixture failure"}]})
    elif name == "sized_error":
        result(request_id, {"isError": True, "content": [{"type": "text", "text": "E" * 104}]})
    elif name == "hang_with_child":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        (STATE_DIR / "hanging-pids.json").write_text(
            json.dumps({"server": os.getpid(), "grandchild": child.pid}), encoding="utf-8"
        )
        time.sleep(60)
    elif name == "slow_lookup":
        time.sleep(60)
    elif name == "backpressure":
        result(request_id, {"content": [{"type": "text", "text": "unexpected read"}]})
    elif name == "oversized_frame":
        response = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"content": [{"type": "text", "text": "X" * (4 * 1024 * 1024)}]},
        }
        write_frame(response)
    elif name == "hang_with_stubborn_child":
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)",
            ]
        )
        (STATE_DIR / "stubborn-pids.json").write_text(
            json.dumps({"server": os.getpid(), "grandchild": child.pid}), encoding="utf-8"
        )
        time.sleep(60)
    elif name == "inspect_transport":
        (STATE_DIR / "process.json").write_text(
            json.dumps({"argv": sys.argv, "pid": os.getpid()}, sort_keys=True), encoding="utf-8"
        )
        result(request_id, {"content": [{"type": "text", "text": "transport inspected"}]})
    elif name.startswith("malformed_"):
        result(request_id, {"content": [{"type": "text", "text": "malformed schema was invoked"}]})
    else:
        error(request_id, -32602, "unknown fixture tool")


def main() -> int:
    append(
        "starts.jsonl",
        {
            "server": SERVER_NAME,
            "pid": os.getpid(),
            "argv": sys.argv,
            "secret_present": bool(os.environ.get("FIXTURE_SECRET")),
        },
    )
    if os.environ.get("FIXTURE_SECRET"):
        print("fixture diagnostic " + os.environ["FIXTURE_SECRET"] * 1000, file=sys.stderr, flush=True)

    for raw_line in sys.stdin.buffer:
        try:
            request = json.loads(raw_line)
        except json.JSONDecodeError:
            error(None, -32700, "parse error")
            continue
        if "id" not in request:
            if mode() == "null-notification-response" and request.get("method") == "notifications/initialized":
                result(None, {})
            continue
        request_id = request["id"]
        method = request.get("method")
        if mode() == "hang-initialize" and method == "initialize":
            time.sleep(60)
        if mode() == "exit-before-response-with-child" and method == "initialize":
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            (STATE_DIR / "exited-parent-pids.json").write_text(
                json.dumps({"server": os.getpid(), "grandchild": child.pid}), encoding="utf-8"
            )
            return 0
        if mode() == "exit-with-detached-pipe-child" and method == "initialize":
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(2)"],
                start_new_session=True,
            )
            (STATE_DIR / "detached-pipe-pids.json").write_text(
                json.dumps({"server": os.getpid(), "grandchild": child.pid}), encoding="utf-8"
            )
            return 0
        if mode() == "fail" and method in {"initialize", "tools/list"}:
            return 23
        if method == "initialize":
            result(
                request_id,
                {
                    "protocolVersion": (
                        []
                        if mode() == "unsupported-version-list"
                        else "2099-01-01"
                        if mode() == "unsupported-version"
                        else "2025-11-25"
                    ),
                    "serverInfo": {"name": "fixture", "version": "1"},
                    "capabilities": {"tools": {"listChanged": False}},
                },
            )
        elif method == "ping":
            result(request_id, {})
        elif method == "tools/list":
            cursor = request.get("params", {}).get("cursor")
            all_tools = tools()
            if mode() == "task-required":
                for tool in all_tools:
                    if tool.get("name") == "search_messages":
                        tool["execution"] = {"taskSupport": "required"}
            if cursor is None:
                page: dict[str, Any] = {"tools": all_tools[:2], "nextCursor": "page-2"}
            elif cursor == "page-2":
                page = {"tools": all_tools[2:]}
                if mode() == "cursor-loop":
                    page["nextCursor"] = "page-2"
            else:
                error(request_id, -32602, "unknown cursor")
                continue
            result(request_id, page)
            if mode() == "stop-after-list" and cursor == "page-2":
                time.sleep(60)
        elif method == "tools/call":
            call_tool(request_id, request.get("params", {}))
        else:
            error(request_id, -32601, "method not found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
