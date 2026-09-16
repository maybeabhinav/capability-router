from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any
from unittest import mock

from capability_router import cli
from capability_router.catalog import search
from tests.support import (
    CLI,
    FIXTURE_SERVER,
    PROJECT_ROOT,
    McpSession,
    RouterTestCase,
    Workspace,
    canonical_bytes,
    first_nested,
    nested_values,
    process_exists,
    read_json_lines,
    wait_process_gone,
)


def matches(document: dict[str, Any]) -> list[dict[str, Any]]:
    value = first_nested(document, "matches")
    if not isinstance(value, list):
        raise AssertionError(f"matches is not a list: {document!r}")
    return value


def find_match(document: dict[str, Any], suffix: str) -> dict[str, Any]:
    for match in matches(document):
        if match["id"].endswith(suffix):
            return match
    raise AssertionError(f"no match ends with {suffix!r}: {document!r}")


class SearchAndSkillTests(RouterTestCase):
    def test_search_matches_a_skill_body_term_after_refresh(self) -> None:
        skill_dir = self.workspace.root / "skills" / "operations"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: operations\ndescription: Runtime helpers.\n---\n"
            "Use this skill to process queued work.\n",
            encoding="utf-8",
        )

        with self.ready_session() as session:
            _, document = session.tool_document(
                {"action": "search", "query": "process", "kinds": ["skill"]}
            )

        self.assertEqual(find_match(document, ".operations")["id"], "fixture.operations")

    def test_nested_skill_metadata_does_not_replace_top_level_fields(self) -> None:
        skill_dir = self.workspace.root / "skills" / "reading-room"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "name: reading-room\n"
            "description: Read books together.\n"
            "metadata:\n"
            "  name: vendor-package\n"
            "  description: Packaging metadata.\n"
            "---\n"
            "Use this skill for shared reading.\n",
            encoding="utf-8",
        )
        with self.ready_session() as session:
            _, document = session.tool_document(
                {"action": "search", "query": "read books together", "kinds": ["skill"]}
            )

        match = find_match(document, ".reading-room")
        self.assertEqual(match["description"], "Read books together.")

    def test_skill_frontmatter_supports_crlf_folded_descriptions(self) -> None:
        skill_dir = self.workspace.root / "skills" / "reading-room"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_bytes(
            b"---\r\n"
            b"name: reading-room\r\n"
            b"description: >-\r\n"
            b"  Explore books together with shared reading notes.\r\n"
            b"---\r\n"
            b"Use this skill for collaborative reading.\r\n"
        )
        with self.ready_session() as session:
            _, document = session.tool_document(
                {"action": "search", "query": "shared reading notes", "kinds": ["skill"]}
            )

        match = find_match(document, ".reading-room")
        self.assertEqual(
            match["description"], "Explore books together with shared reading notes."
        )

    def test_search_prioritizes_task_skill_names_over_incidental_body_terms(self) -> None:
        catalog = {
            "capabilities": [
                {
                    "id": "agents.design",
                    "kind": "skill",
                    "name": "design",
                    "description": "Choose architecture and interfaces.",
                    "source": "agents",
                    "access": "read",
                    "availability": "available",
                    "search_terms": [],
                },
                {
                    "id": "agents.noisy",
                    "kind": "skill",
                    "name": "noisy",
                    "description": "A large unrelated skill.",
                    "source": "agents",
                    "access": "read",
                    "availability": "available",
                    "search_terms": ["designing", "python", "cli", "architecture"],
                },
            ]
        }

        ranked = search(catalog, "designing a Python CLI architecture", ["skill"], 2)

        self.assertEqual("agents.design", ranked[0]["id"])

    def test_search_returns_ranked_bounded_matches(self) -> None:
        with self.ready_session() as session:
            _, document = session.tool_document({"action": "search", "query": "search messages", "limit": 3})
            _, no_match = session.tool_document({"action": "search", "query": "quartz-no-such-capability"})

        ranked = matches(document)
        self.assertGreaterEqual(len(ranked), 1)
        self.assertLessEqual(len(ranked), 3)
        self.assertTrue(ranked[0]["id"].endswith(".search_messages"), ranked)
        for item in ranked:
            self.assertTrue(
                {"id", "kind", "description", "source", "access", "score", "availability"}.issubset(item),
                item,
            )
            self.assertIsInstance(item["score"], (int, float))
        self.assertIn("no_match", canonical_bytes(no_match).decode("utf-8"))

    def test_search_keeps_risk_metadata_for_similar_tools(self) -> None:
        with self.ready_session() as session:
            _, document = session.tool_document({"action": "search", "query": "message", "limit": 5})

        read_match = find_match(document, ".search_messages")
        write_match = find_match(document, ".send_message")
        self.assertEqual(read_match["access"], "read")
        self.assertEqual(write_match["access"], "external_write")

    def test_load_skill_returns_only_selected_body(self) -> None:
        with self.ready_session() as session:
            _, search = session.tool_document(
                {"action": "search", "query": "cobalt archive lookup", "kinds": ["skill"], "limit": 5}
            )
            skill = find_match(search, ".message-reader")
            _, loaded = session.tool_document({"action": "load_skill", "capability_id": skill["id"]})

        encoded = canonical_bytes(loaded).decode("utf-8")
        self.assertIn("cobalt archive lookup", encoded)
        self.assertNotIn("UNRELATED-SKILL-BODY-MUST-NOT-LEAK", encoded)
        self.assertIn(True, nested_values(loaded, "untrusted"), loaded)

    def test_load_skill_rejects_escape_or_change(self) -> None:
        self.workspace.refresh()
        with self.workspace.session() as session:
            session.initialize()
            _, search = session.tool_document({"action": "search", "query": "cobalt archive lookup", "kinds": ["skill"]})
            skill_id = find_match(search, ".message-reader")["id"]

        skill_path = self.workspace.root / "skills" / "message-reader" / "SKILL.md"
        skill_path.write_text(skill_path.read_text(encoding="utf-8") + "\nchanged\n", encoding="utf-8")
        with self.workspace.session() as session:
            session.initialize()
            session.assert_tool_error(self, {"action": "load_skill", "capability_id": skill_id}, "refresh_required")

        self.workspace.refresh()
        outside = self.workspace.root / "outside-skill.md"
        outside.write_text("escaped instruction", encoding="utf-8")
        skill_path.unlink()
        skill_path.symlink_to(outside)
        with self.workspace.session() as session:
            session.initialize()
            session.assert_tool_error(self, {"action": "load_skill", "capability_id": skill_id}, "refresh_required")


class ValidationAndPolicyTests(RouterTestCase):
    def test_call_rejects_bad_arguments_before_spawn(self) -> None:
        self.workspace.refresh()
        self.workspace.clear_fixture_observation()
        invalid_arguments = [
            {},
            {"query": ""},
            {"query": 7},
            {"query": "valid", "unexpected": True},
            {"query": "x" * 51},
        ]
        with self.workspace.session() as session:
            session.initialize()
            for arguments in invalid_arguments:
                with self.subTest(arguments=arguments):
                    session.assert_tool_error(
                        self,
                        {
                            "action": "call",
                            "capability_id": "fixture.search_messages",
                            "arguments": arguments,
                        },
                        "schema_validation_failed",
                    )

        self.assertEqual(read_json_lines(self.workspace.state / "starts.jsonl"), [])
        self.assertEqual(read_json_lines(self.workspace.state / "calls.jsonl"), [])

    def test_supported_schema_subset_accepts_and_rejects_at_boundary(self) -> None:
        self.workspace.refresh()
        valid = {
            "name": "alpha",
            "count": 2,
            "ratio": 0.5,
            "active": True,
            "nothing": None,
            "tags": ["red", "blue"],
            "kind": "fixture",
        }
        invalid_values = [
            {**valid, "name": "A"},
            {**valid, "count": True},
            {**valid, "count": 4},
            {**valid, "ratio": 1.1},
            {**valid, "active": 1},
            {**valid, "nothing": "null"},
            {**valid, "tags": []},
            {**valid, "tags": ["red", "red"]},
            {**valid, "kind": "other"},
        ]
        self.workspace.clear_fixture_observation()
        with self.workspace.session() as session:
            session.initialize()
            for arguments in invalid_values:
                with self.subTest(arguments=arguments):
                    session.assert_tool_error(
                        self,
                        {"action": "call", "capability_id": "fixture.validate_payload", "arguments": arguments},
                        "schema_validation_failed",
                    )
            response = session.call(
                {"action": "call", "capability_id": "fixture.validate_payload", "arguments": valid}
            )
            self.assertFalse(response["result"].get("isError", False), response)

        calls = read_json_lines(self.workspace.state / "calls.jsonl")
        self.assertEqual(calls, [{"arguments": valid, "server": "fixture", "tool": "validate_payload"}])

    def test_pattern_rejects_a_trailing_newline_before_spawn(self) -> None:
        self.workspace.refresh()
        self.workspace.clear_fixture_observation()
        arguments = {
            "name": "abc\n",
            "count": 2,
            "ratio": 0.5,
            "active": True,
            "nothing": None,
            "tags": ["red"],
            "kind": "fixture",
        }
        with self.workspace.session() as session:
            session.initialize()
            session.assert_tool_error(
                self,
                {
                    "action": "call",
                    "capability_id": "fixture.validate_payload",
                    "arguments": arguments,
                },
                "schema_validation_failed",
            )

        self.assertEqual(read_json_lines(self.workspace.state / "starts.jsonl"), [])

    def test_wildcard_pattern_uses_ecmascript_line_terminators(self) -> None:
        self.workspace.refresh()
        self.workspace.clear_fixture_observation()
        with self.workspace.session() as session:
            session.initialize()
            for value in ("\r", "\u2028", "\u2029"):
                with self.subTest(value=repr(value)):
                    session.assert_tool_error(
                        self,
                        {
                            "action": "call",
                            "capability_id": "fixture.wildcard_pattern",
                            "arguments": {"value": value},
                        },
                        "schema_validation_failed",
                    )
            response = session.call(
                {
                    "action": "call",
                    "capability_id": "fixture.wildcard_pattern",
                    "arguments": {"value": "a"},
                }
            )

        self.assertFalse(response["result"].get("isError", False), response)
        self.assertEqual(
            read_json_lines(self.workspace.state / "calls.jsonl"),
            [
                {
                    "arguments": {"value": "a"},
                    "server": "fixture",
                    "tool": "wildcard_pattern",
                }
            ],
        )

    def test_percent_encoded_reference_selects_the_decoded_key(self) -> None:
        self.workspace.refresh()
        self.workspace.clear_fixture_observation()
        with self.workspace.session() as session:
            session.initialize()
            session.assert_tool_error(
                self,
                {
                    "action": "call",
                    "capability_id": "fixture.encoded_reference",
                    "arguments": {"value": "wrong-target"},
                },
                "schema_validation_failed",
            )
            response = session.call(
                {
                    "action": "call",
                    "capability_id": "fixture.encoded_reference",
                    "arguments": {"value": 7},
                }
            )

        self.assertFalse(response["result"].get("isError", False), response)
        self.assertEqual(
            read_json_lines(self.workspace.state / "calls.jsonl"),
            [
                {
                    "arguments": {"value": 7},
                    "server": "fixture",
                    "tool": "encoded_reference",
                }
            ],
        )

    def test_integral_numeric_limit_is_enforced(self) -> None:
        self.workspace.refresh()
        self.workspace.clear_fixture_observation()
        with self.workspace.session() as session:
            session.initialize()
            session.assert_tool_error(
                self,
                {
                    "action": "call",
                    "capability_id": "fixture.floating_limits",
                    "arguments": {"value": ""},
                },
                "schema_validation_failed",
            )
            response = session.call(
                {
                    "action": "call",
                    "capability_id": "fixture.floating_limits",
                    "arguments": {"value": "a"},
                }
            )

        self.assertFalse(response["result"].get("isError", False), response)

    def test_json_numeric_semantics_match_the_schema_standard(self) -> None:
        self.workspace.refresh()
        large = 1000000000000000000000000000000
        valid = {
            "integer": 1.0,
            "choice": 1.0,
            "fixed": 1.0,
            "large_choice": large,
            "large_fixed": large,
            "values": [large, large + 1],
        }
        invalid_values = [
            {**valid, "choice": True},
            {**valid, "fixed": True},
            {**valid, "large_fixed": large + 1},
            {**valid, "values": [large, large]},
        ]
        self.workspace.clear_fixture_observation()
        with self.workspace.session() as session:
            session.initialize()
            for arguments in invalid_values:
                with self.subTest(arguments=arguments):
                    session.assert_tool_error(
                        self,
                        {
                            "action": "call",
                            "capability_id": "fixture.json_number_semantics",
                            "arguments": arguments,
                        },
                        "schema_validation_failed",
                    )
            response = session.call(
                {
                    "action": "call",
                    "capability_id": "fixture.json_number_semantics",
                    "arguments": valid,
                }
            )
            self.assertFalse(response["result"].get("isError", False), response)

        calls = read_json_lines(self.workspace.state / "calls.jsonl")
        self.assertEqual(
            calls,
            [{"arguments": valid, "server": "fixture", "tool": "json_number_semantics"}],
        )

    def test_repeated_schema_references_have_a_bounded_validation_cost(self) -> None:
        self.workspace.close()
        self.workspace = Workspace(timeout=0.2)
        self.workspace.refresh()
        started = time.monotonic()
        with self.workspace.session() as session:
            session.initialize()
            session.assert_tool_error(
                self,
                {
                    "action": "call",
                    "capability_id": "fixture.branching_schema",
                    "arguments": {"value": 0},
                },
                "schema_validation_failed",
            )
            response = session.call(
                {
                    "action": "call",
                    "capability_id": "fixture.branching_schema",
                    "arguments": {"value": "valid"},
                }
            )

        self.assertFalse(response["result"].get("isError", False), response)
        self.assertLess(time.monotonic() - started, 1.0)

    def test_validation_limits_cannot_turn_invalid_one_of_into_a_match(self) -> None:
        self.workspace.close()
        self.workspace = Workspace(timeout=0.2)
        self.workspace.refresh()
        self.workspace.clear_fixture_observation()
        started = time.monotonic()
        with self.workspace.session() as session:
            session.initialize()
            session.assert_tool_error(
                self,
                {
                    "action": "call",
                    "capability_id": "fixture.budget_one_of",
                    "arguments": {"values": list(range(5000))},
                },
                "schema_validation_failed",
            )

        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(read_json_lines(self.workspace.state / "starts.jsonl"), [])

    def test_large_enum_identity_work_obeys_the_validation_limit(self) -> None:
        self.workspace.close()
        self.workspace = Workspace(timeout=0.2)
        self.workspace.refresh()
        self.workspace.clear_fixture_observation()
        started = time.monotonic()
        with self.workspace.session() as session:
            session.initialize()
            session.assert_tool_error(
                self,
                {
                    "action": "call",
                    "capability_id": "fixture.large_enum",
                    "arguments": {"value": list(range(10000))},
                },
                "schema_validation_failed",
            )

        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(read_json_lines(self.workspace.state / "starts.jsonl"), [])

    def test_nonfinite_json_numbers_are_rejected_at_process_boundaries(self) -> None:
        self.workspace.refresh()
        with self.workspace.session() as session:
            for raw in (
                b'{"jsonrpc":"2.0","id":7,"method":"ping","params":{"value":NaN}}',
                b'{"jsonrpc":"2.0","id":8,"method":"ping","params":{"value":1e999}}',
            ):
                session.send_raw(raw)
                response = session.receive()
                self.assertEqual(response["error"]["code"], -32700, response)
            response = session.request("ping")
            self.assertEqual(response.get("result"), {}, response)

        raw_config = self.workspace.config()
        payload = json.dumps(raw_config, indent=2).replace(
            '"call_timeout_seconds": 1.0', '"call_timeout_seconds": 1e999'
        )
        self.workspace.config_path.write_text(payload, encoding="utf-8")
        completed = self.workspace.run_cli("status")
        self.assertEqual(completed.returncode, 2, completed)
        self.assertEqual(json.loads(completed.stdout)["error"]["code"], "invalid_input")

    def test_oversized_input_is_drained_before_the_next_request(self) -> None:
        self.workspace.refresh()
        with self.workspace.session() as session:
            session.send_raw(b"x" * (1_048_576 + 64))
            oversized = session.receive()
            self.assertEqual(oversized["error"]["message"], "input too large", oversized)
            response = session.request("ping")
            self.assertEqual(response.get("result"), {}, response)

    def test_deeply_nested_json_is_rejected_without_stopping_the_server(self) -> None:
        self.workspace.refresh()
        nested = b"[" * 100000 + b"0" + b"]" * 100000
        raw = b'{"jsonrpc":"2.0","id":7,"method":"ping","params":' + nested + b"}"
        with self.workspace.session() as session:
            session.send_raw(raw)
            rejected = session.receive()
            self.assertEqual(rejected.get("error", {}).get("code"), -32700, rejected)
            response = session.request("ping")
            self.assertEqual(response.get("result"), {}, response)

    def test_tightened_access_policy_applies_before_catalog_refresh(self) -> None:
        self.workspace.refresh()
        config = self.workspace.config()
        config["servers"]["fixture"]["default_access"] = "destructive"
        self.workspace.config_path.write_text(json.dumps(config), encoding="utf-8")
        self.workspace.clear_fixture_observation()
        with self.workspace.session() as session:
            session.initialize()
            session.assert_tool_error(
                self,
                {
                    "action": "call",
                    "capability_id": "fixture.search_messages",
                    "arguments": {"query": "must stay local"},
                },
                "policy_denied",
            )

        self.assertEqual(read_json_lines(self.workspace.state / "starts.jsonl"), [])

    def test_unsafe_regex_schema_is_unavailable_before_spawn(self) -> None:
        self.workspace.refresh()
        self.workspace.clear_fixture_observation()
        with self.workspace.session() as session:
            session.initialize()
            for capability_id in ("fixture.unsafe_pattern", "fixture.unanchored_pattern"):
                with self.subTest(capability_id=capability_id):
                    session.assert_tool_error(
                        self,
                        {
                            "action": "call",
                            "capability_id": capability_id,
                            "arguments": {"value": "aa!"},
                        },
                        "unavailable",
                    )

        self.assertEqual(read_json_lines(self.workspace.state / "starts.jsonl"), [])

    def test_excessive_reference_chain_is_unavailable_before_spawn(self) -> None:
        self.workspace.refresh()
        self.workspace.clear_fixture_observation()
        with self.workspace.session() as session:
            session.initialize()
            session.assert_tool_error(
                self,
                {
                    "action": "call",
                    "capability_id": "fixture.long_reference_schema",
                    "arguments": {},
                },
                "unavailable",
            )

        self.assertEqual(read_json_lines(self.workspace.state / "starts.jsonl"), [])

    def test_mixed_schema_depth_is_unavailable_without_aborting_refresh(self) -> None:
        self.workspace.refresh()
        self.workspace.clear_fixture_observation()
        with self.workspace.session() as session:
            session.initialize()
            session.assert_tool_error(
                self,
                {
                    "action": "call",
                    "capability_id": "fixture.mixed_reference_schema",
                    "arguments": {},
                },
                "unavailable",
            )

        self.assertEqual(read_json_lines(self.workspace.state / "starts.jsonl"), [])

    def test_ordered_reference_chain_is_unavailable_without_runtime_recursion(self) -> None:
        self.workspace.refresh()
        self.workspace.clear_fixture_observation()
        with self.workspace.session() as session:
            session.initialize()
            session.assert_tool_error(
                self,
                {
                    "action": "call",
                    "capability_id": "fixture.ordered_reference_schema",
                    "arguments": {"value": "safe"},
                },
                "unavailable",
            )

        self.assertEqual(read_json_lines(self.workspace.state / "starts.jsonl"), [])

    def test_unsupported_schema_dialect_is_unavailable_before_spawn(self) -> None:
        self.workspace.refresh()
        self.workspace.clear_fixture_observation()
        with self.workspace.session() as session:
            session.initialize()
            session.assert_tool_error(
                self,
                {
                    "action": "call",
                    "capability_id": "fixture.draft_seven_schema",
                    "arguments": {"value": "valid-in-draft-seven"},
                },
                "unavailable",
            )

        self.assertEqual(read_json_lines(self.workspace.state / "starts.jsonl"), [])

    def test_explicit_null_type_is_unavailable_before_spawn(self) -> None:
        self.workspace.refresh()
        self.workspace.clear_fixture_observation()
        with self.workspace.session() as session:
            session.initialize()
            session.assert_tool_error(
                self,
                {
                    "action": "call",
                    "capability_id": "fixture.malformed_null_type",
                    "arguments": {},
                },
                "unavailable",
            )

        self.assertEqual(read_json_lines(self.workspace.state / "starts.jsonl"), [])

    def test_malformed_annotation_types_are_unavailable_before_spawn(self) -> None:
        self.workspace.refresh()
        self.workspace.clear_fixture_observation()
        with self.workspace.session() as session:
            session.initialize()
            session.assert_tool_error(
                self,
                {
                    "action": "call",
                    "capability_id": "fixture.malformed_annotations",
                    "arguments": {},
                },
                "unavailable",
            )

        self.assertEqual(read_json_lines(self.workspace.state / "starts.jsonl"), [])

    def test_malformed_unicode_reference_is_unavailable_before_spawn(self) -> None:
        self.workspace.refresh()
        self.workspace.clear_fixture_observation()
        with self.workspace.session() as session:
            session.initialize()
            session.assert_tool_error(
                self,
                {
                    "action": "call",
                    "capability_id": "fixture.unicode_reference_schema",
                    "arguments": {},
                },
                "unavailable",
            )

        self.assertEqual(read_json_lines(self.workspace.state / "starts.jsonl"), [])

    def test_malformed_character_class_is_unavailable_before_spawn(self) -> None:
        self.workspace.refresh()
        self.workspace.clear_fixture_observation()
        with self.workspace.session() as session:
            session.initialize()
            session.assert_tool_error(
                self,
                {
                    "action": "call",
                    "capability_id": "fixture.malformed_character_class",
                    "arguments": {"value": "a"},
                },
                "unavailable",
            )

        self.assertEqual(read_json_lines(self.workspace.state / "starts.jsonl"), [])

    def test_nullable_schema_type_is_callable_through_public_tool(self) -> None:
        self.workspace.refresh()
        self.workspace.clear_fixture_observation()
        with self.workspace.session() as session:
            session.initialize()
            session.assert_tool_error(
                self,
                {
                    "action": "call",
                    "capability_id": "fixture.nullable_lookup",
                    "arguments": {"query": "alpha", "cursor": 7},
                },
                "schema_validation_failed",
            )
            response = session.call(
                {
                    "action": "call",
                    "capability_id": "fixture.nullable_lookup",
                    "arguments": {"query": "alpha", "cursor": None},
                }
            )
            self.assertFalse(response["result"].get("isError", False), response)
            self.assertIn("nullable:alpha:None", canonical_bytes(response).decode("utf-8"))

        calls = read_json_lines(self.workspace.state / "calls.jsonl")
        self.assertEqual(
            calls,
            [{"arguments": {"cursor": None, "query": "alpha"}, "server": "fixture", "tool": "nullable_lookup"}],
        )

    def test_standard_schema_composition_is_enforced_by_public_tool(self) -> None:
        self.workspace.refresh()
        valid = {
            "target": "alpha",
            "filters": {"region": "in", "owner": None},
            "page": 1,
            "strict": 1.5,
        }
        invalid_values = [
            {**valid, "target": "A"},
            {**valid, "filters": {"Region": "in"}},
            {**valid, "filters": {"region": 7}},
            {**valid, "page": 0},
            {**valid, "strict": 1},
        ]
        self.workspace.clear_fixture_observation()
        with self.workspace.session() as session:
            session.initialize()
            for arguments in invalid_values:
                with self.subTest(arguments=arguments):
                    session.assert_tool_error(
                        self,
                        {"action": "call", "capability_id": "fixture.structured_lookup", "arguments": arguments},
                        "schema_validation_failed",
                    )
            response = session.call(
                {"action": "call", "capability_id": "fixture.structured_lookup", "arguments": valid}
            )
            self.assertFalse(response["result"].get("isError", False), response)

        calls = read_json_lines(self.workspace.state / "calls.jsonl")
        self.assertEqual(calls, [{"arguments": valid, "server": "fixture", "tool": "structured_lookup"}])

    def test_call_denies_write_before_spawn(self) -> None:
        self.workspace.refresh()
        self.workspace.clear_fixture_observation()
        with self.workspace.session() as session:
            session.initialize()
            session.assert_tool_error(
                self,
                {
                    "action": "call",
                    "capability_id": "fixture.send_message",
                    "arguments": {"channel": "ops", "text": "must not send"},
                },
                "policy_denied",
            )

        self.assertEqual(read_json_lines(self.workspace.state / "calls.jsonl"), [])

    def test_denied_call_never_starts_server(self) -> None:
        self.workspace.refresh()
        self.workspace.clear_fixture_observation()
        with self.workspace.session() as session:
            session.initialize()
            session.assert_tool_error(
                self,
                {
                    "action": "call",
                    "capability_id": "fixture.send_message",
                    "arguments": {"channel": "ops", "text": "must not send"},
                },
                "policy_denied",
            )

        self.assertEqual(read_json_lines(self.workspace.state / "starts.jsonl"), [])

    def test_call_denies_changed_runtime_schema(self) -> None:
        self.workspace.refresh()
        (self.workspace.state / "fixture.schema-mode").write_text("changed", encoding="utf-8")
        self.workspace.clear_fixture_observation()
        with self.workspace.session() as session:
            session.initialize()
            session.assert_tool_error(
                self,
                {
                    "action": "call",
                    "capability_id": "fixture.search_messages",
                    "arguments": {"query": "safe"},
                },
                "refresh_required",
            )

        self.assertEqual(read_json_lines(self.workspace.state / "calls.jsonl"), [])
        self.assertEqual(len(read_json_lines(self.workspace.state / "starts.jsonl")), 1)

    def test_call_denies_changed_runtime_task_support_before_call(self) -> None:
        self.workspace.refresh()
        (self.workspace.state / "fixture.mode").write_text(
            "task-required", encoding="utf-8"
        )
        self.workspace.clear_fixture_observation()

        with self.workspace.session() as session:
            session.initialize()
            session.assert_tool_error(
                self,
                {
                    "action": "call",
                    "capability_id": "fixture.search_messages",
                    "arguments": {"query": "safe"},
                },
                "refresh_required",
            )

        self.assertEqual(read_json_lines(self.workspace.state / "calls.jsonl"), [])


class ExecutionTests(RouterTestCase):
    def test_failed_io_thread_start_returns_a_structured_error_without_leaking_fds(
        self,
    ) -> None:
        descriptor_root = Path("/proc/self/fd")
        if not descriptor_root.exists():
            descriptor_root = Path("/dev/fd")
        before = len(list(descriptor_root.iterdir()))
        documents: list[dict[str, Any]] = []

        with mock.patch(
            "capability_router.downstream.threading.Thread.start",
            side_effect=RuntimeError("synthetic thread start failure"),
        ), mock.patch("capability_router.cli.emit", side_effect=documents.append):
            return_code = cli.main(
                ["refresh", "--config", str(self.workspace.config_path)]
            )

        after = len(list(descriptor_root.iterdir()))
        self.assertEqual(return_code, 4)
        self.assertEqual(first_nested(documents, "code"), "execution_failed")
        self.assertEqual(after, before)

    def test_call_executes_read_tool(self) -> None:
        self.workspace.refresh()
        self.workspace.clear_fixture_observation()
        arguments = {"query": "needle"}
        with self.workspace.session() as session:
            session.initialize()
            response = session.call(
                {"action": "call", "capability_id": "fixture.search_messages", "arguments": arguments}
            )
            self.assertFalse(response["result"].get("isError", False), response)

        self.assertEqual(
            read_json_lines(self.workspace.state / "calls.jsonl"),
            [{"arguments": arguments, "server": "fixture", "tool": "search_messages"}],
        )

    def test_downstream_ping_is_answered_while_a_request_is_pending(self) -> None:
        self.workspace.refresh()
        self.workspace.clear_fixture_observation()
        (self.workspace.state / "fixture.mode").write_text(
            "ping-before-response", encoding="utf-8"
        )
        with self.workspace.session() as session:
            session.initialize()
            response = session.call(
                {
                    "action": "call",
                    "capability_id": "fixture.search_messages",
                    "arguments": {"query": "ping"},
                }
            )

        self.assertFalse(response["result"].get("isError", False), response)
        ping_responses = read_json_lines(self.workspace.state / "ping-responses.jsonl")
        self.assertGreaterEqual(len(ping_responses), 3, ping_responses)
        self.assertTrue(
            all(item == {"jsonrpc": "2.0", "id": 900, "result": {}} for item in ping_responses),
            ping_responses,
        )

    def test_malformed_downstream_protocol_is_rejected(self) -> None:
        self.workspace.refresh()
        for mode in (
            "wrong-jsonrpc",
            "unsupported-version",
            "unsupported-version-list",
            "boolean-response-id",
            "malformed-call-result",
        ):
            with self.subTest(mode=mode):
                self.workspace.clear_fixture_observation()
                (self.workspace.state / "fixture.mode").write_text(mode, encoding="utf-8")
                with self.workspace.session() as session:
                    session.initialize()
                    session.assert_tool_error(
                        self,
                        {
                            "action": "call",
                            "capability_id": "fixture.search_messages",
                            "arguments": {"query": "protocol"},
                        },
                        "downstream_protocol_error",
                    )

    def test_failed_call_is_not_retried(self) -> None:
        self.workspace.refresh()
        self.workspace.clear_fixture_observation()
        with self.workspace.session() as session:
            session.initialize()
            response = session.call({"action": "call", "capability_id": "fixture.fail_once", "arguments": {}})
            self.assertIs(response["result"].get("isError"), True, response)

        calls = read_json_lines(self.workspace.state / "calls.jsonl")
        self.assertEqual([item["tool"] for item in calls], ["fail_once"])

    def test_timeout_terminates_fixture(self) -> None:
        self.workspace.close()
        self.workspace = Workspace(timeout=0.25)
        self.workspace.refresh()
        self.workspace.clear_fixture_observation()
        with self.workspace.session() as session:
            session.initialize()
            session.assert_tool_error(
                self,
                {"action": "call", "capability_id": "fixture.hang_with_child", "arguments": {}},
                "timeout",
            )

        pids = json.loads((self.workspace.state / "hanging-pids.json").read_text(encoding="utf-8"))
        self.assertTrue(wait_process_gone(pids["server"]), pids)
        self.assertTrue(wait_process_gone(pids["grandchild"]), pids)
        calls = read_json_lines(self.workspace.state / "calls.jsonl")
        self.assertEqual([item["tool"] for item in calls], ["hang_with_child"])

    def test_stdin_backpressure_obeys_the_call_deadline(self) -> None:
        self.workspace.close()
        self.workspace = Workspace(timeout=0.25)
        self.workspace.refresh()
        self.workspace.environment["ROUTER_TEST_FIXTURE_MODE"] = "stop-after-list"
        started = time.monotonic()
        with self.workspace.session() as session:
            session.initialize()
            session.assert_tool_error(
                self,
                {
                    "action": "call",
                    "capability_id": "fixture.backpressure",
                    "arguments": {"text": "x" * 500000},
                },
                "timeout",
            )
        self.assertLess(time.monotonic() - started, 2.0)

    def test_router_termination_cleans_active_downstream_processes(self) -> None:
        self.workspace.close()
        self.workspace = Workspace(timeout=10)
        self.workspace.refresh()
        self.workspace.clear_fixture_observation()
        downstream_pid: int | None = None
        try:
            with self.workspace.session() as session:
                session.initialize()
                session.send(
                    {
                        "jsonrpc": "2.0",
                        "id": 77,
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
                deadline = time.monotonic() + 2
                starts: list[dict[str, Any]] = []
                while time.monotonic() < deadline:
                    starts = read_json_lines(self.workspace.state / "starts.jsonl")
                    if starts:
                        break
                    time.sleep(0.02)
                self.assertTrue(starts, "downstream process did not start")
                downstream_pid = starts[-1]["pid"]
                assert session.process is not None
                session.process.terminate()
                session.process.wait(timeout=3)
                self.assertEqual(session.process.returncode, 143)

            self.assertTrue(wait_process_gone(downstream_pid), downstream_pid)
        finally:
            if downstream_pid is not None and process_exists(downstream_pid):
                try:
                    os.killpg(downstream_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_repeated_termination_does_not_interrupt_downstream_cleanup(self) -> None:
        definition = self.workspace.server()
        definition["args"] = [
            "-c",
            "import runpy,signal; "
            "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
            f"runpy.run_path({str(FIXTURE_SERVER)!r},run_name='__main__')",
        ]
        self.workspace.write_config({"fixture": definition})
        self.workspace.refresh()
        self.workspace.clear_fixture_observation()
        downstream_pid: int | None = None
        try:
            with self.workspace.session() as session:
                session.initialize()
                session.send(
                    {
                        "jsonrpc": "2.0",
                        "id": 77,
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
                deadline = time.monotonic() + 2
                starts: list[dict[str, Any]] = []
                while time.monotonic() < deadline:
                    starts = read_json_lines(self.workspace.state / "starts.jsonl")
                    if starts:
                        break
                    time.sleep(0.02)
                self.assertTrue(starts, "downstream process did not start")
                downstream_pid = starts[-1]["pid"]
                assert session.process is not None
                session.process.terminate()
                time.sleep(0.05)
                session.process.terminate()
                session.process.wait(timeout=3)
                self.assertEqual(session.process.returncode, 143)

            self.assertTrue(wait_process_gone(downstream_pid), downstream_pid)
        finally:
            if downstream_pid is not None and process_exists(downstream_pid):
                try:
                    os.killpg(downstream_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_output_backpressure_does_not_block_router_termination(self) -> None:
        self.workspace.refresh()
        session = McpSession(self.workspace, run_id="output-backpressure")
        session.start()
        try:
            session.initialize()
            for index in range(8):
                session.send(
                    {
                        "jsonrpc": "2.0",
                        "id": f"{index}-" + "x" * 20000,
                        "method": "ping",
                    }
                )
            time.sleep(0.2)
            assert session.process is not None
            session.process.terminate()
            session.process.wait(timeout=3)
            self.assertEqual(session.process.returncode, 143)
        finally:
            if session.process is not None and session.process.poll() is None:
                session.process.kill()
                session.process.wait(timeout=2)
            session.close()

    def test_exited_parent_still_has_its_process_group_cleaned_up(self) -> None:
        self.workspace.environment["ROUTER_TEST_FIXTURE_MODE"] = "exit-before-response-with-child"
        completed = self.workspace.run_cli("refresh")
        self.assertEqual(completed.returncode, 4, completed)
        pids = json.loads((self.workspace.state / "exited-parent-pids.json").read_text(encoding="utf-8"))
        self.assertTrue(wait_process_gone(pids["server"]), pids)
        self.assertTrue(wait_process_gone(pids["grandchild"]), pids)

    def test_cleanup_does_not_wait_for_detached_inherited_pipes(self) -> None:
        self.workspace.environment["ROUTER_TEST_FIXTURE_MODE"] = "exit-with-detached-pipe-child"
        started = time.monotonic()
        completed = self.workspace.run_cli("refresh", timeout=5)
        elapsed = time.monotonic() - started
        self.assertEqual(completed.returncode, 4, completed)
        self.assertLess(elapsed, 1.5)
        pids = json.loads((self.workspace.state / "detached-pipe-pids.json").read_text(encoding="utf-8"))
        self.assertTrue(wait_process_gone(pids["grandchild"]), pids)

    def test_downstream_response_frame_is_bounded(self) -> None:
        self.workspace.refresh()
        with self.workspace.session() as session:
            session.initialize()
            session.assert_tool_error(
                self,
                {"action": "call", "capability_id": "fixture.oversized_frame", "arguments": {}},
                "downstream_protocol_error",
            )

    def test_error_response_obeys_the_output_limit(self) -> None:
        self.workspace.close()
        self.workspace = Workspace(output_limit=512)
        self.workspace.refresh()
        with self.workspace.session() as session:
            session.initialize()
            response, document = session.tool_document(
                {"action": "describe", "capability_id": "missing-" + "x" * 10000}
            )

        self.assertIs(response["result"].get("isError"), True, response)
        self.assertLessEqual(len(canonical_bytes(response["result"])), 512)
        self.assertEqual(first_nested(document, "code"), "not_found")
        self.assertIn(True, nested_values(document, "truncated"), document)

    def test_execution_error_with_a_long_server_name_obeys_the_output_limit(self) -> None:
        self.workspace.close()
        self.workspace = Workspace(output_limit=512)
        server_name = "server-" + "x" * 300
        self.workspace.write_config({server_name: self.workspace.server(server_name)})
        self.workspace.refresh()
        config = json.loads(self.workspace.config_path.read_text(encoding="utf-8"))
        config["servers"][server_name]["command"] = "/missing/router-test-command"
        self.workspace.config_path.write_text(json.dumps(config), encoding="utf-8")

        with self.workspace.session() as session:
            session.initialize()
            response = session.call(
                {
                    "action": "call",
                    "capability_id": f"{server_name}.search_messages",
                    "arguments": {"query": "x"},
                }
            )

        self.assertIs(response["result"].get("isError"), True, response)
        self.assertLessEqual(len(canonical_bytes(response["result"])), 512)
        self.assertEqual(first_nested(response["result"], "code"), "execution_failed")

    def test_downstream_error_obeys_the_output_limit(self) -> None:
        self.workspace.close()
        self.workspace = Workspace(output_limit=512)
        self.workspace.refresh()
        with self.workspace.session() as session:
            session.initialize()
            response = session.call(
                {"action": "call", "capability_id": "fixture.sized_error", "arguments": {}}
            )

        self.assertIs(response["result"].get("isError"), True, response)
        self.assertLessEqual(len(canonical_bytes(response["result"])), 512)

    def test_large_result_is_bounded_and_artifact_is_complete(self) -> None:
        self.workspace.refresh()
        with self.workspace.session() as session:
            session.initialize("2024-11-05")
            response, document = session.tool_document(
                {"action": "call", "capability_id": "fixture.large_result", "arguments": {}}, timeout=4
            )

        self.assertLessEqual(len(canonical_bytes(response["result"])), self.workspace.output_limit)
        self.assertIn(True, nested_values(document, "truncated"), document)
        relative = Path(first_nested(document, "artifact_path"))
        self.assertFalse(relative.is_absolute(), relative)
        artifact = (self.workspace.root / relative).resolve()
        self.assertTrue(artifact.is_relative_to(self.workspace.artifact_dir.resolve()), artifact)
        payload = artifact.read_bytes()
        decoded = payload.decode("utf-8")
        self.assertIn("FULL-PAYLOAD-BEGIN:", decoded)
        self.assertIn(":FULL-PAYLOAD-END", decoded)
        self.assertEqual(decoded.count("λ"), 16_000)
        self.assertEqual(first_nested(document, "byte_count"), len(payload))
        self.assertEqual(first_nested(document, "sha256"), hashlib.sha256(payload).hexdigest())
        self.assertTrue(first_nested(document, "media_type").startswith("application/json"), document)
        self.assertEqual(stat.S_IMODE(self.workspace.artifact_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(artifact.stat().st_mode), 0o600)
        self.assertIn(str(relative), response["result"]["content"][0]["text"])

    def test_completed_write_is_not_reported_as_failed_when_artifact_storage_fails(self) -> None:
        self.workspace.close()
        self.workspace = Workspace(output_limit=512)
        config = self.workspace.config()
        config["mode"] = "unrestricted"
        self.workspace.config_path.write_text(json.dumps(config), encoding="utf-8")
        self.workspace.refresh()
        self.workspace.artifact_dir.write_text("occupied", encoding="utf-8")

        with self.workspace.session() as session:
            session.initialize()
            response, document = session.tool_document(
                {"action": "call", "capability_id": "fixture.large_write", "arguments": {}},
                timeout=4,
            )

        calls = read_json_lines(self.workspace.state / "calls.jsonl")
        self.assertEqual([item["tool"] for item in calls], ["large_write"])
        self.assertFalse(response["result"].get("isError", False), response)
        self.assertIs(first_nested(document, "completed"), True)
        self.assertIs(first_nested(document, "result_unavailable"), True)

    def test_task_required_tools_are_discovered_as_unavailable(self) -> None:
        self.workspace.refresh()
        with self.workspace.session() as session:
            session.initialize()
            _, described = session.tool_document(
                {"action": "describe", "capability_id": "fixture.task_required"}
            )
            rejected = session.call(
                {"action": "call", "capability_id": "fixture.task_required", "arguments": {}}
            )

        self.assertEqual(first_nested(described, "availability"), "unavailable")
        self.assertEqual(first_nested(rejected, "code"), "unavailable")

    def test_external_artifact_directory_returns_the_real_absolute_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="router-external-artifacts-") as directory:
            external = Path(directory)
            config = self.workspace.config()
            config["artifact_dir"] = str(external)
            self.workspace.config_path.write_text(json.dumps(config), encoding="utf-8")
            self.workspace.refresh()
            with self.workspace.session() as session:
                session.initialize()
                _, document = session.tool_document(
                    {"action": "call", "capability_id": "fixture.large_result", "arguments": {}},
                    timeout=4,
                )

            artifact = Path(first_nested(document, "artifact_path"))
            self.assertTrue(artifact.is_absolute(), artifact)
            self.assertTrue(artifact.is_relative_to(external.resolve()), artifact)
            self.assertTrue(artifact.is_file(), artifact)


class RefreshAndHygieneTests(RouterTestCase):
    def test_mapped_path_resolves_a_bare_downstream_command(self) -> None:
        bindir = self.workspace.root / "bin"
        bindir.mkdir()
        command = bindir / "fixture-python"
        command.symlink_to(Path(sys.executable))
        definition = self.workspace.server()
        definition["command"] = command.name
        definition["inherit_environment"] = []
        definition["environment_from_parent"]["PATH"] = "ROUTER_TEST_MAPPED_PATH"
        self.workspace.environment["ROUTER_TEST_MAPPED_PATH"] = str(bindir)
        self.workspace.write_config({"fixture": definition})

        completed = self.workspace.run_cli("refresh")

        self.assertEqual(completed.returncode, 0, completed)

    def test_catalog_write_failure_is_a_structured_execution_error(self) -> None:
        self.workspace.catalog_path.mkdir(parents=True)

        completed = self.workspace.run_cli("refresh")

        self.assertEqual(completed.returncode, 4, completed)
        self.assertEqual(json.loads(completed.stdout)["error"]["code"], "execution_failed")
        self.assertEqual(completed.stderr, "")

    def test_invalid_process_fields_return_structured_input_errors(self) -> None:
        cases = {
            "command": "bad\0command",
            "args": ["bad\0argument"],
            "cwd": "bad\0directory",
            "inherit_environment": ["BAD=NAME"],
            "environment_from_parent": {"BAD=NAME": "PATH"},
            "environment_from_parent_source": {"SAFE_NAME": "BAD=NAME"},
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                config = self.workspace.config()
                server = config["servers"]["fixture"]
                target = "environment_from_parent" if field.endswith("_source") else field
                server[target] = value
                self.workspace.config_path.write_text(json.dumps(config), encoding="utf-8")

                completed = self.workspace.run_cli("refresh")

                self.assertEqual(completed.returncode, 2, completed)
                self.assertEqual(completed.stderr, "")
                self.assertEqual(
                    json.loads(completed.stdout)["error"]["code"], "invalid_input"
                )

    def test_refresh_discards_invalid_stale_schema_before_publish(self) -> None:
        healthy = self.workspace.server()
        broken = self.workspace.server()
        self.workspace.write_config({"healthy": healthy, "broken": broken})
        self.workspace.refresh()

        catalog = json.loads(self.workspace.catalog_path.read_text(encoding="utf-8"))
        corrupt = next(
            item
            for item in catalog["capabilities"]
            if item.get("source") == "broken" and item.get("availability") == "available"
        )
        corrupt["input_schema"] = {"type": "definitely-invalid"}
        corrupt["schema_sha256"] = hashlib.sha256(
            canonical_bytes(corrupt["input_schema"])
        ).hexdigest()
        self.workspace.catalog_path.write_text(json.dumps(catalog), encoding="utf-8")

        broken["args"] = ["-c", "raise SystemExit(1)"]
        self.workspace.write_config({"healthy": healthy, "broken": broken})
        refreshed = self.workspace.run_cli("refresh")
        status = self.workspace.run_cli("status")

        self.assertEqual(refreshed.returncode, 0, refreshed)
        self.assertEqual(refreshed.stderr, "")
        self.assertEqual(status.returncode, 0, status)
        self.assertEqual(status.stderr, "")
        published = json.loads(self.workspace.catalog_path.read_text(encoding="utf-8"))
        sources = {item.get("source") for item in published["capabilities"]}
        self.assertIn("healthy", sources)
        self.assertNotIn("broken", sources)

    def test_refresh_discards_structurally_invalid_cached_entries(self) -> None:
        self.workspace.catalog_path.parent.mkdir(parents=True)
        self.workspace.catalog_path.write_text(
            json.dumps({"version": 1, "capabilities": [None]}),
            encoding="utf-8",
        )
        self.workspace.environment["ROUTER_TEST_FIXTURE_MODE"] = "fail"

        completed = self.workspace.run_cli("refresh")

        self.assertEqual(completed.returncode, 4, completed)
        self.assertEqual(completed.stderr, "")
        document = json.loads(completed.stdout)
        self.assertEqual(document["error"]["code"], "execution_failed")
        self.assertTrue(document["error"]["details"]["errors"], document)

    def test_refresh_contains_malformed_initialization_version(self) -> None:
        self.workspace.refresh()
        (self.workspace.state / "fixture.mode").write_text(
            "unsupported-version-list", encoding="utf-8"
        )

        completed = self.workspace.run_cli("refresh")

        self.assertEqual(completed.returncode, 0, completed)
        self.assertEqual(completed.stderr, "")
        document = json.loads(completed.stdout)
        self.assertEqual(document["errors"][0]["code"], "downstream_protocol_error")
        catalog = json.loads(self.workspace.catalog_path.read_text(encoding="utf-8"))
        fixture_tools = [
            item
            for item in catalog["capabilities"]
            if item.get("kind") == "mcp_tool" and item.get("source") == "fixture"
        ]
        self.assertTrue(fixture_tools)
        self.assertTrue(all(item["stale"] is True for item in fixture_tools))

    def test_signal_during_normal_refresh_cleanup_finishes_child_cleanup(self) -> None:
        marker = self.workspace.state / "cleanup-term-seen"
        program = (
            "import json,os,pathlib,signal,sys,time;"
            f"marker=pathlib.Path({str(marker)!r});"
            "signal.signal(signal.SIGTERM,lambda *_: marker.write_text(str(os.getpid())));"
            "request=json.loads(sys.stdin.readline());"
            "print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':"
            "{'protocolVersion':'2025-11-25','capabilities':{'tools':{}},"
            "'serverInfo':{'name':'cleanup-fixture','version':'1'}}}),flush=True);"
            "sys.stdin.readline();"
            "request=json.loads(sys.stdin.readline());"
            "print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':{'tools':[]}}),flush=True);"
            "time.sleep(60)"
        )
        definition = self.workspace.server()
        definition["args"] = ["-c", program]
        self.workspace.write_config({"fixture": definition})
        process = subprocess.Popen(
            [*CLI, "refresh", "--config", str(self.workspace.config_path)],
            cwd=PROJECT_ROOT,
            env=self.workspace.environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        downstream_pid: int | None = None
        try:
            deadline = time.monotonic() + 2
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.002)
            self.assertTrue(marker.exists(), "normal refresh cleanup did not start")
            downstream_pid = int(marker.read_text(encoding="utf-8"))
            process.terminate()
            process.wait(timeout=3)
            self.assertEqual(process.returncode, 143)
            self.assertTrue(wait_process_gone(downstream_pid), downstream_pid)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
            if downstream_pid is not None and process_exists(downstream_pid):
                try:
                    os.killpg(downstream_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()

    def test_repeated_refresh_termination_cleans_active_downstream_processes(self) -> None:
        definition = self.workspace.server()
        definition["args"] = [
            "-c",
            "import runpy,signal; "
            "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
            f"runpy.run_path({str(FIXTURE_SERVER)!r},run_name='__main__')",
        ]
        self.workspace.write_config({"fixture": definition})
        self.workspace.environment["ROUTER_TEST_FIXTURE_MODE"] = "hang-initialize"
        process = subprocess.Popen(
            [*CLI, "refresh", "--config", str(self.workspace.config_path)],
            cwd=PROJECT_ROOT,
            env=self.workspace.environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        downstream_pid: int | None = None
        try:
            deadline = time.monotonic() + 2
            starts: list[dict[str, Any]] = []
            while time.monotonic() < deadline:
                starts = read_json_lines(self.workspace.state / "starts.jsonl")
                if starts:
                    break
                time.sleep(0.02)
            self.assertTrue(starts, "refresh downstream process did not start")
            downstream_pid = starts[-1]["pid"]
            process.terminate()
            time.sleep(0.05)
            process.terminate()
            process.wait(timeout=3)
            self.assertEqual(process.returncode, 143)
            self.assertTrue(wait_process_gone(downstream_pid), downstream_pid)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
            if downstream_pid is not None and process_exists(downstream_pid):
                try:
                    os.killpg(downstream_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()

    def test_refresh_tolerates_null_response_to_initialized_notification(self) -> None:
        self.workspace.environment["ROUTER_TEST_FIXTURE_MODE"] = "null-notification-response"
        report = self.workspace.refresh()
        catalog = json.loads(self.workspace.catalog_path.read_text(encoding="utf-8"))

        self.assertEqual(report["errors"], [])
        self.assertTrue(any(item["id"] == "fixture.search_messages" for item in catalog["capabilities"]))

    def test_refresh_preserves_other_servers_on_failure(self) -> None:
        servers = {"alpha": self.workspace.server("alpha"), "beta": self.workspace.server("beta")}
        self.workspace.write_config(servers)
        self.workspace.refresh()
        original = json.loads(self.workspace.catalog_path.read_text(encoding="utf-8"))
        beta_ids = {item["id"] for item in original["capabilities"] if item["source"] == "beta"}
        self.assertTrue(beta_ids)

        self.workspace.environment["ROUTER_TEST_BETA_MODE"] = "fail"
        refreshed = self.workspace.refresh()
        catalog = json.loads(self.workspace.catalog_path.read_text(encoding="utf-8"))
        alpha = [item for item in catalog["capabilities"] if item["source"] == "alpha"]
        beta = [item for item in catalog["capabilities"] if item["source"] == "beta"]
        self.assertTrue(alpha)
        self.assertTrue(all(not item["stale"] for item in alpha), alpha)
        self.assertEqual({item["id"] for item in beta}, beta_ids)
        self.assertTrue(all(item["stale"] for item in beta), beta)
        self.assertEqual(
            refreshed["errors"],
            [{"server": "beta", "error": "RouterError", "code": "downstream_protocol_error"}],
        )

    def test_refresh_and_call_follow_tool_pages(self) -> None:
        self.workspace.refresh()
        catalog = json.loads(self.workspace.catalog_path.read_text(encoding="utf-8"))
        self.assertIn("fixture.paginated_lookup", {item["id"] for item in catalog["capabilities"]})
        self.workspace.clear_fixture_observation()
        with self.workspace.session() as session:
            session.initialize()
            response = session.call(
                {"action": "call", "capability_id": "fixture.paginated_lookup", "arguments": {"id": 7}}
            )
            self.assertFalse(response["result"].get("isError", False), response)

        self.assertEqual(
            read_json_lines(self.workspace.state / "calls.jsonl"),
            [{"arguments": {"id": 7}, "server": "fixture", "tool": "paginated_lookup"}],
        )

    def test_refresh_records_catalog_and_schema_digests(self) -> None:
        refreshed = self.workspace.refresh()
        catalog_bytes = self.workspace.catalog_path.read_bytes()
        catalog = json.loads(catalog_bytes)
        generation_digests = nested_values(refreshed, "catalog_sha256") + nested_values(refreshed, "sha256")
        self.assertIn(hashlib.sha256(catalog_bytes).hexdigest(), generation_digests, refreshed)
        for capability in catalog["capabilities"]:
            if capability["kind"] == "mcp_tool" and capability["availability"] == "available":
                self.assertRegex(capability["schema_sha256"], r"^[0-9a-f]{64}$")
                self.assertEqual(
                    capability["schema_sha256"], hashlib.sha256(canonical_bytes(capability["input_schema"])).hexdigest()
                )
        unsupported = next(item for item in catalog["capabilities"] if item["id"] == "fixture.unsupported_schema")
        self.assertEqual(unsupported["availability"], "unavailable")

    def test_catalog_and_logs_exclude_environment_values(self) -> None:
        sentinel = "SECRET-CATALOG-SENTINEL-91c7"
        self.workspace.environment["ROUTER_TEST_SECRET"] = sentinel
        refresh = self.workspace.run_cli("refresh")
        self.assertEqual(refresh.returncode, 0, refresh.stderr)
        self.assertNotIn(sentinel, refresh.stdout + refresh.stderr)
        with self.workspace.session() as session:
            session.initialize()
            session.call({"action": "search", "query": "message"})
        router_stderr = session.stderr_bytes.decode("utf-8", errors="replace")
        self.assertNotIn(sentinel, router_stderr)
        for path in (self.workspace.catalog_path, self.workspace.audit_path, self.workspace.state / "starts.jsonl"):
            self.assertNotIn(sentinel, path.read_text(encoding="utf-8"), path)

    def test_arguments_use_stdin_only(self) -> None:
        sentinel = "CREDENTIAL-CMDLINE-SENTINEL-77d2"
        self.workspace.refresh()
        self.workspace.clear_fixture_observation()
        with self.workspace.session() as session:
            session.initialize()
            response = session.call(
                {
                    "action": "call",
                    "capability_id": "fixture.inspect_transport",
                    "arguments": {"credential": sentinel},
                }
            )
            self.assertFalse(response["result"].get("isError", False), response)
        observed = (self.workspace.state / "process.json").read_text(encoding="utf-8")
        self.assertNotIn(sentinel, observed)
        self.assertNotIn(sentinel, self.workspace.audit_path.read_text(encoding="utf-8"))
        self.assertNotIn(sentinel, session.stderr_bytes.decode("utf-8", errors="replace"))
        self.assertNotIn(sentinel, (self.workspace.state / "calls.jsonl").read_text(encoding="utf-8"))

    def test_downstream_stderr_is_bounded_and_redacted(self) -> None:
        sentinel = "STDERR-SECRET-SENTINEL-d840"
        self.workspace.environment["ROUTER_TEST_SECRET"] = sentinel
        self.workspace.refresh()
        (self.workspace.state / "fixture.mode").write_text("fail", encoding="utf-8")
        with self.workspace.session() as session:
            session.initialize()
            response = session.call(
                {"action": "call", "capability_id": "fixture.search_messages", "arguments": {"query": "x"}}
            )
        visible = canonical_bytes(response) + session.stderr_bytes
        self.assertNotIn(sentinel.encode(), visible)
        self.assertLess(len(visible), 16_000)


class StatusAndAuditTests(RouterTestCase):
    def test_status_reports_catalog_and_mode(self) -> None:
        self.workspace.refresh()
        completed = self.workspace.run_cli("status")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        status_document = json.loads(completed.stdout)
        self.assertEqual(status_document["mode"], "read-only")
        self.assertGreater(status_document["capability_count"], 0)
        self.assertRegex(status_document["catalog_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(status_document["catalog_path"], str(self.workspace.catalog_path))

    def test_cli_uses_stable_input_execution_and_verification_exit_codes(self) -> None:
        self.workspace.config_path.write_text("[]", encoding="utf-8")
        invalid = self.workspace.run_cli("status")
        self.assertEqual(invalid.returncode, 2, invalid)
        self.assertEqual(json.loads(invalid.stdout)["error"]["code"], "invalid_input")

        broken_server = self.workspace.server()
        broken_server["command"] = "/definitely/not/a/router-test-executable"
        self.workspace.write_config({"fixture": broken_server})
        execution = self.workspace.run_cli("refresh")
        self.assertEqual(execution.returncode, 4, execution)
        self.assertEqual(json.loads(execution.stdout)["error"]["code"], "execution_failed")

        self.workspace.write_config()
        self.workspace.refresh()
        catalog = json.loads(self.workspace.catalog_path.read_text(encoding="utf-8"))
        entry = next(item for item in catalog["capabilities"] if item["id"] == "fixture.search_messages")
        entry["input_schema"]["properties"]["query"]["maxLength"] = 99
        self.workspace.catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
        verification = self.workspace.run_cli("status")
        self.assertEqual(verification.returncode, 5, verification)
        self.assertEqual(json.loads(verification.stdout)["error"]["code"], "verification_failed")

    def test_router_audits_allowed_and_denied_actions(self) -> None:
        self.workspace.refresh()
        self.workspace.clear_fixture_observation()
        with self.workspace.session(run_id="audit-correlation-42") as session:
            session.initialize()
            _, mcp_search = session.tool_document({"action": "search", "query": "search messages"})
            mcp_id = find_match(mcp_search, ".search_messages")["id"]
            session.tool_document({"action": "describe", "capability_id": mcp_id})
            _, skill_search = session.tool_document(
                {"action": "search", "query": "cobalt archive lookup", "kinds": ["skill"]}
            )
            skill_id = find_match(skill_search, ".message-reader")["id"]
            session.tool_document({"action": "load_skill", "capability_id": skill_id})
            session.call({"action": "call", "capability_id": mcp_id, "arguments": {"query": "audit"}})
            session.assert_tool_error(
                self,
                {
                    "action": "call",
                    "capability_id": "fixture.send_message",
                    "arguments": {"channel": "x", "text": "denied"},
                },
                "policy_denied",
            )

        audit = read_json_lines(self.workspace.audit_path)
        actions = [entry["action"] for entry in audit]
        self.assertEqual(actions, ["search", "describe", "search", "load_skill", "call", "call"])
        self.assertTrue(all(entry["run_id"] == "audit-correlation-42" for entry in audit), audit)
        self.assertEqual(audit[-2]["outcome"], "allowed")
        self.assertEqual(audit[-1]["outcome"], "denied")
        self.assertEqual(stat.S_IMODE(self.workspace.audit_path.stat().st_mode), 0o600)
        fixture_calls = read_json_lines(self.workspace.state / "calls.jsonl")
        self.assertEqual([entry["tool"] for entry in fixture_calls], ["search_messages"])

    def test_audit_write_failure_does_not_replace_a_completed_result(self) -> None:
        self.workspace.refresh()
        session = McpSession(
            self.workspace,
            run_id="broken-audit",
            audit_path=self.workspace.root,
        )
        try:
            session.start()
            session.initialize()
            response, document = session.tool_document(
                {"action": "search", "query": "search messages"}
            )
        finally:
            session.close()

        self.assertFalse(response["result"].get("isError", False), response)
        self.assertTrue(matches(document), document)
        self.assertEqual(
            session.stderr_bytes,
            b"capability-router: audit log write failed\n",
        )

    def test_rejected_arguments_do_not_leak_into_audit_records(self) -> None:
        secret = "REJECTED-AUDIT-SECRET-5d9e"
        self.workspace.refresh()
        with self.workspace.session(run_id="audit-redaction") as session:
            session.initialize()
            session.call({"action": {"token": secret}, "capability_id": secret})
            session.call(
                {
                    "action": "call",
                    "capability_id": {"token": secret},
                    "arguments": {},
                }
            )

        raw_audit = self.workspace.audit_path.read_text(encoding="utf-8")
        self.assertNotIn(secret, raw_audit)
        audit = read_json_lines(self.workspace.audit_path)
        self.assertEqual([entry["action"] for entry in audit], ["invalid", "call"])
        self.assertTrue(all("capability_id" not in entry for entry in audit), audit)


if __name__ == "__main__":
    import unittest

    unittest.main()
