"""Local image cache.

Lives in `~/Library/Caches/<bundle-id>/`, not in the
source tree. The manifest is the source of truth for "what we actually have," decoupled from whatever Image Server
currently reports — that decoupling is what lets rotation survive a
source outage. Written atomically (temp file + rename); a parse
failure is treated identically to a missing manifest (falls back to an
empty cache, never raises).

Cache filenames are always derived from `id`, never from the API's raw
`filename` field directly — the configured source may be trusted local
infrastructure, not attacker-facing, but this closes the path-safety gap
by construction rather than by trusting an external string.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from display import paths
from display.atomic_io import atomic_write_json
from display.sources.base import ImageRecord, ImageSource

logger = logging.getLogger(__name__)

# `~/Library/Caches/<bundle-id>/` — relocated out of the source tree in
# Step -1. Regenerable data, so it lives where macOS
# expects regenerable data and where uninstall can remove it
# without prompting.
DEFAULT_CACHE_DIR = paths.cache_dir()
IMAGES_SUBDIR = "images"
MANIFEST_FILENAME = "manifest.json"

DEFAULT_MAX_CACHE_SIZE = 300
# Consecutive missed polls before an id absent from the API becomes an
# eviction candidate - a grace period against transient hiccups/unstars.
MISS_GRACE_POLLS = 2

# How many images one sync() may download before stopping
# and leaving the rest for the next poll.
#
# sync() runs off the poll NSTimer, on the same main thread as every menu
# action and every calibration nudge. Unbounded, a first run against a
# 300-image pool issues 300 sequential downloads there — minutes during
# which the UI is indistinguishable from a hung process, at exactly the
# moment a new user is forming their first impression of it. Bounded, the
# pool fills in over successive polls instead, and the display shows the
# first batch immediately rather than nothing at all until the last byte
# lands.
#
# Deferred images are safe by construction: they were added to `seen_ids`
# before the bound was checked, so the miss-counter/eviction pass below
# does not touch them, and they are simply still-absent from the manifest
# on the next sync, which downloads them then.
MAX_DOWNLOADS_PER_SYNC = 10

# Same allow-list character class as image_pool.SAFE_ID_RE, extended with
# an optional single dot-extension - exactly what _on_disk_name() ever
# produces (f"{id}{ext}"). Used to re-validate manifest entries loaded
# from local disk: manifest.json is local state, not a live API response,
# but a hand-edited or corrupted entry could still reintroduce the Fix 1
# path-traversal shape without any network round-trip at all.
# The length bound matches sources.base.MAX_ID_CHARS: the id was
# already allow-listed to this character class but was unbounded, which on
# a source that isn't Image Server's UUIDs is an ENAMETOOLONG waiting to
# happen.
_SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}(\.[A-Za-z0-9]{1,10})?$")


def _is_safe_filename(filename: Any) -> bool:
    return isinstance(filename, str) and bool(_SAFE_FILENAME_RE.match(filename))


@dataclasses.dataclass
class ManifestEntry:
    """One cached file. `width`, `height` and `starred` were dropped in
    Step 0 along with the same three fields on `ImageRecord` — no
    code read them, and an entry written by an older build simply has
    extra keys `from_dict` ignores."""

    id: str
    filename: str
    downloaded_at: float
    last_shown_at: float | None = None
    missing_polls: int = 0

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ManifestEntry:
        return cls(
            id=data["id"],
            filename=data["filename"],
            downloaded_at=data["downloaded_at"],
            last_shown_at=data.get("last_shown_at"),
            missing_polls=data.get("missing_polls", 0),
        )


class ImageCache:
    """Owns display/cache/: the manifest and the downloaded image files.
    No network calls except the explicit `sync()` download pass."""

    def __init__(
        self,
        cache_dir: Path | str = DEFAULT_CACHE_DIR,
        max_size: int = DEFAULT_MAX_CACHE_SIZE,
    ) -> None:
        self._cache_dir = Path(cache_dir)
        self._images_dir = self._cache_dir / IMAGES_SUBDIR
        self._manifest_path = self._cache_dir / MANIFEST_FILENAME
        self._max_size = max_size
        self._manifest: dict[str, ManifestEntry] = self._load_manifest()

    @classmethod
    def for_source(
        cls,
        source: ImageSource,
        max_size: int = DEFAULT_MAX_CACHE_SIZE,
        root: Path | str = DEFAULT_CACHE_DIR,
    ) -> ImageCache:
        """Namespace the cache by source. Ids only have to be
        unique *within* a source, so a shared directory would let a
        switch between two Image Server servers — or from a URL list to a
        folder — serve the previous source's bytes under the new source's
        ids."""
        return cls(cache_dir=Path(root) / source.cache_namespace, max_size=max_size)

    # -- manifest I/O ---------------------------------------------------

    def _load_manifest(self) -> dict[str, ManifestEntry]:
        import json

        try:
            raw = self._manifest_path.read_text()
        except OSError:
            return {}

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(
                "cache.py: %s is not valid JSON; starting from an empty cache.",
                self._manifest_path,
            )
            return {}

        if not isinstance(data, dict):
            logger.warning(
                "cache.py: %s has an unexpected shape (not an object); "
                "starting from an empty cache.",
                self._manifest_path,
            )
            return {}

        # Per-entry, not one try/except around the whole comprehension:
        # a single malformed entry among potentially hundreds must not
        # wipe out every other, otherwise-good entry. Matches this
        # project's "degrade minimally, not maximally" philosophy for a
        # local data problem (see module docstring).
        manifest: dict[str, ManifestEntry] = {}
        for image_id, raw_entry in data.items():
            try:
                entry = ManifestEntry.from_dict(raw_entry)
            except (KeyError, TypeError) as exc:
                logger.warning(
                    "cache.py: dropping malformed manifest entry %r (%s).",
                    image_id,
                    exc,
                )
                continue
            if not _is_safe_filename(entry.filename):
                # Defense in depth for Fix 1: a corrupted/hand-edited
                # manifest.json shouldn't be able to reintroduce the
                # path-traversal shape via local state, even without a
                # live network round-trip.
                logger.warning(
                    "cache.py: dropping manifest entry %r with unsafe "
                    "filename %r.",
                    image_id,
                    entry.filename,
                )
                continue
            manifest[image_id] = entry
        return manifest

    def _save_manifest(self) -> None:
        data = {k: v.to_dict() for k, v in self._manifest.items()}
        atomic_write_json(self._manifest_path, data)

    # -- lookups ----------------------------------------------------------

    def known_ids(self) -> set[str]:
        return set(self._manifest.keys())

    def get_path(self, image_id: str) -> Path | None:
        """Return the on-disk path for a cached image, or None if it
        isn't cached or has gone missing from disk (in which case it's
        dropped from the manifest so the next sync() re-downloads it —
        never raises into a caller like the render loop)."""
        entry = self._manifest.get(image_id)
        if entry is None:
            return None
        path = self._images_dir / entry.filename
        if not path.exists():
            logger.warning(
                "cache.py: %s missing on disk, dropping from manifest.", path
            )
            del self._manifest[image_id]
            self._save_manifest()
            return None
        return path

    def mark_shown(self, image_id: str) -> None:
        entry = self._manifest.get(image_id)
        if entry is not None:
            entry.last_shown_at = time.time()
            self._save_manifest()

    # -- sync against a fresh list_images() result -------------------------

    def _on_disk_name(self, record: ImageRecord) -> str:
        ext = Path(record.filename).suffix or ".png"
        return f"{record.id}{ext}"

    def _download(self, record: ImageRecord, source: ImageSource) -> bool:
        """Ask the source for bytes and write them under an id-derived
        name.

        This used to build `/images/{filename}` and issue the GET
        itself, which is why `cache.py` could never have served a local
        folder or an arbitrary URL. Retrieval now belongs to the source,
        which is also where size cap, magic-byte check and
        dimension bound are applied — so by the time bytes arrive here
        they have already been validated, once, in one place.

        The on-disk name is still derived from `record.id`, never from
        the source's `filename`: `_on_disk_name`'s only variable
        component beyond the allow-listed id is `Path(...).suffix`, which
        by construction contains no path separator."""
        # `fetch()` is contractually "never raises", but sync() is called
        # from an NSTimer selector and a source is the most likely place
        # for a future contributor's new code to break that contract. One
        # cheap guard here keeps a source bug from costing the display.
        try:
            data = source.fetch(record)
        except Exception as exc:  # noqa: BLE001 - see above
            logger.warning(
                "cache.py: source.fetch(%s) raised %r; skipping this image.",
                record.id,
                exc,
            )
            return False
        if data is None:
            # The source has already logged the concrete reason.
            return False

        filename = self._on_disk_name(record)
        dest = self._images_dir / filename
        self._images_dir.mkdir(parents=True, exist_ok=True)
        # mkstemp() itself is inside the try (not just fdopen/write/
        # replace): a permission error creating the temp file - e.g. a
        # read-only images/ dir - must degrade the same way a write or
        # rename failure does, not escape sync()'s "never raises"
        # contract.
        tmp_name: str | None = None
        try:
            fd, tmp_name = tempfile.mkstemp(
                dir=self._images_dir, prefix=".dl-", suffix=dest.suffix
            )
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp_name, dest)
        except OSError as exc:
            logger.warning("cache.py: failed to write %s: %s", dest, exc)
            if tmp_name is not None:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
            return False

        self._manifest[record.id] = ManifestEntry(
            id=record.id,
            filename=filename,
            downloaded_at=time.time(),
        )
        return True

    def _evict(self, image_id: str) -> None:
        entry = self._manifest.pop(image_id, None)
        if entry is None:
            return
        path = self._images_dir / entry.filename
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(
                "cache.py: failed to remove %s during eviction: %s", path, exc
            )

    def _enforce_size_ceiling(self) -> None:
        overflow = len(self._manifest) - self._max_size
        if overflow <= 0:
            return
        least_recently_shown = sorted(
            self._manifest.items(), key=lambda kv: kv[1].last_shown_at or 0.0
        )
        for image_id, _entry in least_recently_shown[:overflow]:
            self._evict(image_id)

    def sync(
        self,
        records: list[ImageRecord],
        source: ImageSource,
        max_downloads: int = MAX_DOWNLOADS_PER_SYNC,
    ) -> int:
        """Reconcile the manifest against a fresh, successful
        `list_images()` result: download **up to `max_downloads`** new
        ids, reset the miss counter for ids still present, age out
        ids that have been absent for more than MISS_GRACE_POLLS
        consecutive polls, then enforce the size ceiling. Callers must
        only call this after a *successful* poll — an empty list from a
        failed poll should never reach here, or every image would look
        "missing" and get pruned.

        Returns the number of records deferred to a later poll, so a
        caller can log or surface "still fetching" rather than having to
        infer it."""
        seen_ids = set()
        downloaded = 0
        deferred = 0
        for record in records:
            # Before the bound check, deliberately: a record deferred to
            # the next poll must still count as *seen*, or the pass below
            # would start aging out images that are missing only because
            # we chose not to download them yet.
            seen_ids.add(record.id)
            entry = self._manifest.get(record.id)
            if entry is not None:
                entry.missing_polls = 0
                continue
            if downloaded >= max_downloads:
                deferred += 1
                continue
            if self._download(record, source):
                downloaded += 1
            else:
                # A failed download still counts against the bound. It
                # cost a network round-trip and main-thread time either
                # way, and a source failing every fetch must not be able
                # to spin through the whole pool on one tick.
                downloaded += 1

        if deferred:
            logger.info(
                "cache.py: downloaded %d image(s) this poll; %d more will "
                "follow on the next one (bound: %d).",
                downloaded,
                deferred,
                max_downloads,
            )

        for image_id, entry in list(self._manifest.items()):
            if image_id in seen_ids:
                continue
            entry.missing_polls += 1
            if entry.missing_polls > MISS_GRACE_POLLS:
                self._evict(image_id)

        self._enforce_size_ceiling()
        self._save_manifest()
        return deferred
