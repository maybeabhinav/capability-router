from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from capability_router import cli
from capability_router.errors import InputError


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class InitCommandTests(unittest.TestCase):
    def run_init(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "capability_router.cli", "init", *arguments],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_init_creates_safe_config_and_refuses_silent_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "router" / "config.json"
            config.parent.mkdir(mode=0o750)

            created = self.run_init("--config", str(config), "--skill-root", "team=~/agent-skills")
            document = json.loads(config.read_text(encoding="utf-8"))
            repeated = self.run_init("--config", str(config))

            self.assertEqual(created.returncode, 0, created.stderr)
            self.assertEqual(document["mode"], "read-only")
            self.assertEqual(document["servers"], {})
            self.assertEqual(document["skill_roots"], [{"name": "team", "path": "~/agent-skills"}])
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            self.assertEqual(config.parent.stat().st_mode & 0o777, 0o750)
            self.assertEqual(repeated.returncode, 2)
            self.assertIn("already exists", repeated.stdout)

    def test_concurrent_init_without_force_has_one_winner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "router" / "config.json"
            config.parent.mkdir()
            barrier = threading.Barrier(2)
            original_create = cli.atomic_create
            results: list[str] = []
            result_lock = threading.Lock()

            def delayed_create(*args: object, **kwargs: object) -> None:
                barrier.wait(timeout=2)
                original_create(*args, **kwargs)

            def invoke(index: int) -> None:
                arguments = SimpleNamespace(
                    config=str(config),
                    force=False,
                    mode="read-only",
                    skill_root=[f"root{index}=~/skills/{index}"],
                )
                try:
                    cli.initialize(arguments)
                    outcome = "created"
                except InputError:
                    outcome = "exists"
                with result_lock:
                    results.append(outcome)

            with mock.patch("capability_router.cli.atomic_create", side_effect=delayed_create):
                workers = [threading.Thread(target=invoke, args=(index,)) for index in range(2)]
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join()

            self.assertEqual(sorted(results), ["created", "exists"])
            document = json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(len(document["skill_roots"]), 1)

    def test_init_filesystem_failure_is_a_structured_execution_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "config.json"
            target.mkdir()

            completed = self.run_init("--config", str(target), "--force")

            self.assertEqual(completed.returncode, 4, completed)
            self.assertEqual(json.loads(completed.stdout)["error"]["code"], "execution_failed")
            self.assertEqual(completed.stderr, "")


if __name__ == "__main__":
    unittest.main()
