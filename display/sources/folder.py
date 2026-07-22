"""`FolderSource` — a folder on this Mac. The default source.

`caches = False`. A local folder needs no cache: the bytes are already on
this disk, and copying up to 300 of the user's own photos into
`~/Library/Caches/<bundle-id>/` would double their disk usage to no end.
So this source serves paths directly and `ImageCache` is bypassed
entirely for it.

That decision is exactly why the safety layer insists the magic-byte and dimension
checks live at the *decode* boundary rather than in cache-sync: for the
source most people will use, cache-sync never runs at all. A 60000x60000
PNG in the user's own Pictures folder decodes to ~14GB on the main thread
inside `drawRect_`, and no amount of hardening in `cache.py` would touch
it. `image_safety.validate_file()` is what does.

Ids are `sha256(absolute_path)[:16]` — stable across runs because
`rotation.py` persists walk order keyed by id. Renaming or moving a file
changes its id, and the picture re-enters the rotation as a new one.
"""

from __future__ import annotations

import logging
from pathlib import Path

from display import image_safety
from display.sources.base import ImageRecord, ImageSource, make_display_label, stable_id_from_path

logger = logging.getLogger(__name__)

# Ten seconds, not the HTTP sources' thirty minutes. Half an hour
# between dropping a photo into the folder and seeing it appear reads as
# broken, and a directory listing is cheap.
POLL_INTERVAL_S = 10.0

SORT_NAME = "name"
SORT_NEWEST = "newest"
SORT_OLDEST = "oldest"
VALID_SORT_ORDERS = frozenset({SORT_NAME, SORT_NEWEST, SORT_OLDEST})
DEFAULT_SORT_ORDER = SORT_NAME

# Bound on how many files one listing will consider, so pointing this at a
# home directory with include_subfolders on degrades to "the first 5000"
# rather than to a multi-minute stat storm on a 10s timer.
MAX_FILES = 5000


class FolderSource(ImageSource):
    kind = "folder"
    caches = False
    poll_interval_s = POLL_INTERVAL_S

    def __init__(
        self,
        folder: Path | str,
        include_subfolders: bool = False,
        sort_order: str = DEFAULT_SORT_ORDER,
    ) -> None:
        self._folder = Path(folder).expanduser()
        self._include_subfolders = bool(include_subfolders)
        self._sort_order = (
            sort_order if sort_order in VALID_SORT_ORDERS else DEFAULT_SORT_ORDER
        )
        # Validation memo keyed by (path, size, mtime). Re-reading the
        # header of every file every ten seconds would be pure waste; a
        # file whose size and mtime are unchanged cannot have become a
        # different image.
        self._validated: dict[tuple[str, int, float], bool] = {}

    @property
    def folder(self) -> Path:
        return self._folder

    def identity(self) -> str:
        return f"{self.kind}:{self._folder}"

    @property
    def label(self) -> str:
        """The folder's own name, not its path — "Pictures", not
        "/Users/someone/Pictures". The full path is a identity leak in
        any string that reaches a screenshot or a log someone pastes."""
        return self._folder.name or str(self._folder)

    # -- listing ---------------------------------------------------------

    def _candidate_paths(self) -> list[Path]:
        pattern = "**/*" if self._include_subfolders else "*"
        try:
            entries = sorted(self._folder.glob(pattern))
        except OSError as exc:
            logger.warning("folder: cannot list %s (%s).", self._folder, exc)
            return []

        paths: list[Path] = []
        for entry in entries:
            if len(paths) >= MAX_FILES:
                logger.warning(
                    "folder: %s has more than %d images; using the first %d.",
                    self._folder,
                    MAX_FILES,
                    MAX_FILES,
                )
                break
            if entry.suffix.lower() not in image_safety.ALLOWED_EXTENSIONS:
                continue
            try:
                if not entry.is_file():
                    continue
            except OSError:
                # A broken symlink or a file that vanished mid-listing.
                continue
            paths.append(entry)
        return paths

    def _is_valid(self, path: Path) -> bool:
        try:
            stat = path.stat()
            key = (str(path), stat.st_size, stat.st_mtime)
        except OSError:
            return False
        cached = self._validated.get(key)
        if cached is not None:
            return cached
        ok = image_safety.validate_file(path)
        self._validated[key] = ok
        return ok

    def _sort_key(self, path: Path):
        if self._sort_order == SORT_NAME:
            return path.name.lower()
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        return -mtime if self._sort_order == SORT_NEWEST else mtime

    def list_images(self) -> list[ImageRecord]:
        """Every readable, valid PNG/JPEG in the folder. `[]` — never an
        exception — if the folder is gone, unreadable, or TCC-blocked."""
        try:
            paths = [p for p in self._candidate_paths() if self._is_valid(p)]
            paths.sort(key=self._sort_key)
            return [
                ImageRecord(
                    id=stable_id_from_path(path),
                    filename=path.name,
                    display_label=make_display_label(path.stem, fallback=path.name),
                    locator=str(path.resolve()),
                )
                for path in paths
            ]
        except OSError as exc:
            logger.warning("folder: listing %s failed (%s).", self._folder, exc)
            return []

    # -- bytes -----------------------------------------------------------

    def path_for(self, record: ImageRecord) -> Path | None:
        """The point of `caches = False`: hand back the file where it
        already lives. Re-validated on every call rather than trusted from
        listing time — the file can be replaced between the two, and this
        return value goes straight to a decoder."""
        if not record.locator:
            return None
        path = Path(record.locator)
        if not self._is_valid(path):
            return None
        return path

    def fetch(self, record: ImageRecord) -> bytes | None:
        """Present for interface completeness (and for a future source
        that wants to cache a folder anyway); the display path uses
        `path_for` instead. Never raises."""
        path = self.path_for(record)
        if path is None:
            return None
        try:
            data = path.read_bytes()
        except OSError as exc:
            logger.warning("folder: cannot read %s (%s).", path, exc)
            return None
        if not image_safety.validate_bytes(data, label=str(path)):
            return None
        return data


__all__ = [
    "DEFAULT_SORT_ORDER",
    "MAX_FILES",
    "POLL_INTERVAL_S",
    "SORT_NAME",
    "SORT_NEWEST",
    "SORT_OLDEST",
    "VALID_SORT_ORDERS",
    "FolderSource",
]
