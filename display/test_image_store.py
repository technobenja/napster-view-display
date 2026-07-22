"""Unit tests for image_store.py — the `caches = False` store.

`DirectStore` exists so the display loop does not have to branch on
`source.caches` at every call site, which means the property worth testing
is that it presents the same four methods `ImageCache` does and behaves
sensibly where it deliberately differs.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from display.cache import ImageCache
from display.image_store import DirectStore
from display.sources.base import ImageRecord, ImageSource


class StubFolderSource(ImageSource):
    kind = "stub-folder"
    caches = False

    def __init__(self, paths: dict[str, Path]) -> None:
        self._paths = paths

    def list_images(self) -> list[ImageRecord]:
        return [
            ImageRecord(id=i, filename=p.name, locator=str(p))
            for i, p in self._paths.items()
        ]

    def fetch(self, record: ImageRecord) -> bytes | None:
        return None

    def path_for(self, record: ImageRecord) -> Path | None:
        return self._paths.get(record.id)


class DirectStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.a = self.dir / "a.png"
        self.a.write_bytes(b"x")
        self.source = StubFolderSource({"id-a": self.a})
        self.store = DirectStore()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_it_presents_the_same_surface_as_the_cache(self) -> None:
        """The whole justification for this class: app.py holds one or
        the other and calls the same four methods either way."""
        for name in ("known_ids", "get_path", "mark_shown", "sync"):
            self.assertTrue(hasattr(DirectStore, name), name)
            self.assertTrue(hasattr(ImageCache, name), name)

    def test_sync_populates_paths_from_the_source(self) -> None:
        self.store.sync(self.source.list_images(), self.source)
        self.assertEqual(self.store.known_ids(), {"id-a"})
        self.assertEqual(self.store.get_path("id-a"), self.a)

    def test_nothing_is_copied_anywhere(self) -> None:
        """Reason for caches=False: the user's own photos are not
        duplicated into ~/Library/Caches."""
        self.store.sync(self.source.list_images(), self.source)
        self.assertEqual(self.store.get_path("id-a"), self.a)
        self.assertEqual(list(self.dir.iterdir()), [self.a])

    def test_a_removed_file_leaves_immediately_rather_than_after_a_grace_period(self) -> None:
        """Unlike a cache, this map is not state worth protecting — it is
        a view of what is on disk right now."""
        self.store.sync(self.source.list_images(), self.source)
        self.source._paths.clear()
        self.store.sync(self.source.list_images(), self.source)
        self.assertEqual(self.store.known_ids(), set())

    def test_get_path_drops_a_file_that_vanished_since_the_sync(self) -> None:
        self.store.sync(self.source.list_images(), self.source)
        self.a.unlink()
        self.assertIsNone(self.store.get_path("id-a"))
        self.assertEqual(self.store.known_ids(), set())

    def test_get_path_for_an_unknown_id_returns_none(self) -> None:
        self.assertIsNone(self.store.get_path("nope"))

    def test_a_record_the_source_rejects_is_not_stored(self) -> None:
        """path_for() re-validates, so a record that fails the safety
        checks at display time simply never lands here."""
        records = self.source.list_images() + [
            ImageRecord(id="id-rejected", filename="b.png", locator="/nope/b.png")
        ]
        self.store.sync(records, self.source)
        self.assertEqual(self.store.known_ids(), {"id-a"})

    def test_mark_shown_is_a_no_op_that_never_raises(self) -> None:
        try:
            self.store.mark_shown("id-a")
            self.store.mark_shown("never-seen")
        except Exception as exc:  # noqa: BLE001 - this is the assertion
            self.fail(f"mark_shown raised {exc!r}")

    def test_sync_survives_a_source_that_breaks_its_contract(self) -> None:
        class BrokenSource(StubFolderSource):
            def path_for(self, record: ImageRecord) -> Path | None:
                raise RuntimeError("simulated source bug")

        broken = BrokenSource({"id-a": self.a})
        try:
            self.store.sync(broken.list_images(), broken)
        except Exception as exc:  # noqa: BLE001 - this is the assertion
            self.fail(f"sync raised {exc!r}")
        self.assertEqual(self.store.known_ids(), set())


if __name__ == "__main__":
    unittest.main()
