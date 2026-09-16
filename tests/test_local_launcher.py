from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests.support import FIXTURE_SERVER, read_json_lines


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = [sys.executable, "-m", "capability_router.local_launcher"]


class LocalLauncherTests(unittest.TestCase):
    def test_refresh_loads_private_environment_without_printing_values(self) -> None:
        with tempfile.TemporaryDirectory(prefix="router-launcher-test-") as raw:
            root = Path(raw)
            state = root / "state"
            state.mkdir()
            config = root / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "mode": "read-only",
                        "catalog_path": str(root / "catalog.json"),
                        "artifact_dir": str(root / "artifacts"),
                        "output_limit_bytes": 12000,
                        "call_timeout_seconds": 1,
                        "input_limit_bytes": 1048576,
                        "skill_roots": [],
                        "servers": {
                            "fixture": {
                                "transport": "stdio",
                                "command": sys.executable,
                                "args": [str(FIXTURE_SERVER)],
                                "cwd": str(root),
                                "environment_from_parent": {
                                    "FIXTURE_STATE_DIR": "ROUTER_LAUNCHER_STATE",
                                    "FIXTURE_SECRET": "ROUTER_PRIVATE_TEST",
                                },
                                "default_access": "read",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            secret = "launcher-secret-must-not-appear"
            env_file = root / "environment.json"
            env_file.write_text(json.dumps({"ROUTER_PRIVATE_TEST": secret}), encoding="utf-8")
            os.chmod(env_file, 0o600)

            completed = subprocess.run(
                [*LAUNCHER, "refresh", "--config", str(config), "--env-file", str(env_file)],
                cwd=PROJECT_ROOT,
                env={**os.environ, "ROUTER_LAUNCHER_STATE": str(state)},
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed)
            self.assertGreater(json.loads(completed.stdout)["capability_count"], 0)
            starts = read_json_lines(state / "starts.jsonl")
            self.assertTrue(starts)
            self.assertIs(starts[0]["secret_present"], True)
            self.assertNotIn(secret, completed.stdout + completed.stderr)

    def test_rejects_group_readable_environment_file_without_exposing_value(self) -> None:
        with tempfile.TemporaryDirectory(prefix="router-launcher-test-") as raw:
            root = Path(raw)
            secret = "insecure-secret-must-not-appear"
            env_file = root / "environment.json"
            env_file.write_text(json.dumps({"ROUTER_PRIVATE_TEST": secret}), encoding="utf-8")
            os.chmod(env_file, 0o640)

            completed = subprocess.run(
                [*LAUNCHER, "status", "--config", str(root / "missing.json"), "--env-file", str(env_file)],
                cwd=PROJECT_ROOT,
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("mode 0600", completed.stderr)
            self.assertNotIn(secret, completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
