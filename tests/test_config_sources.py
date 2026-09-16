from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from capability_router.config import load_config
from capability_router.errors import InputError


class ConfigSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def write_config(self, *, sources: list[dict[str, object]], servers: dict[str, object] | None = None) -> Path:
        path = self.root / "router.json"
        path.write_text(
            json.dumps(
                {
                    "mode": "read-only",
                    "catalog_path": "catalog.json",
                    "artifact_dir": "artifacts",
                    "output_limit_bytes": 4096,
                    "input_limit_bytes": 4096,
                    "call_timeout_seconds": 5,
                    "skill_roots": [],
                    "server_sources": sources,
                    "servers": servers or {},
                }
            ),
            encoding="utf-8",
        )
        return path

    @staticmethod
    def server(command: str = "example") -> dict[str, object]:
        return {"transport": "stdio", "command": command, "default_access": "read"}

    def test_loads_servers_from_named_source_and_skips_explicit_optional_absence(self) -> None:
        package = self.root / "company-servers.json"
        package.write_text(json.dumps({"servers": {"company-search": self.server()}}), encoding="utf-8")
        config_path = self.write_config(
            sources=[
                {"name": "company", "path": "company-servers.json"},
                {"name": "retired", "path": "missing.json", "optional": True},
            ]
        )

        config = load_config(config_path)

        self.assertEqual(["company-search"], list(config.servers))

    def test_rejects_duplicate_server_across_inline_and_package_sources(self) -> None:
        package = self.root / "company-servers.json"
        package.write_text(json.dumps({"servers": {"shared": self.server("package")}}), encoding="utf-8")
        config_path = self.write_config(
            sources=[{"name": "company", "path": "company-servers.json"}],
            servers={"shared": self.server("inline")},
        )

        with self.assertRaisesRegex(InputError, "duplicate server shared"):
            load_config(config_path)

    def test_missing_required_server_source_fails(self) -> None:
        config_path = self.write_config(sources=[{"name": "company", "path": "missing.json"}])

        with self.assertRaisesRegex(InputError, "cannot read server source company"):
            load_config(config_path)

    def test_unhashable_enum_values_are_reported_as_input_errors(self) -> None:
        cases = [
            {"mode": []},
            {"servers": {"fixture": self.server() | {"default_access": []}}},
            {"servers": {"fixture": self.server() | {"access_overrides": {"tool": []}}}},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                config_path = self.write_config(sources=[])
                document = json.loads(config_path.read_text(encoding="utf-8"))
                document.update(changes)
                config_path.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaises(InputError):
                    load_config(config_path)


if __name__ == "__main__":
    unittest.main()
