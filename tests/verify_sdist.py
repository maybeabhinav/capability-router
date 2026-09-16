#!/usr/bin/env python3
"""Build the source archive and run its bundled contract suite."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _safe_extract(archive: tarfile.TarFile, destination: Path) -> None:
    root = destination.resolve()
    for member in archive.getmembers():
        target = (destination / member.name).resolve()
        if not target.is_relative_to(root):
            raise ValueError(f"unsafe source archive member: {member.name}")
    archive.extractall(destination)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="capability-router-sdist-") as directory:
        workspace = Path(directory)
        source = workspace / "source"
        shutil.copytree(
            PROJECT_ROOT,
            source,
            ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", "*.egg-info", "dist"),
        )
        build = subprocess.run(
            [
                sys.executable,
                "-c",
                'from setuptools.build_meta import build_sdist; build_sdist("dist")',
            ],
            cwd=source,
            check=False,
        )
        if build.returncode != 0:
            return build.returncode
        archive_path = next((source / "dist").glob("*.tar.gz"))
        extracted = workspace / "extracted"
        extracted.mkdir()
        with tarfile.open(archive_path) as archive:
            _safe_extract(archive, extracted)
        package_root = next(extracted.iterdir())
        completed = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
            cwd=package_root,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            check=False,
        )
        return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
