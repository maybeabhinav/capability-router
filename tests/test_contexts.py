from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CLI = [sys.executable, "-m", "capability_router.cli"]


def write_router_config(root: Path, name: str) -> Path:
    directory = root / name
    directory.mkdir()
    catalog = directory / "catalog.json"
    catalog.write_text(
        json.dumps(
            {
                "version": 1,
                "generated_at": "2026-09-17T00:00:00Z",
                "capabilities": [
                    {
                        "id": "shared.review",
                        "kind": "skill",
                        "name": "review",
                        "description": "Review code.",
                        "source": "shared",
                        "access": "read",
                        "availability": "available",
                    },
                    {
                        "id": "office.linear-search",
                        "kind": "skill",
                        "name": "linear-search",
                        "description": "Search Linear.",
                        "source": "office",
                        "access": "read",
                        "availability": "available",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    config = directory / "config.json"
    config.write_text(
        json.dumps(
            {
                "mode": "read-only",
                "catalog_path": str(catalog),
                "artifact_dir": str(directory / "artifacts"),
                "output_limit_bytes": 12000,
                "call_timeout_seconds": 5,
                "input_limit_bytes": 1048576,
                "skill_roots": [],
                "servers": {},
            }
        ),
        encoding="utf-8",
    )
    return config


class ContextCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="router-context-test-")
        self.root = Path(self.temporary.name)
        self.registry = self.root / "contexts.json"
        self.personal = write_router_config(self.root, "personal")
        self.office = write_router_config(self.root, "office")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*CLI, "context", "--registry", str(self.registry), *arguments],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )

    def create_contexts(self) -> None:
        personal = self.run_cli(
            "create",
            "personal",
            "--config",
            str(self.personal),
            "--source",
            "shared",
        )
        office = self.run_cli(
            "create",
            "office",
            "--config",
            str(self.office),
            "--source",
            "shared",
            "--source",
            "office",
        )
        self.assertEqual(personal.returncode, 0, personal)
        self.assertEqual(office.returncode, 0, office)

    def test_contexts_are_created_listed_and_selected_per_session(self) -> None:
        self.create_contexts()

        selected = self.run_cli("use", "personal", "--session", "session-a")
        listing = self.run_cli("list")
        current = self.run_cli("current", "--session", "session-a")

        self.assertEqual(selected.returncode, 0, selected)
        self.assertEqual(current.returncode, 0, current)
        self.assertEqual(json.loads(current.stdout)["context"], "personal")
        self.assertEqual(
            [item["id"] for item in json.loads(listing.stdout)["contexts"]],
            ["office", "personal"],
        )
        self.assertEqual(self.registry.stat().st_mode & 0o777, 0o600)

    def test_sessions_switch_independently(self) -> None:
        self.create_contexts()
        errors: list[str] = []
        lock = threading.Lock()

        def select(session: str, context: str) -> None:
            completed = self.run_cli("use", context, "--session", session)
            if completed.returncode:
                with lock:
                    errors.append(completed.stdout + completed.stderr)

        workers = [
            threading.Thread(target=select, args=("session-personal", "personal")),
            threading.Thread(target=select, args=("session-office", "office")),
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()

        personal = json.loads(
            self.run_cli("current", "--session", "session-personal").stdout
        )
        office = json.loads(
            self.run_cli("current", "--session", "session-office").stdout
        )
        self.assertEqual(errors, [])
        self.assertEqual(personal["context"], "personal")
        self.assertEqual(office["context"], "office")

    def test_move_is_atomic_explainable_and_undoable(self) -> None:
        self.create_contexts()

        moved = self.run_cli(
            "move",
            "office.linear-search",
            "--from",
            "office",
            "--to",
            "personal",
        )
        self.assertEqual(moved.returncode, 0, moved)
        move_document = json.loads(moved.stdout)

        office = json.loads(
            self.run_cli(
                "explain",
                "office.linear-search",
                "--context",
                "office",
            ).stdout
        )
        personal = json.loads(
            self.run_cli(
                "explain",
                "office.linear-search",
                "--context",
                "personal",
            ).stdout
        )
        self.assertIs(office["available"], False)
        self.assertIs(personal["available"], True)

        undone = self.run_cli("undo", move_document["change_id"])
        self.assertEqual(undone.returncode, 0, undone)
        restored = json.loads(
            self.run_cli(
                "explain",
                "office.linear-search",
                "--context",
                "office",
            ).stdout
        )
        self.assertIs(restored["available"], True)

    def test_share_unassign_history_and_doctor(self) -> None:
        self.create_contexts()

        shared = self.run_cli(
            "share",
            "office.linear-search",
            "--to",
            "personal",
        )
        personal = json.loads(
            self.run_cli(
                "explain",
                "office.linear-search",
                "--context",
                "personal",
            ).stdout
        )
        office_before = json.loads(
            self.run_cli(
                "explain",
                "office.linear-search",
                "--context",
                "office",
            ).stdout
        )
        unassigned = self.run_cli(
            "unassign",
            "office.linear-search",
            "--context",
            "office",
        )
        office_after = json.loads(
            self.run_cli(
                "explain",
                "office.linear-search",
                "--context",
                "office",
            ).stdout
        )
        history = self.run_cli("history", "--limit", "2")
        doctor = self.run_cli("doctor")

        self.assertEqual(shared.returncode, 0, shared)
        self.assertEqual(unassigned.returncode, 0, unassigned)
        self.assertIs(personal["available"], True)
        self.assertIs(office_before["available"], True)
        self.assertIs(office_after["available"], False)
        self.assertEqual(
            [item["operation"] for item in json.loads(history.stdout)["changes"]],
            ["share", "unassign"],
        )
        self.assertIs(json.loads(doctor.stdout)["healthy"], True)

    def test_exec_uses_context_environment_for_cli_identity(self) -> None:
        environment = self.root / "personal-environment.json"
        environment.write_text(
            json.dumps({"ROUTER_CONTEXT_MARKER": "personal-account"}),
            encoding="utf-8",
        )
        environment.chmod(0o600)
        created = self.run_cli(
            "create",
            "personal-cli",
            "--config",
            str(self.personal),
            "--source",
            "shared",
            "--environment-file",
            str(environment),
        )
        executed = self.run_cli(
            "exec",
            "personal-cli",
            "--",
            sys.executable,
            "-c",
            (
                "import os; "
                "print(os.environ.get('ROUTER_CONTEXT_MARKER', 'missing'))"
            ),
        )

        self.assertEqual(created.returncode, 0, created)
        self.assertEqual(executed.returncode, 0, executed)
        self.assertEqual(executed.stdout.strip(), "personal-account")

    def test_move_rejects_stale_registry_revision(self) -> None:
        self.create_contexts()
        registry = json.loads(self.registry.read_text(encoding="utf-8"))
        stale_revision = registry["revision"] - 1

        completed = self.run_cli(
            "move",
            "office.linear-search",
            "--from",
            "office",
            "--to",
            "personal",
            "--expected-revision",
            str(stale_revision),
        )

        self.assertEqual(completed.returncode, 2, completed)
        self.assertEqual(
            json.loads(completed.stdout)["error"]["code"],
            "invalid_input",
        )


if __name__ == "__main__":
    unittest.main()
