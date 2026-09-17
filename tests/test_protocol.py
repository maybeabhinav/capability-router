from __future__ import annotations

import json
import time

from capability_router.schema import validate
from tests.support import RouterTestCase


EXPECTED_TOOL = {
    "name": "capability",
    "description": "Search, inspect, load, or call an optional capability. Search for one need per call with limit 1 through 5; do not combine unrelated needs. Search returns metadata only. A skill is not loaded until load_skill succeeds. Describe an MCP tool before calling it. Context actions list, inspect, switch, explain, move, share, unassign, or undo session assignments.",
    "inputSchema": {
        "type": "object",
        "oneOf": [
            {
                "type": "object",
                "required": ["action", "query"],
                "properties": {
                    "action": {"const": "search"},
                    "kinds": {
                        "type": "array",
                        "items": {"enum": ["mcp_tool", "skill"]},
                        "minItems": 1,
                        "uniqueItems": True,
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 5,
                        "description": "Maximum matches. Use an integer from 1 through 5.",
                    },
                    "query": {"type": "string", "minLength": 1, "maxLength": 1024},
                },
                "additionalProperties": False,
            },
            {
                "type": "object",
                "required": ["action", "capability_id"],
                "properties": {
                    "action": {"const": "describe"},
                    "capability_id": {"type": "string", "minLength": 1},
                },
                "additionalProperties": False,
            },
            {
                "type": "object",
                "required": ["action", "capability_id"],
                "properties": {
                    "action": {"const": "load_skill"},
                    "capability_id": {"type": "string", "minLength": 1},
                },
                "additionalProperties": False,
            },
            {
                "type": "object",
                "required": ["action", "arguments", "capability_id"],
                "properties": {
                    "action": {"const": "call"},
                    "arguments": {"type": "object"},
                    "capability_id": {"type": "string", "minLength": 1},
                },
                "additionalProperties": False,
            },
            {
                "type": "object",
                "required": ["action"],
                "properties": {"action": {"const": "status"}},
                "additionalProperties": False,
            },
            {
                "type": "object",
                "required": ["action"],
                "properties": {"action": {"const": "context_list"}},
                "additionalProperties": False,
            },
            {
                "type": "object",
                "required": ["action"],
                "properties": {"action": {"const": "context_current"}},
                "additionalProperties": False,
            },
            {
                "type": "object",
                "required": ["action", "context"],
                "properties": {
                    "action": {"const": "context_use"},
                    "context": {"type": "string", "minLength": 1},
                },
                "additionalProperties": False,
            },
            {
                "type": "object",
                "required": ["action", "capability_id", "context"],
                "properties": {
                    "action": {"const": "context_explain"},
                    "capability_id": {"type": "string", "minLength": 1},
                    "context": {"type": "string", "minLength": 1},
                },
                "additionalProperties": False,
            },
            {
                "type": "object",
                "required": [
                    "action",
                    "capability_id",
                    "source_context",
                    "target_context",
                ],
                "properties": {
                    "action": {"const": "context_move"},
                    "capability_id": {"type": "string", "minLength": 1},
                    "expected_revision": {"type": "integer", "minimum": 0},
                    "source_context": {"type": "string", "minLength": 1},
                    "target_context": {"type": "string", "minLength": 1},
                },
                "additionalProperties": False,
            },
            {
                "type": "object",
                "required": ["action", "change_id"],
                "properties": {
                    "action": {"const": "context_undo"},
                    "change_id": {"type": "string", "minLength": 1},
                },
                "additionalProperties": False,
            },
            {
                "type": "object",
                "required": ["action", "capability_id", "target_context"],
                "properties": {
                    "action": {"const": "context_share"},
                    "capability_id": {"type": "string", "minLength": 1},
                    "expected_revision": {"type": "integer", "minimum": 0},
                    "target_context": {"type": "string", "minLength": 1},
                },
                "additionalProperties": False,
            },
            {
                "type": "object",
                "required": ["action", "capability_id", "context"],
                "properties": {
                    "action": {"const": "context_unassign"},
                    "capability_id": {"type": "string", "minLength": 1},
                    "context": {"type": "string", "minLength": 1},
                    "expected_revision": {"type": "integer", "minimum": 0},
                },
                "additionalProperties": False,
            },
        ]
    },
}


class ProtocolContractTests(RouterTestCase):
    def test_protocol_lists_only_capability(self) -> None:
        with self.ready_session() as session:
            response = session.request("tools/list", {})

        self.assertEqual(response["result"], {"tools": [EXPECTED_TOOL]})

    def test_advertised_schema_rejects_shapes_that_runtime_rejects(self) -> None:
        with self.ready_session() as session:
            response = session.request("tools/list", {})

        schema = response["result"]["tools"][0]["inputSchema"]
        invalid = (
            {"action": "search"},
            {"action": "search", "query": "message", "kinds": []},
            {"action": "status", "query": "extra"},
            {"action": "call", "capability_id": "fixture.search_messages"},
        )
        for arguments in invalid:
            with self.subTest(arguments=arguments):
                self.assertTrue(validate(schema, arguments), arguments)

    def test_protocol_rejects_invalid_action_fields(self) -> None:
        cases = [
            ({"action": "search"}, "search requires query"),
            ({"action": "status", "query": "extra"}, "status accepts no extra field"),
            ({"action": "describe", "capability_id": "fixture.search_messages", "limit": 1}, "describe rejects limit"),
            ({"action": "call", "capability_id": "fixture.search_messages"}, "call requires arguments"),
            ({"action": "not-an-action"}, "action enum is closed"),
            ({"action": []}, "action must be hashable text"),
            ({"action": "search", "query": "x", "kinds": [{}]}, "kinds must be text values"),
            ({"action": "search", "query": "x" * 1025}, "query length is bounded"),
        ]
        with self.ready_session() as session:
            for arguments, reason in cases:
                with self.subTest(reason=reason):
                    session.assert_tool_error(self, arguments, "invalid_input")

    def test_protocol_accepts_json_integer_numeric_form(self) -> None:
        with self.ready_session() as session:
            response, document = session.tool_document(
                {"action": "search", "query": "message", "limit": 1.0}
            )

        self.assertFalse(response["result"].get("isError", False), response)
        self.assertLessEqual(len(document["matches"]), 1)

    def test_unknown_tool_name_returns_a_protocol_error(self) -> None:
        with self.ready_session() as session:
            response = session.request(
                "tools/call",
                {"name": "missing", "arguments": {"action": "status"}},
            )

        self.assertEqual(response["error"]["code"], -32602, response)
        self.assertNotIn("result", response)

    def test_protocol_lifecycle_and_notifications(self) -> None:
        supported = ("2025-11-25", "2025-06-18", "2024-11-05")
        self.workspace.refresh()
        for version in (*supported, "1900-01-01"):
            with self.subTest(version=version), self.workspace.session() as session:
                initialized = session.initialize(version)
                expected = version if version in supported else "2025-11-25"
                self.assertEqual(initialized["result"]["protocolVersion"], expected)
                self.assertEqual(initialized["result"]["capabilities"], {"tools": {"listChanged": False}})
                self.assertEqual(set(initialized["result"]["serverInfo"]), {"name", "version"})

                session.notify_initialized()
                ping = session.request("ping")
                self.assertEqual(ping["result"], {})

                unknown = session.request("fixture/unknown")
                self.assertEqual(unknown["error"]["code"], -32601)

                session.send_raw(b"{not-json")
                malformed = session.receive()
                self.assertEqual(malformed["error"]["code"], -32700)
                self.assertIsNone(malformed.get("id"))

        with self.workspace.session() as session:
            session.initialize()
            session.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 99}})
            ping = session.request("ping")
            self.assertEqual(ping["result"], {})
            process = session.process
        assert process is not None
        self.assertEqual(process.returncode, 0)

    def test_protocol_rejects_duplicate_in_flight_ids(self) -> None:
        self.call_timeout = 0.35
        self.workspace.close()
        self.workspace = type(self.workspace)(timeout=self.call_timeout)
        self.workspace.refresh()
        with self.workspace.session() as session:
            session.initialize()
            session.notify_initialized()
            hanging = {
                "jsonrpc": "2.0",
                "id": 77,
                "method": "tools/call",
                "params": {
                    "name": "capability",
                    "arguments": {
                        "action": "call",
                        "capability_id": "fixture.hang_with_child",
                        "arguments": {},
                    },
                },
            }
            session.send(hanging)
            session.send({"jsonrpc": "2.0", "id": 77, "method": "ping"})
            first = session.receive(timeout=2)
            second = session.receive(timeout=2)

        responses = [first, second]
        protocol_errors = [item for item in responses if item.get("error", {}).get("code") == -32600]
        self.assertEqual(len(protocol_errors), 1, responses)
        self.assertEqual([item.get("id") for item in responses], [77, 77])

    def test_protocol_rejects_composite_ids_and_stays_alive(self) -> None:
        self.workspace.refresh()
        with self.workspace.session() as session:
            session.initialize()
            for invalid_id in ([], {"nested": "id"}):
                with self.subTest(invalid_id=invalid_id):
                    session.send({"jsonrpc": "2.0", "id": invalid_id, "method": "ping"})
                    invalid = session.receive()
                    self.assertEqual(invalid.get("id"), None, invalid)
                    self.assertEqual(invalid.get("error", {}).get("code"), -32600, invalid)
                    ping = session.request("ping")
                    self.assertEqual(ping.get("result"), {}, ping)

    def test_invalid_request_shape_sanitizes_a_composite_id(self) -> None:
        self.workspace.refresh()
        with self.workspace.session() as session:
            session.initialize()
            for request_id in ([], {"nested": "id"}):
                with self.subTest(request_id=request_id):
                    session.send(
                        {"jsonrpc": "2.0", "id": request_id, "method": 0}
                    )
                    invalid = session.receive()
                    self.assertEqual(invalid.get("id"), None, invalid)
                    self.assertEqual(invalid.get("error", {}).get("code"), -32600, invalid)

    def test_protocol_rejects_null_and_noninteger_request_ids(self) -> None:
        self.workspace.refresh()
        with self.workspace.session() as session:
            session.initialize()
            for request_id in (None, 1.0, 1.25):
                with self.subTest(request_id=request_id):
                    session.send(
                        {"jsonrpc": "2.0", "id": request_id, "method": "ping"}
                    )
                    invalid = session.receive()
                    self.assertEqual(invalid.get("id"), None, invalid)
                    self.assertEqual(invalid.get("error", {}).get("code"), -32600, invalid)
                    ping = session.request("ping")
                    self.assertEqual(ping.get("result"), {}, ping)

    def test_protocol_rejects_surrogate_ids_and_stays_alive(self) -> None:
        self.workspace.refresh()
        with self.workspace.session() as session:
            session.send_raw(b'{"jsonrpc":"2.0","id":"\\ud800","method":0}')
            invalid = session.receive()
            self.assertEqual(invalid.get("error", {}).get("code"), -32700, invalid)
            ping = session.request("ping")
            self.assertEqual(ping.get("result"), {}, ping)

    def test_protocol_bounds_concurrent_requests(self) -> None:
        self.workspace.close()
        self.workspace = type(self.workspace)(timeout=0.35)
        self.workspace.refresh()
        with self.workspace.session() as session:
            session.initialize()
            for request_id in range(100, 109):
                session.send(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": "tools/call",
                        "params": {
                            "name": "capability",
                            "arguments": {
                                "action": "call",
                                "capability_id": "fixture.slow_lookup",
                                "arguments": {},
                            },
                        },
                    }
                )
            responses = [session.receive(timeout=3) for _ in range(9)]

        busy = [item for item in responses if item.get("error", {}).get("message") == "server busy"]
        self.assertEqual(len(busy), 1, responses)

    def test_router_stops_before_new_calls_after_output_breaks(self) -> None:
        config = self.workspace.config()
        config["mode"] = "unrestricted"
        self.workspace.config_path.write_text(json.dumps(config), encoding="utf-8")
        self.workspace.refresh()
        self.workspace.clear_fixture_observation()
        with self.workspace.session() as session:
            session.initialize()
            assert session.process is not None and session.process.stdout is not None
            session.process.stdout.close()
            session.send({"jsonrpc": "2.0", "id": 70, "method": "ping"})
            deadline = time.monotonic() + 2
            while session.process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            if session.process.poll() is None:
                session.send(
                    {
                        "jsonrpc": "2.0",
                        "id": 71,
                        "method": "tools/call",
                        "params": {
                            "name": "capability",
                            "arguments": {
                                "action": "call",
                                "capability_id": "fixture.send_message",
                                "arguments": {"channel": "x", "text": "must-not-run"},
                            },
                        },
                    }
                )
                time.sleep(0.2)

            self.assertIsNotNone(session.process.poll(), "router stayed alive after losing stdout")
            calls_path = self.workspace.state / "calls.jsonl"
            calls = [] if not calls_path.exists() else calls_path.read_text(encoding="utf-8")
            self.assertNotIn("send_message", calls)


if __name__ == "__main__":
    import unittest

    unittest.main()
