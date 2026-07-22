"""The display agent — the process that drives the View.

**Deliberately empty of imports**, for the same reason `sources/__init__.py`
is: `app` pulls in nearly every other module here, so re-exporting anything
from this file would make `import display.paths` execute `app` — dragging
AppKit, a window, and a control timer into every consumer, including the
menu bar process and every unit test. Import the specific module:

    from display.cache import ImageCache
    from display import paths

Entry point is `display.app:main`, invoked as `python3 -m display.app`
from the repo root (the display LaunchAgent) or via
`packaging/imageview_main.py --display` (the shipped bundle).
"""

from __future__ import annotations

__all__: list[str] = []
