"""Turn a validated `SourceSettings` into a live `ImageSource`.

Kept apart from `base.py` so the interface has no knowledge of its
implementations, and apart from `settings.py` so config validation stays
free of httpx and filesystem concerns.
"""

from __future__ import annotations

import logging

from display import source_settings
from display.source_settings import SourceSettings
from display.sources.base import ImageSource
from display.sources.folder import FolderSource
from display.sources.image_server import ImageServerSource
from display.sources.json_url import JsonUrlSource

logger = logging.getLogger(__name__)


def build_source(settings: SourceSettings) -> ImageSource:
    """Construct the configured source. Falls back to a folder source on
    an unrecognized kind rather than raising — this runs at startup on a
    machine driving a display, where "show nothing and exit" is the worst
    available outcome."""
    if settings.kind == source_settings.KIND_JSON_URL:
        return JsonUrlSource(list_url=settings.list_url)
    if settings.kind == source_settings.KIND_IMAGE_SERVER:
        return ImageServerSource(base_url=settings.base_url, pool=settings.pool)
    if settings.kind != source_settings.KIND_FOLDER:
        logger.warning(
            "sources: unknown source kind %r; falling back to a folder source.",
            settings.kind,
        )
    return FolderSource(
        folder=settings.folder or source_settings.default_folder(),
        include_subfolders=settings.include_subfolders,
        sort_order=settings.sort_order,
    )


__all__ = ["build_source"]
