from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import unittest

from tests.support import CLI, McpSession, RouterTestCase, read_json_lines


class ContextRuntimeTests(RouterTestCase):
    def setUp(self) -> None:
        super().setUp()
        config = self.workspace.config()
        config["skill_roots"][0]["name"] = "skills"
        self.workspace.config_path.write_text(
            json.dumps(config, indent=2),
            encoding="utf-8",
        )
        self.workspace.refresh()
        self.registry = self.workspace.root / "contexts.json"
        self._context_cli(
            "create",
            "personal",
            "--config",
            str(self.workspace.config_path),
            "--source",
            "skills",
        )
        self._context_cli(
            "create",
            "office",
            "--config",
            str(self.workspace.config_path),
            "--source",
            "fixture",
        )
        moved = self._context_cli(
            "move",
            "fixture.search_messages",
            "--from",
            "office",
            "--to",
            "personal",
        )
        self.assertEqual(moved.returncode, 0, moved)
        self.move_change_id = json.loads(moved.stdout)["change_id"]
        selected = self._context_cli("use", "office", "--session", "agent-one")
        self.assertEqual(selected.returncode, 0, selected)

    def _context_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            [
                *CLI,
                "context",
                "--registry",
                str(self.registry),
                *arguments,
            ],
            cwd=Path(__file__).resolve().parents[1],
            env=self.workspace.environment,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        if completed.returncode and arguments[0] != "move":
            raise AssertionError(completed.stdout + completed.stderr)
        return completed

    def test_agent_moves_and_undoes_capability_assignment(self) -> None:
        session = McpSession(
            self.workspace,
            run_id="context-agent-move",
            serve_arguments=[
                "--registry",
                str(self.registry),
                "--session",
                "agent-one",
            ],
        )
        try:
            session.start()
            session.initialize()
            _, hidden = session.tool_document(
                {
                    "action": "context_explain",
                    "context": "office",
                    "capability_id": "fixture.search_messages",
                }
            )
            _, undone = session.tool_document(
                {
                    "action": "context_undo",
                    "change_id": self.move_change_id,
                }
            )
            _, restored = session.tool_document(
                {
                    "action": "context_explain",
                    "context": "office",
                    "capability_id": "fixture.search_messages",
                }
            )
            _, moved = session.tool_document(
                {
                    "action": "context_move",
                    "capability_id": "fixture.search_messages",
                    "source_context": "office",
                    "target_context": "personal",
                    "expected_revision": undone["revision"],
                }
            )
            _, hidden_again = session.tool_document(
                {
                    "action": "context_explain",
                    "context": "office",
                    "capability_id": "fixture.search_messages",
                }
            )
        finally:
            session.close()

        self.assertIs(hidden["available"], False)
        self.assertIs(restored["available"], True)
        self.assertEqual(moved["status"], "moved")
        self.assertIs(hidden_again["available"], False)


    def test_agent_shares_and_unassigns_capability(self) -> None:
        session = McpSession(
            self.workspace,
            run_id="context-agent-share",
            serve_arguments=[
                "--registry",
                str(self.registry),
                "--session",
                "agent-one",
            ],
        )
        try:
            session.start()
            session.initialize()
            _, shared = session.tool_document(
                {
                    "action": "context_share",
                    "capability_id": "fixture.send_message",
                    "target_context": "personal",
                }
            )
            _, available = session.tool_document(
                {
                    "action": "context_explain",
                    "context": "personal",
                    "capability_id": "fixture.send_message",
                }
            )
            _, unassigned = session.tool_document(
                {
                    "action": "context_unassign",
                    "capability_id": "fixture.send_message",
                    "context": "personal",
                    "expected_revision": shared["revision"],
                }
            )
            _, hidden = session.tool_document(
                {
                    "action": "context_explain",
                    "context": "personal",
                    "capability_id": "fixture.send_message",
                }
            )
        finally:
            session.close()

        self.assertEqual(shared["status"], "shared")
        self.assertIs(available["available"], True)
        self.assertEqual(unassigned["status"], "unassigned")
        self.assertIs(hidden["available"], False)


    def test_context_switches_downstream_environment_without_leakage(self) -> None:
        environment_file = self.workspace.root / "context-environment.json"
        environment_file.write_text(
            json.dumps({"ROUTER_TEST_SECRET": "context-only-marker"}),
            encoding="utf-8",
        )
        environment_file.chmod(0o600)
        self._context_cli(
            "create",
            "secret",
            "--config",
            str(self.workspace.config_path),
            "--source",
            "fixture",
            "--environment-file",
            str(environment_file),
        )
        self._context_cli(
            "create",
            "plain",
            "--config",
            str(self.workspace.config_path),
            "--source",
            "fixture",
        )
        self._context_cli("use", "secret", "--session", "agent-environment")
        self.workspace.clear_fixture_observation()
        session = McpSession(
            self.workspace,
            run_id="context-agent-environment",
            serve_arguments=[
                "--registry",
                str(self.registry),
                "--session",
                "agent-environment",
            ],
        )
        try:
            session.start()
            session.initialize()
            session.tool_document(
                {
                    "action": "call",
                    "capability_id": "fixture.search_messages",
                    "arguments": {"query": "secret-context"},
                }
            )
            session.tool_document(
                {"action": "context_use", "context": "plain"}
            )
            session.tool_document(
                {
                    "action": "call",
                    "capability_id": "fixture.search_messages",
                    "arguments": {"query": "plain-context"},
                }
            )
        finally:
            session.close()

        starts = read_json_lines(self.workspace.state / "starts.jsonl")
        self.assertEqual(
            [item["secret_present"] for item in starts],
            [True, False],
        )
        self.assertNotIn("context-only-marker", json.dumps(starts))

    def test_agent_switches_context_and_search_uses_new_catalog(self) -> None:
        session = McpSession(
            self.workspace,
            run_id="context-agent",
            serve_arguments=[
                "--registry",
                str(self.registry),
                "--session",
                "agent-one",
            ],
        )
        try:
            session.start()
            session.initialize()
            _, current = session.tool_document({"action": "context_current"})
            _, office_search = session.tool_document(
                {"action": "search", "query": "search messages"}
            )
            _, switched = session.tool_document(
                {"action": "context_use", "context": "personal"}
            )
            _, personal_search = session.tool_document(
                {"action": "search", "query": "search messages"}
            )
        finally:
            session.close()

        self.assertEqual(current["context"], "office")
        self.assertNotIn(
            "fixture.search_messages",
            {item["id"] for item in office_search["matches"]},
        )
        self.assertEqual(switched["context"], "personal")
        self.assertIn(
            "fixture.search_messages",
            {item["id"] for item in personal_search["matches"]},
        )


if __name__ == "__main__":
    unittest.main()
