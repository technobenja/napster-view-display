"""Unit tests for log_rotation.py — same conventions as test_settings.py:
stdlib unittest, tempfile.TemporaryDirectory(), no pytest, no new
dependency.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from display.log_rotation import rotate_if_oversized

SMALL_CAP = 100  # bytes — small enough to exercise both sides cheaply


class RotateIfOversizedTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.log_path = self.tmpdir / "display.stdout.log"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_missing_file_does_not_raise(self) -> None:
        try:
            rotate_if_oversized(self.log_path, max_bytes=SMALL_CAP)
        except Exception as exc:  # noqa: BLE001 - this is the assertion
            self.fail(f"rotate_if_oversized raised {exc!r} for a missing file")
        self.assertFalse(self.log_path.exists())

    def test_empty_file_does_not_raise_and_is_left_alone(self) -> None:
        self.log_path.write_text("")
        rotate_if_oversized(self.log_path, max_bytes=SMALL_CAP)
        self.assertTrue(self.log_path.exists())
        self.assertEqual(self.log_path.read_text(), "")
        self.assertFalse((self.tmpdir / "display.stdout.log.old").exists())

    def test_file_under_cap_is_left_alone(self) -> None:
        content = "x" * (SMALL_CAP - 1)
        self.log_path.write_text(content)
        rotate_if_oversized(self.log_path, max_bytes=SMALL_CAP)
        self.assertEqual(self.log_path.read_text(), content)
        self.assertFalse((self.tmpdir / "display.stdout.log.old").exists())

    def test_file_at_exact_cap_is_left_alone(self) -> None:
        """max_bytes is a threshold to exceed, not meet — a file exactly
        at the cap hasn't overflowed it."""
        content = "x" * SMALL_CAP
        self.log_path.write_text(content)
        rotate_if_oversized(self.log_path, max_bytes=SMALL_CAP)
        self.assertEqual(self.log_path.read_text(), content)
        self.assertFalse((self.tmpdir / "display.stdout.log.old").exists())

    def test_file_over_cap_is_rotated(self) -> None:
        content = "x" * (SMALL_CAP + 1)
        self.log_path.write_text(content)
        rotate_if_oversized(self.log_path, max_bytes=SMALL_CAP)

        old_path = self.tmpdir / "display.stdout.log.old"
        self.assertTrue(old_path.exists())
        self.assertEqual(old_path.read_text(), content)
        # Nothing left at the original path - the next write (launchd
        # reopening it for the restarted process) recreates it fresh.
        self.assertFalse(self.log_path.exists())

    def test_rotation_overwrites_previous_old_generation(self) -> None:
        old_path = self.tmpdir / "display.stdout.log.old"
        old_path.write_text("stale generation from a prior rotation")

        content = "y" * (SMALL_CAP + 1)
        self.log_path.write_text(content)
        rotate_if_oversized(self.log_path, max_bytes=SMALL_CAP)

        self.assertEqual(old_path.read_text(), content)

    def test_default_max_bytes_is_ten_megabytes(self) -> None:
        from display import log_rotation

        self.assertEqual(log_rotation.MAX_LOG_BYTES, 10 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
