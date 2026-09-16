from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from capability_router.util import atomic_write


class AtomicWriteTests(unittest.TestCase):
    def test_replace_failure_does_not_close_a_reused_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.mkdir()
            survivor = root / "survivor"
            reused: list[int] = []

            def fail_after_reuse(source: str | bytes | os.PathLike[str], destination: str | bytes | os.PathLike[str]) -> None:
                reused.append(os.open(survivor, os.O_WRONLY | os.O_CREAT, 0o600))
                raise IsADirectoryError(str(destination))

            with mock.patch("capability_router.util.os.replace", side_effect=fail_after_reuse):
                with self.assertRaises(IsADirectoryError):
                    atomic_write(target, b"payload")

            try:
                os.fstat(reused[0])
            finally:
                try:
                    os.close(reused[0])
                except OSError:
                    pass


if __name__ == "__main__":
    unittest.main()
