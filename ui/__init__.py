"""The menu bar app — a separate process from the display.

**Deliberately empty of imports**, matching `display/__init__.py`:
`menubar` imports AppKit and builds an NSStatusItem, so re-exporting it
here would mean `from ui import menubar_state` could not be imported by a
plain unit test without a window server. Import the specific module:

    from ui import menubar_state
    from ui.calibrate_state import CalibrationSession

Entry point is `ui.menubar:main`, invoked via
`packaging/imageview_main.py` with no arguments (the shipped bundle, and
the `dev.viewlab.imageview.ui` LaunchAgent through it).
"""

from __future__ import annotations

__all__: list[str] = []
