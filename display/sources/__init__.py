"""Image sources.

Three sources, three hand-written panels (the generic capability
descriptor is cut). The interface lives in `base.py`;
`factory.py` turns a validated `SourceSettings` into a live instance.

**Deliberately empty of imports, and enforced by a test.**
`display/image_pool.py` imports `display.sources.base`, and
`display.sources.image_server` imports `display.image_pool` — so
re-exporting the concrete sources from this package's `__init__` closes
that loop. It closes it *conditionally*, which is the dangerous part:
verified both ways, `import display.sources` first still works, while
`import display.image_pool` first raises `ImportError: cannot import
name 'REQUEST_TIMEOUT_S' from partially initialized module`. The obvious
tidy-up therefore looks correct locally and breaks on import order.

`display/test_package_layout.py` fails if anything is added here. If you
came to add the re-exports and that test stopped you, this is why —
break the `image_server` -> `image_pool` dependency first.

Import from the specific module instead:

    from display.sources.base import ImageRecord, ImageSource
    from display.sources.factory import build_source
"""

from __future__ import annotations

__all__: list[str] = []
