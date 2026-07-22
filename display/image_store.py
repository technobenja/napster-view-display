"""`DirectStore` — the `caches = False` counterpart to `ImageCache`.

`FolderSource` sets `caches = False`: a local folder needs no cache,
and copying up to 300 of the user's own photos into
`~/Library/Caches/<bundle-id>/` would double their disk usage for nothing.

Rather than teaching the display loop to branch on `source.caches` at
every call site — resolve a path, mark something shown, ask what the pool
is — this presents the same four methods `app.py` already calls on
`ImageCache` and serves paths straight from the source. The loop stays
identical; only which object it holds changes.

`mark_shown` is a no-op here on purpose. It exists on `ImageCache` to
drive least-recently-shown eviction, and there is nothing to evict when
the files are the user's own and were never copied.
"""

from __future__ import annotations

import logging
from pathlib import Path

from display.sources.base import ImageRecord, ImageSource

logger = logging.getLogger(__name__)


class DirectStore:
    def __init__(self) -> None:
        self._paths: dict[str, Path] = {}

    def known_ids(self) -> set[str]:
        return set(self._paths)

    def sync(
        self,
        records: list[ImageRecord],
        source: ImageSource,
        max_downloads: int | None = None,
    ) -> int:
        """Rebuild the id -> path map from a fresh listing. No grace
        period and no eviction: unlike a cache, this map is not state
        worth protecting — it is a view of what is on disk right now, and
        a file that has gone away should leave immediately rather than
        linger for two polls.

        `max_downloads` is accepted and ignored, and the deferred count
        is always 0: bound exists to stop a poll from issuing
        hundreds of sequential *downloads*, and this store never
        downloads anything. Present only so `app.py` can keep calling one
        signature without branching on `source.caches` (see the module
        docstring)."""
        resolved: dict[str, Path] = {}
        for record in records:
            try:
                path = source.path_for(record)
            except Exception as exc:  # noqa: BLE001 - sources never raise, but
                logger.warning("image_store: path_for(%s) failed (%s).", record.id, exc)
                continue
            if path is not None:
                resolved[record.id] = path
        self._paths = resolved
        return 0

    def get_path(self, image_id: str) -> Path | None:
        """Never raises. A file that has vanished since the last listing
        is dropped rather than handed to the renderer."""
        path = self._paths.get(image_id)
        if path is None:
            return None
        try:
            if not path.exists():
                logger.warning("image_store: %s is gone; dropping it.", path)
                del self._paths[image_id]
                return None
        except OSError as exc:
            logger.warning("image_store: cannot stat %s (%s).", path, exc)
            return None
        return path

    def mark_shown(self, image_id: str) -> None:
        """No-op — see the module docstring."""


__all__ = ["DirectStore"]
