"""Unit tests for cache.py — manifest diffing, grace-period
pruning, missing-file detection, size eviction, all pure filesystem +
JSON against a temp dir.

Rewritten: `sync()` now takes an `ImageSource`, not an
`httpx.Client`, and `_download` calls `source.fetch(record)` instead of
building `/images/{filename}` itself. The fake httpx client these tests
used to pass in *was* the old contract, which is why the plan says to
update this file first — the stub below is the new one.

Byte retrieval and its safety checks now belong to the sources, and
are tested against each source in test_sources_*.py. What is left here is
what cache.py still owns: the manifest.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from display.cache import MISS_GRACE_POLLS, ImageCache
from display.sources.base import ImageRecord, ImageSource


def _record(image_id: str, filename: str | None = None) -> ImageRecord:
    return ImageRecord(
        id=image_id,
        filename=filename or f"{image_id}.png",
        display_label=image_id,
        locator=filename or f"{image_id}.png",
        style="Minimalist",
    )


class StubSource(ImageSource):
    """The new `sync()` contract in miniature: something that answers
    `fetch()` with bytes or None. Records every request so the tests can
    assert on *what was asked for*, which is what the old mock-transport
    URL assertions were really checking."""

    kind = "stub"

    def __init__(self, body: bytes | None = b"fake-image-bytes") -> None:
        self._body = body
        self.fetched: list[ImageRecord] = []

    def list_images(self) -> list[ImageRecord]:
        return []

    def fetch(self, record: ImageRecord) -> bytes | None:
        self.fetched.append(record)
        return self._body


def _source(body: bytes | None = b"fake-image-bytes") -> StubSource:
    return StubSource(body)


class CacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_sync_downloads_new_record_and_writes_manifest(self) -> None:
        cache = ImageCache(cache_dir=self.cache_dir)
        cache.sync([_record("img-1")], _source())

        path = cache.get_path("img-1")
        self.assertIsNotNone(path)
        self.assertTrue(path.exists())
        self.assertEqual(path.read_bytes(), b"fake-image-bytes")
        self.assertTrue((self.cache_dir / "manifest.json").exists())

    def test_on_disk_filename_derived_from_id_not_api_filename(self) -> None:
        """Path safety: even a hostile-looking `filename` field must not
        end up joined directly into a filesystem path."""
        cache = ImageCache(cache_dir=self.cache_dir)
        record = _record("safe-id", filename="../../etc/passwd.png")
        cache.sync([record], _source())

        path = cache.get_path("safe-id")
        self.assertIsNotNone(path)
        self.assertEqual(path.name, "safe-id.png")
        self.assertEqual(path.parent, self.cache_dir / "images")

    def test_download_delegates_to_the_source(self) -> None:
        """The outbound request is no longer cache.py's to build.
        What is asserted here is the new seam — the source is asked for
        exactly the record it was given — while the assertion about the
        *request URL* being id-derived moved to
        test_sources_image_server.py, which is now where that URL is
        assembled."""
        source = _source()
        cache = ImageCache(cache_dir=self.cache_dir)
        record = _record("safe-id", filename="../elsewhere")
        cache.sync([record], source)

        self.assertEqual([r.id for r in source.fetched], ["safe-id"])
        self.assertEqual(cache.get_path("safe-id").name, "safe-id.png")

    def test_extensionless_filename_falls_back_to_png(self) -> None:
        cache = ImageCache(cache_dir=self.cache_dir)
        record = _record("no-ext-id", filename="no-ext-id")
        cache.sync([record], _source())

        path = cache.get_path("no-ext-id")
        self.assertIsNotNone(path)
        self.assertEqual(path.name, "no-ext-id.png")

    def test_already_cached_id_is_not_redownloaded(self) -> None:
        source = _source()
        cache = ImageCache(cache_dir=self.cache_dir)
        cache.sync([_record("img-1")], source)
        cache.sync([_record("img-1")], source)
        self.assertEqual(len(source.fetched), 1)

    def test_manifest_persists_across_instances(self) -> None:
        cache = ImageCache(cache_dir=self.cache_dir)
        cache.sync([_record("img-1")], _source())

        reloaded = ImageCache(cache_dir=self.cache_dir)
        self.assertEqual(reloaded.known_ids(), {"img-1"})

    def test_get_path_unknown_id_returns_none(self) -> None:
        cache = ImageCache(cache_dir=self.cache_dir)
        self.assertIsNone(cache.get_path("nope"))

    def test_get_path_drops_entry_when_file_missing_on_disk(self) -> None:
        cache = ImageCache(cache_dir=self.cache_dir)
        cache.sync([_record("img-1")], _source())
        path = cache.get_path("img-1")
        path.unlink()  # simulate the file vanishing out from under the manifest

        self.assertIsNone(cache.get_path("img-1"))
        self.assertNotIn("img-1", cache.known_ids())

    def test_malformed_manifest_falls_back_to_empty(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        (self.cache_dir / "manifest.json").write_text("{not valid json")
        cache = ImageCache(cache_dir=self.cache_dir)
        self.assertEqual(cache.known_ids(), set())

    def test_one_corrupted_manifest_entry_does_not_drop_the_others(self) -> None:
        """Fix 4: a single malformed entry among several good ones must
        be skipped individually, not wipe out the whole manifest."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        good_entry = {
            "id": "img-1",
            "filename": "img-1.png",
            "downloaded_at": 1.0,
            "width": 100,
            "height": 100,
            "starred": False,
        }
        corrupted_entry = {
            "id": "img-2",
            # missing required "filename"/"downloaded_at"/etc keys
        }
        another_good_entry = {**good_entry, "id": "img-3", "filename": "img-3.png"}
        manifest_data = {
            "img-1": good_entry,
            "img-2": corrupted_entry,
            "img-3": another_good_entry,
        }
        (self.cache_dir / "manifest.json").write_text(json.dumps(manifest_data))

        cache = ImageCache(cache_dir=self.cache_dir)
        self.assertEqual(cache.known_ids(), {"img-1", "img-3"})

    def test_manifest_entry_with_unsafe_filename_is_dropped(self) -> None:
        """Fix 4 (defense in depth for Fix 1): a hand-edited/corrupted
        manifest.json entry with a path-traversal-shaped filename must
        be dropped on load, even without any network round-trip."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        unsafe_entry = {
            "id": "img-evil",
            "filename": "../../../../tmp/evil.png",
            "downloaded_at": 1.0,
            "width": 100,
            "height": 100,
            "starred": False,
        }
        safe_entry = {
            "id": "img-safe",
            "filename": "img-safe.png",
            "downloaded_at": 1.0,
            "width": 100,
            "height": 100,
            "starred": False,
        }
        manifest_data = {"img-evil": unsafe_entry, "img-safe": safe_entry}
        (self.cache_dir / "manifest.json").write_text(json.dumps(manifest_data))

        cache = ImageCache(cache_dir=self.cache_dir)
        self.assertEqual(cache.known_ids(), {"img-safe"})

    def test_manifest_not_an_object_falls_back_to_empty(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        (self.cache_dir / "manifest.json").write_text(json.dumps(["not", "an", "object"]))
        cache = ImageCache(cache_dir=self.cache_dir)
        self.assertEqual(cache.known_ids(), set())

    def test_missing_id_within_grace_period_is_kept(self) -> None:
        cache = ImageCache(cache_dir=self.cache_dir)
        client = _source()
        cache.sync([_record("img-1")], client)

        for _ in range(MISS_GRACE_POLLS):
            cache.sync([], client)  # id absent from this poll's results

        self.assertIn("img-1", cache.known_ids())

    def test_missing_id_past_grace_period_is_evicted(self) -> None:
        cache = ImageCache(cache_dir=self.cache_dir)
        client = _source()
        cache.sync([_record("img-1")], client)
        path = cache.get_path("img-1")

        for _ in range(MISS_GRACE_POLLS + 1):
            cache.sync([], client)

        self.assertNotIn("img-1", cache.known_ids())
        self.assertFalse(path.exists())

    def test_reappearing_id_resets_miss_counter(self) -> None:
        cache = ImageCache(cache_dir=self.cache_dir)
        client = _source()
        cache.sync([_record("img-1")], client)

        cache.sync([], client)  # one miss
        cache.sync([_record("img-1")], client)  # reappears, should reset to 0

        for _ in range(MISS_GRACE_POLLS):
            cache.sync([], client)
        # Only MISS_GRACE_POLLS misses since the reset - still within grace.
        self.assertIn("img-1", cache.known_ids())

    def test_size_ceiling_evicts_least_recently_shown_first(self) -> None:
        cache = ImageCache(cache_dir=self.cache_dir, max_size=2)
        client = _source()
        cache.sync([_record("img-1"), _record("img-2"), _record("img-3")], client)

        # Only max_size=2 should survive the same sync() pass that added them.
        self.assertEqual(len(cache.known_ids()), 2)

    def test_mark_shown_protects_from_eviction(self) -> None:
        cache = ImageCache(cache_dir=self.cache_dir, max_size=3)
        client = _source()
        cache.sync([_record("img-1"), _record("img-2"), _record("img-3")], client)
        cache.mark_shown("img-1")
        time.sleep(0.01)
        cache.mark_shown("img-2")

        # Now shrink the ceiling and sync again to trigger eviction; img-3
        # (never shown) should go before the two marked-shown entries.
        cache._max_size = 2  # type: ignore[attr-defined]
        cache.sync([_record("img-1"), _record("img-2"), _record("img-3")], client)

        self.assertIn("img-1", cache.known_ids())
        self.assertIn("img-2", cache.known_ids())
        self.assertNotIn("img-3", cache.known_ids())

    def test_download_failure_leaves_manifest_unchanged(self) -> None:
        """A source that returns None — network failure, a 500, or bytes
        that failed magic-byte/size/dimension checks — must leave
        the manifest exactly as it was."""
        cache = ImageCache(cache_dir=self.cache_dir)
        cache.sync([_record("img-1")], _source(body=None))
        self.assertEqual(cache.known_ids(), set())

    def test_sync_never_raises_when_a_source_breaks_its_contract(self) -> None:
        """`fetch()` is contractually never-raises, but sync() runs off an
        NSTimer selector — a source that breaks that contract must cost
        one image, not the display."""

        class BrokenSource(StubSource):
            def fetch(self, record: ImageRecord) -> bytes | None:
                raise RuntimeError("simulated source bug")

        cache = ImageCache(cache_dir=self.cache_dir)
        try:
            cache.sync([_record("img-1")], BrokenSource())
        except Exception as exc:  # noqa: BLE001 - this is the assertion
            self.fail(f"sync() raised {exc!r} instead of skipping the download")
        self.assertEqual(cache.known_ids(), set())

    def test_download_write_failure_is_handled_gracefully(self) -> None:
        """Matches the existing failure-path style (e.g.
        test_download_failure_leaves_manifest_unchanged): a write error
        during the download's own atomic-write step (here, a read-only
        images/ dir so even tempfile.mkstemp() itself fails) must be
        caught and logged, not raised, and must leave the manifest
        unchanged."""
        images_dir = self.cache_dir / "images"
        images_dir.mkdir(parents=True)
        os.chmod(images_dir, 0o500)  # read + execute, no write
        try:
            cache = ImageCache(cache_dir=self.cache_dir)
            try:
                cache.sync([_record("img-1")], _source())
            except Exception as exc:  # noqa: BLE001 - this is the assertion
                self.fail(
                    f"sync() raised {exc!r} instead of handling the write failure"
                )
            self.assertEqual(cache.known_ids(), set())
        finally:
            os.chmod(images_dir, 0o700)  # restore so TemporaryDirectory cleanup works

    def test_manifest_write_is_atomic_no_leftover_tmp_files(self) -> None:
        cache = ImageCache(cache_dir=self.cache_dir)
        cache.sync([_record("img-1")], _source())
        leftovers = list(self.cache_dir.glob(".tmp-*"))
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
