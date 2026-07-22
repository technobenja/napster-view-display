"""Unit tests for atomic_io.py's byte writer — added with Step 5.

`atomic_write_json` has been exercised indirectly since Phase 2 (through
config_store, cache, and rotation). `atomic_write_bytes` is new and has a
caller with a sharper failure mode: a truncated LaunchAgent plist is a
service that silently never starts at login, with nothing on screen to
say why. So the atomicity property gets asserted directly here rather
than assumed.

    ./.venv/bin/python3 -m unittest test_atomic_io -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from display.atomic_io import atomic_write_bytes, atomic_write_json


class AtomicWriteBytesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.path = self.tmpdir / "out.bin"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_writes_the_bytes(self) -> None:
        atomic_write_bytes(self.path, b"\x00\x01binary\xff")
        self.assertEqual(self.path.read_bytes(), b"\x00\x01binary\xff")

    def test_creates_the_parent_directory(self) -> None:
        nested = self.tmpdir / "a" / "b" / "out.bin"
        atomic_write_bytes(nested, b"x")
        self.assertTrue(nested.is_file())

    def test_overwrites_an_existing_file(self) -> None:
        self.path.write_bytes(b"old and longer")
        atomic_write_bytes(self.path, b"new")
        self.assertEqual(self.path.read_bytes(), b"new")

    def test_leaves_no_temp_file_behind_on_success(self) -> None:
        atomic_write_bytes(self.path, b"x")
        self.assertEqual([p.name for p in self.tmpdir.iterdir()], ["out.bin"])

    def test_a_failed_write_leaves_the_original_untouched(self) -> None:
        """The property the whole helper exists for: never a
        half-written file where a good one used to be."""
        self.path.write_bytes(b"the good contents")
        with patch("os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                atomic_write_bytes(self.path, b"the bad contents")
        self.assertEqual(self.path.read_bytes(), b"the good contents")

    def test_a_failed_write_cleans_up_its_temp_file(self) -> None:
        self.path.write_bytes(b"good")
        with patch("os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                atomic_write_bytes(self.path, b"bad")
        leftovers = [p.name for p in self.tmpdir.iterdir() if p.name.startswith(".tmp-")]
        self.assertEqual(leftovers, [])

    def test_temp_file_is_created_beside_the_destination(self) -> None:
        """os.replace() is atomic only within one filesystem, which is
        why the temp file is not in /tmp."""
        seen: list[Path] = []
        real_mkstemp = tempfile.mkstemp

        def spy(*args, **kwargs):
            seen.append(Path(kwargs["dir"]))
            return real_mkstemp(*args, **kwargs)

        with patch("tempfile.mkstemp", side_effect=spy):
            atomic_write_bytes(self.path, b"x")
        self.assertEqual(seen, [self.tmpdir])


class AtomicWriteJsonTests(unittest.TestCase):
    """Regression cover for the refactor: `atomic_write_json` now
    delegates to `atomic_write_bytes`, and its existing callers must not
    be able to tell."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "out.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_round_trips(self) -> None:
        data = {"b": 1, "a": [1, 2, {"c": None}]}
        atomic_write_json(self.path, data)
        self.assertEqual(json.loads(self.path.read_text()), data)

    def test_still_indents(self) -> None:
        atomic_write_json(self.path, {"a": 1})
        self.assertIn("\n  ", self.path.read_text())

    def test_utf8_survives(self) -> None:
        atomic_write_json(self.path, {"label": "café — naïve"})
        self.assertEqual(
            json.loads(self.path.read_text())["label"], "café — naïve"
        )

    def test_unserialisable_data_raises_before_touching_the_file(self) -> None:
        self.path.write_text('{"good": true}')
        with self.assertRaises(TypeError):
            atomic_write_json(self.path, {"bad": object()})
        self.assertEqual(self.path.read_text(), '{"good": true}')


if __name__ == "__main__":
    unittest.main()
