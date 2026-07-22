"""Unit tests for sources/folder.py — default source.

Two properties carry disproportionate weight here.

**Id stability.** `rotation.py` persists the walk order keyed by id and
resumes only when the pool hash matches, so an id derived from anything
run-scoped would reshuffle from position 0 on every restart — a silent
failure with no error anywhere.

**Validation on the path-serving route.** `caches = False` means these
paths go straight to the renderer with `ImageCache` never involved, so
this is the source for which a check living only in cache-sync would
never run at all.
"""

from __future__ import annotations

import os
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from display.sources.folder import POLL_INTERVAL_S, FolderSource


def png_bytes(width: int = 64, height: int = 64) -> bytes:
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    chunk = (
        struct.pack(">I", len(ihdr))
        + b"IHDR"
        + ihdr
        + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr))
    )
    return signature + chunk


class FolderSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, name: str, data: bytes | None = None) -> Path:
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png_bytes() if data is None else data)
        return path

    # -- listing ---------------------------------------------------------

    def test_lists_png_and_jpeg_only(self) -> None:
        self.write("a.png")
        self.write("b.JPG")
        self.write("notes.txt", b"hello")
        self.write("clip.mov", b"\x00" * 32)

        names = {r.filename for r in FolderSource(self.dir).list_images()}
        self.assertEqual(names, {"a.png", "b.JPG"})

    def test_subfolders_are_excluded_by_default(self) -> None:
        self.write("top.png")
        self.write("nested/deep.png")

        self.assertEqual(
            [r.filename for r in FolderSource(self.dir).list_images()], ["top.png"]
        )

    def test_subfolders_are_included_when_asked(self) -> None:
        self.write("top.png")
        self.write("nested/deep.png")

        names = {
            r.filename
            for r in FolderSource(self.dir, include_subfolders=True).list_images()
        }
        self.assertEqual(names, {"top.png", "deep.png"})

    def test_a_missing_folder_returns_empty_without_raising(self) -> None:
        source = FolderSource(self.dir / "does-not-exist")
        try:
            self.assertEqual(source.list_images(), [])
        except Exception as exc:  # noqa: BLE001 - this is the assertion
            self.fail(f"list_images() raised {exc!r}")

    def test_an_unreadable_folder_returns_empty_without_raising(self) -> None:
        """TCC denial looks exactly like this, and it must degrade
        to 'no pictures', not to a crash."""
        locked = self.dir / "locked"
        locked.mkdir()
        (locked / "a.png").write_bytes(png_bytes())
        os.chmod(locked, 0o000)
        try:
            self.assertEqual(FolderSource(locked).list_images(), [])
        finally:
            os.chmod(locked, 0o700)

    # -- sort order ------------------------------------------------------

    def test_sorted_by_name_by_default(self) -> None:
        for name in ("c.png", "a.png", "b.png"):
            self.write(name)
        self.assertEqual(
            [r.filename for r in FolderSource(self.dir).list_images()],
            ["a.png", "b.png", "c.png"],
        )

    def test_newest_and_oldest_orders(self) -> None:
        old = self.write("old.png")
        new = self.write("new.png")
        os.utime(old, (1000, 1000))
        os.utime(new, (2000, 2000))

        newest = FolderSource(self.dir, sort_order="newest").list_images()
        oldest = FolderSource(self.dir, sort_order="oldest").list_images()
        self.assertEqual([r.filename for r in newest], ["new.png", "old.png"])
        self.assertEqual([r.filename for r in oldest], ["old.png", "new.png"])

    def test_an_unknown_sort_order_falls_back_rather_than_failing(self) -> None:
        """Source options must not become the one unvalidated
        region of the config."""
        self.write("a.png")
        source = FolderSource(self.dir, sort_order="by-vibes")
        self.assertEqual([r.filename for r in source.list_images()], ["a.png"])

    # -- ids -------------------------------------------------------------

    def test_ids_are_stable_across_instances(self) -> None:
        """The property rotation.py's resume depends on."""
        self.write("a.png")
        first = FolderSource(self.dir).list_images()[0].id
        second = FolderSource(self.dir).list_images()[0].id
        self.assertEqual(first, second)

    def test_ids_are_the_documented_derivation(self) -> None:
        import hashlib

        path = self.write("a.png")
        expected = hashlib.sha256(
            str(path.resolve()).encode("utf-8")
        ).hexdigest()[:16]
        self.assertEqual(FolderSource(self.dir).list_images()[0].id, expected)

    def test_renaming_a_file_changes_its_id(self) -> None:
        """This consequence is stated rather than
        discovered: the picture re-enters the rotation as a new one."""
        path = self.write("before.png")
        before = FolderSource(self.dir).list_images()[0].id
        path.rename(self.dir / "after.png")
        after = FolderSource(self.dir).list_images()[0].id
        self.assertNotEqual(before, after)

    def test_ids_are_path_safe_and_bounded(self) -> None:
        """The id becomes an on-disk filename elsewhere, so a name full
        of separators or 300 characters long must not survive."""
        from display.sources.base import is_safe_id

        self.write("../../nasty name with spaces.png".replace("../../", ""))
        self.write("x" * 200 + ".png")
        for record in FolderSource(self.dir).list_images():
            self.assertTrue(is_safe_id(record.id), record.id)

    # -- labels ----------------------------------------------------------

    def test_label_is_the_filename_stem(self) -> None:
        self.write("Harbour at dawn.png")
        self.assertEqual(
            FolderSource(self.dir).list_images()[0].display_label, "Harbour at dawn"
        )

    def test_a_long_stem_is_capped(self) -> None:
        self.write("z" * 120 + ".png")
        label = FolderSource(self.dir).list_images()[0].display_label
        self.assertLessEqual(len(label), 28)
        self.assertTrue(label.endswith("..."))

    # -- safety ----------------------------------------------------------

    def test_a_local_decompression_bomb_never_enters_the_listing(self) -> None:
        """Motivating case for moving the check to the decode
        boundary: `caches = False` means nothing in cache.py would ever
        have looked at this file."""
        self.write("bomb.png", png_bytes(60000, 60000))
        self.write("fine.png")
        self.assertEqual(
            [r.filename for r in FolderSource(self.dir).list_images()], ["fine.png"]
        )

    def test_a_renamed_non_image_never_enters_the_listing(self) -> None:
        self.write("actually-a-script.png", b"#!/bin/sh\nrm -rf /\n")
        self.assertEqual(FolderSource(self.dir).list_images(), [])

    def test_path_for_revalidates_rather_than_trusting_the_listing(self) -> None:
        """The return value goes straight to a decoder, and a file can be
        replaced between listing and display."""
        path = self.write("a.png")
        source = FolderSource(self.dir)
        record = source.list_images()[0]
        self.assertEqual(source.path_for(record), path.resolve())

        path.write_bytes(png_bytes(60000, 60000))
        self.assertIsNone(source.path_for(record))

    def test_path_for_a_vanished_file_returns_none(self) -> None:
        path = self.write("a.png")
        source = FolderSource(self.dir)
        record = source.list_images()[0]
        path.unlink()
        self.assertIsNone(source.path_for(record))

    def test_fetch_returns_validated_bytes(self) -> None:
        self.write("a.png")
        source = FolderSource(self.dir)
        record = source.list_images()[0]
        self.assertEqual(source.fetch(record), png_bytes())

    # -- configuration ---------------------------------------------------

    def test_this_source_does_not_cache(self) -> None:
        """A local folder needs no cache, and copying up to 300 of
        the user's own photos into ~/Library/Caches would double their
        disk usage to no end."""
        self.assertFalse(FolderSource(self.dir).caches)

    def test_the_poll_interval_is_seconds_not_half_an_hour(self) -> None:
        """Half an hour after dropping in a photo reads as broken."""
        self.assertEqual(POLL_INTERVAL_S, 10.0)
        self.assertEqual(FolderSource(self.dir).poll_interval_s, 10.0)

    def test_two_folders_get_different_cache_namespaces(self) -> None:
        other = self.dir / "other"
        other.mkdir()
        self.assertNotEqual(
            FolderSource(self.dir).cache_namespace,
            FolderSource(other).cache_namespace,
        )


if __name__ == "__main__":
    unittest.main()
