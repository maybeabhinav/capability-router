from __future__ import annotations

from contextlib import redirect_stderr
import io
import json
import fcntl
import os
from unittest import mock

from capability_router.catalog import load_catalog, refresh_catalog
from capability_router.config import AccessLevel, load_config
from capability_router.errors import RouterError
from capability_router.executor import Executor
from capability_router.server import Server
from capability_router.util import atomic_write, canonical_bytes, sha256_bytes
from tests.support import McpSession, RouterTestCase, read_json_lines


class AutoRefreshTests(RouterTestCase):
    def call_arguments(self, query: str = "safe") -> dict:
        return {
            "action": "call",
            "capability_id": "fixture.search_messages",
            "arguments": {"query": query},
        }

    def set_schema(self, value: str) -> None:
        (self.workspace.state / "fixture.schema-mode").write_text(value, encoding="utf-8")

    def set_mode(self, value: str) -> None:
        (self.workspace.state / "fixture.mode").write_text(value, encoding="utf-8")

    def refresh_records(self) -> list[dict]:
        return [
            entry for entry in read_json_lines(self.workspace.audit_path)
            if entry["action"] == "auto_refresh"
        ]

    def test_refresh_preserves_other_sources_and_reuses_updated_catalog(self) -> None:
        self.workspace.write_config({
            "fixture": self.workspace.server(),
            "other": self.workspace.server("other"),
        })
        with self.ready_session() as session:
            before = json.loads(self.workspace.catalog_path.read_bytes())
            self.workspace.clear_fixture_observation()
            self.set_schema("changed")
            for query in ("safe", "x" * 51):
                response, document = session.tool_document(self.call_arguments(query))
                self.assertNotIn("isError", response["result"], document)
                self.assertIn(f"matches:{query}", str(document))
            _, described = session.tool_document({
                "action": "describe", "capability_id": "fixture.search_messages"
            })
            self.assertEqual(
                described["capability"]["input_schema"]["properties"]["query"]["maxLength"], 51
            )
        after = json.loads(self.workspace.catalog_path.read_bytes())
        def unaffected(catalog):
            return [item for item in catalog["capabilities"]
                    if item["kind"] != "mcp_tool" or item["source"] != "fixture"]
        self.assertEqual(unaffected(before), unaffected(after))
        self.assertEqual({item["server"] for item in read_json_lines(
            self.workspace.state / "starts.jsonl"
        )}, {"fixture"})
        self.assertEqual(len(read_json_lines(self.workspace.state / "calls.jsonl")), 2)
        records = self.refresh_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["outcome"], "allowed")
        self.assertEqual(records[0]["capability_id"], "fixture.search_messages")

    def test_incompatible_arguments_include_new_schema_and_allow_retry(self) -> None:
        with self.ready_session() as session:
            self.workspace.clear_fixture_observation()
            self.set_schema("incompatible")
            document = session.assert_tool_error(
                self, self.call_arguments(), "schema_validation_failed"
            )
            schema = document["error"]["details"]["input_schema"]
            self.assertEqual(schema["properties"]["query"]["maxLength"], 3)
            self.assertEqual(read_json_lines(self.workspace.state / "calls.jsonl"), [])
            response, document = session.tool_document(self.call_arguments("ok"))
            self.assertNotIn("isError", response["result"], document)
        self.assertEqual(len(self.refresh_records()), 1)
        self.assertEqual(len(read_json_lines(self.workspace.state / "calls.jsonl")), 1)

    def test_running_server_reloads_external_refresh_without_restart(self) -> None:
        with self.ready_session() as session:
            session.tool_document({"action": "status"})
            self.set_schema("changed")
            self.workspace.refresh()
            self.workspace.clear_fixture_observation()
            _, document = session.tool_document({
                "action": "describe", "capability_id": "fixture.search_messages"
            })
            self.assertEqual(
                document["capability"]["input_schema"]["properties"]["query"]["maxLength"], 51
            )
            response, document = session.tool_document(self.call_arguments("x" * 51))
            self.assertNotIn("isError", response["result"], document)
        self.assertEqual(self.refresh_records(), [])
        self.assertEqual(len(read_json_lines(self.workspace.state / "calls.jsonl")), 1)

    def test_large_incompatible_schema_is_available_in_error_artifact(self) -> None:
        for limit in (1024, 12000):
            with self.subTest(output_limit=limit):
                self.workspace.output_limit = limit
                self.workspace.write_config()
                self.set_schema("normal")
                with self.ready_session() as session:
                    self.workspace.clear_fixture_observation()
                    self.set_schema("large-incompatible")
                    response, document = session.tool_document(self.call_arguments())
                    self.assertTrue(response["result"]["isError"])
                    self.assertEqual(document["error"]["code"], "schema_validation_failed")
                    self.assertLessEqual(len(canonical_bytes(response["result"])), limit)
                    details = document["error"]["details"]
                    payload = (self.workspace.root / details["artifact_path"]).read_bytes()
                    self.assertEqual(sha256_bytes(payload), details["sha256"])
                    stored = json.loads(payload)
                    schema = stored["error"]["details"]["input_schema"]
                    self.assertEqual(schema["properties"]["query"]["maxLength"], 3)
                    self.assertEqual(len(schema["description"]), 7000)
                    self.assertEqual(read_json_lines(self.workspace.state / "calls.jsonl"), [])
                    response, document = session.tool_document(self.call_arguments("ok"))
                    self.assertNotIn("isError", response["result"], document)

    def test_schema_artifact_failure_keeps_a_bounded_error(self) -> None:
        for failure in ("write", "metadata-size"):
            with self.subTest(failure=failure):
                config = self.workspace.config()
                config["output_limit_bytes"] = 1024
                if failure == "write":
                    self.workspace.artifact_dir.write_text("blocked", encoding="utf-8")
                else:
                    config["artifact_dir"] = "/".join(["long-artifact-path" * 8] * 6)
                self.workspace.config_path.write_text(json.dumps(config), encoding="utf-8")
                self.set_schema("normal")
                with self.ready_session() as session:
                    self.workspace.clear_fixture_observation()
                    self.set_schema("large-incompatible")
                    response, document = session.tool_document(self.call_arguments())
                self.assertTrue(response["result"]["isError"])
                self.assertEqual(document["error"]["code"], "schema_validation_failed")
                self.assertEqual(document["error"]["details"], {"truncated": True})
                self.assertNotIn("completed", document)
                self.assertLessEqual(len(canonical_bytes(response["result"])), 1024)
                self.assertEqual(read_json_lines(self.workspace.state / "calls.jsonl"), [])

    def test_external_addition_keeps_audit_identity_on_success_and_error(self) -> None:
        requests = (
            {"action": "describe", "capability_id": "fixture.search_messages"},
            self.call_arguments(),
            self.call_arguments(""),
        )
        for arguments in requests:
            with self.subTest(arguments=arguments):
                self.workspace.refresh()
                catalog = json.loads(self.workspace.catalog_path.read_bytes())
                catalog["capabilities"] = [item for item in catalog["capabilities"]
                                           if item["id"] != "fixture.search_messages"]
                atomic_write(self.workspace.catalog_path, canonical_bytes(catalog))
                with self.workspace.session() as session:
                    session.initialize()
                    self.workspace.refresh()
                    response, document = session.tool_document(arguments)
                    if arguments.get("arguments") == {"query": ""}:
                        self.assertEqual(document["error"]["code"], "schema_validation_failed")
                    else:
                        self.assertNotIn("isError", response["result"], document)
                record = read_json_lines(self.workspace.audit_path)[-1]
                self.assertEqual(record["action"], arguments["action"])
                self.assertEqual(record["capability_id"], "fixture.search_messages")

    def test_atomic_replacement_with_same_size_and_mtime_is_reloaded(self) -> None:
        with self.ready_session() as session:
            session.tool_document({"action": "status"})
            original = self.workspace.catalog_path.stat()
            payload = self.workspace.catalog_path.read_bytes()
            replacement = payload.replace(b"Search messages", b"Locate messages")
            self.assertNotEqual(payload, replacement)
            atomic_write(self.workspace.catalog_path, replacement)
            os.utime(self.workspace.catalog_path, ns=(original.st_atime_ns, original.st_mtime_ns))
            _, document = session.tool_document({
                "action": "describe", "capability_id": "fixture.search_messages"
            })
            self.assertIn("Locate messages", document["capability"]["description"])

    def test_missing_tool_refreshes_once_then_returns_refresh_required(self) -> None:
        with self.ready_session() as session:
            self.workspace.clear_fixture_observation()
            self.set_mode("missing")
            session.assert_tool_error(self, self.call_arguments(), "refresh_required")
        self.assertEqual(len(self.refresh_records()), 1)
        self.assertEqual(read_json_lines(self.workspace.state / "calls.jsonl"), [])
        self.assertEqual(len(read_json_lines(self.workspace.state / "lists.jsonl")), 4)
        self.assertEqual(
            read_json_lines(self.workspace.audit_path)[-1]["capability_id"],
            "fixture.search_messages",
        )

    def test_transient_missing_tool_recovers_after_one_refresh(self) -> None:
        with self.ready_session() as session:
            self.workspace.clear_fixture_observation()
            self.set_mode("missing-once")
            response, document = session.tool_document(self.call_arguments())
            self.assertNotIn("isError", response["result"], document)
        self.assertEqual(len(self.refresh_records()), 1)
        self.assertEqual(len(read_json_lines(self.workspace.state / "calls.jsonl")), 1)

    def test_repeated_drift_does_not_loop_or_invoke_tool(self) -> None:
        with self.ready_session() as session:
            self.workspace.clear_fixture_observation()
            self.set_schema("changing")
            session.assert_tool_error(self, self.call_arguments(), "refresh_required")
        self.assertEqual(len(self.refresh_records()), 1)
        self.assertEqual(len(read_json_lines(self.workspace.state / "lists.jsonl")), 4)
        self.assertEqual(read_json_lines(self.workspace.state / "calls.jsonl"), [])

    def test_failed_refresh_is_audited_and_does_not_call_tool(self) -> None:
        with self.ready_session() as session:
            self.workspace.clear_fixture_observation()
            self.set_schema("changed")
            self.set_mode("refresh-fail")
            session.assert_tool_error(self, self.call_arguments(), "refresh_required")
        records = self.refresh_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["outcome"], "error")
        self.assertEqual(records[0]["code"], "refresh_required")
        self.assertEqual(read_json_lines(self.workspace.state / "calls.jsonl"), [])

    def test_refresh_lock_wait_uses_call_deadline(self) -> None:
        with self.ready_session() as session:
            self.workspace.clear_fixture_observation()
            self.set_schema("changed")
            lock_path = self.workspace.catalog_path.with_name("catalog.json.lock")
            with lock_path.open("a+b") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                session.assert_tool_error(self, self.call_arguments(), "timeout")
        self.assertEqual(self.refresh_records()[0]["code"], "timeout")
        self.assertEqual(read_json_lines(self.workspace.state / "calls.jsonl"), [])

    def test_audit_failure_preserves_refresh_success_and_original_error(self) -> None:
        for mode in ("normal", "refresh-fail"):
            with self.subTest(mode=mode):
                self.set_schema("normal")
                self.set_mode("normal")
                self.workspace.refresh()
                self.workspace.clear_fixture_observation()
                self.set_schema("changed")
                self.set_mode(mode)
                session = McpSession(self.workspace, run_id="broken-audit",
                                     audit_path=self.workspace.root)
                try:
                    session.start()
                    session.initialize()
                    response, document = session.tool_document(self.call_arguments())
                finally:
                    session.close()
                calls = read_json_lines(self.workspace.state / "calls.jsonl")
                if mode == "normal":
                    self.assertNotIn("isError", response["result"], document)
                    self.assertEqual(len(calls), 1)
                else:
                    self.assertEqual(document["error"]["code"], "refresh_required")
                    self.assertEqual(calls, [])
                self.assertEqual(session.stderr_bytes,
                                 b"capability-router: audit log write failed\n")

    def test_concurrent_server_refreshes_preserve_both_updates(self) -> None:
        self.workspace.timeout = 3
        self.workspace.write_config({
            "fixture": self.workspace.server(),
            "other": self.workspace.server("other"),
        })
        with self.ready_session() as first, self.workspace.session() as second:
            second.initialize()
            for session in (first, second):
                session.tool_document({"action": "status"})
            self.set_schema("changed")
            (self.workspace.state / "other.schema-mode").write_text("changed")
            for session, name in ((first, "fixture"), (second, "other")):
                session.send({
                    "jsonrpc": "2.0", "id": 100, "method": "tools/call",
                    "params": {"name": "capability", "arguments": {
                        **self.call_arguments(), "capability_id": f"{name}.search_messages",
                    }},
                })
            for session in (first, second):
                response = session.receive(timeout=5)
                self.assertNotIn("isError", response["result"], response)
        catalog = json.loads(self.workspace.catalog_path.read_bytes())
        schemas = [item["input_schema"] for item in catalog["capabilities"]
                   if item["kind"] == "mcp_tool" and item["name"] == "search_messages"]
        self.assertEqual(len(schemas), 2)
        self.assertTrue(all(schema["properties"]["query"]["maxLength"] == 51
                            for schema in schemas))
        self.assertEqual(len(self.refresh_records()), 2)

    def test_transient_audit_failure_warns_even_when_call_audit_succeeds(self) -> None:
        self.workspace.refresh()
        self.workspace.clear_fixture_observation()
        self.set_schema("changed")
        config = load_config(self.workspace.config_path)
        server = Server(config, run_id="transient-audit", audit_path=self.workspace.audit_path)
        server.output = io.BytesIO()
        audit = server.executor.audit

        def fail_refresh_audit(action, *args, **kwargs):
            if action == "auto_refresh":
                raise OSError("temporary audit failure")
            return audit(action, *args, **kwargs)

        stderr = io.StringIO()
        with (
            mock.patch.dict(os.environ, self.workspace.environment),
            mock.patch.object(server.executor, "audit", side_effect=fail_refresh_audit),
            mock.patch("capability_router.executor._audit_warning_emitted", False),
            redirect_stderr(stderr),
        ):
            server._tool_call(1, {"name": "capability", "arguments": self.call_arguments()})
        response = json.loads(server.output.getvalue())
        self.assertNotIn("isError", response["result"], response)
        self.assertEqual(len(read_json_lines(self.workspace.state / "calls.jsonl")), 1)
        self.assertEqual([record["action"] for record in read_json_lines(self.workspace.audit_path)],
                         ["call"])
        self.assertEqual(stderr.getvalue(), "capability-router: audit log write failed\n")

    def test_reload_keeps_configured_read_only_policy(self) -> None:
        with self.ready_session() as session:
            catalog = json.loads(self.workspace.catalog_path.read_bytes())
            for item in catalog["capabilities"]:
                if item["id"] == "fixture.send_message":
                    item["access"] = "read"
            atomic_write(self.workspace.catalog_path, canonical_bytes(catalog))
            self.workspace.clear_fixture_observation()
            session.assert_tool_error(self, {
                "action": "call", "capability_id": "fixture.send_message",
                "arguments": {"channel": "ops", "text": "blocked"},
            }, "policy_denied")
        self.assertEqual(read_json_lines(self.workspace.state / "starts.jsonl"), [])

    def test_policy_is_checked_again_after_refresh(self) -> None:
        self.workspace.refresh()
        self.workspace.clear_fixture_observation()
        self.set_schema("changed")
        config = load_config(self.workspace.config_path)
        catalog, _ = load_catalog(config)
        executor = Executor(config, catalog, run_id="policy", audit_path=self.workspace.audit_path)

        def refresh_and_change_policy(*args, **kwargs):
            result = refresh_catalog(*args, **kwargs)
            config.servers["fixture"]["access_overrides"]["search_messages"] = AccessLevel.WRITE
            return result

        with mock.patch.dict(os.environ, self.workspace.environment), mock.patch(
            "capability_router.executor.refresh_catalog", side_effect=refresh_and_change_policy
        ):
            with self.assertRaises(RouterError) as caught:
                executor.execute(self.call_arguments())
        self.assertEqual(caught.exception.code, "policy_denied")
        self.assertEqual(len(self.refresh_records()), 1)
        self.assertEqual(read_json_lines(self.workspace.state / "calls.jsonl"), [])
