"""Resolves the Napster View's NSScreen among all connected displays —
resolution-match heuristic, generalized from Step 0's
smoke_test.py so later steps (window.py, app.py) can share it.

get_view_screen() returns None (logging loudly) rather than raising or
exiting when zero or more than one screen matches — callers are expected
to treat None as "target temporarily unavailable" (e.g. the View is
unplugged) and idle-and-poll, not crash. This differs from
smoke_test.py's find_target_screen(), which is a standalone diagnostic
script that intentionally dies loudly on a mismatch; that behavior is
still correct for a one-shot interactive check and is left as-is.

**Step 4 added the explicit picker.** The calibration file's
`target_screen` block can now name a display outright, and this module
honours it — *with a fallback to the heuristic*, never instead of it.
That fallback is the whole safety argument for shipping the picker
before stability gate ("test that it survives unplug/replug and a
reboot on the actual device") has been run: if the stable id turns out
not to be stable, the failure mode is "resolves the way it always did"
rather than "the View goes dark and the settings window is lying".
"""

from __future__ import annotations

import sys

import AppKit

from display.calibration import Calibration

#: `CGDirectDisplayID` — what
#: `NSScreen.deviceDescription()["NSScreenNumber"]` hands you — is
#: **not** a stable identity: WindowServer assigns it at attach time and
#: it changes across reboot and USB re-enumeration. A picker built on it
#: would silently stop matching after a replug and fall back to the very
#: heuristic it exists to replace, with no error anywhere. So the id
#: below is derived from the EDID triad instead.
EXPLICIT_STRATEGY = "explicit"


def _screen_pixel_size(screen: AppKit.NSScreen) -> tuple[int, int]:
    """Return (width_px, height_px) for a screen's true pixel resolution.

    NSScreen.frame() is in points, not pixels; backingScaleFactor() is
    computed explicitly rather than assumed 1.0 so this is correct for
    any screen, Retina or not.
    """
    frame = screen.frame()
    scale = screen.backingScaleFactor()
    return round(frame.size.width * scale), round(frame.size.height * scale)


def stable_display_id(screen: AppKit.NSScreen) -> str:
    """EDID-derived identity: `vendor-model-serial`.

    Returns "" rather than raising when the triad cannot be read — a
    display that will not identify itself is one the heuristic has to
    handle anyway, and an exception here would propagate into a screen
    enumeration that runs on a retry timer.

    Quartz is imported inside the function rather than at module scope
    because this module is imported by `app.py` at startup on the
    display path, and the framework wrapper is only needed by the two
    callers that enumerate displays.
    """
    try:
        import Quartz

        number = screen.deviceDescription().get("NSScreenNumber")
        if number is None:
            return ""
        vendor = Quartz.CGDisplayVendorNumber(number)
        model = Quartz.CGDisplayModelNumber(number)
        serial = Quartz.CGDisplaySerialNumber(number)
    except Exception:  # noqa: BLE001 - identity is never worth a crash
        return ""
    return f"{vendor}-{model}-{serial}"


def screen_records() -> list[dict]:
    """Every attached display, as plain dicts for picker.

    Plain dicts rather than NSScreen objects so the picker's sorting and
    labelling logic (`ui.settings_state.display_options`) stays free of
    AppKit and testable. Never raises: a screen that cannot be described
    is skipped rather than taking the list with it, because the safety rules require
    the list be populated even when the interesting entry is missing.
    """
    records: list[dict] = []
    try:
        main_screen = AppKit.NSScreen.mainScreen()
        screens = list(AppKit.NSScreen.screens())
    except Exception:  # noqa: BLE001 - an empty picker beats a crash
        return records
    for screen in screens:
        try:
            width, height = _screen_pixel_size(screen)
            try:
                name = str(screen.localizedName())
            except Exception:  # noqa: BLE001 - older macOS, or a stub
                name = "Display"
            records.append(
                {
                    "name": name,
                    "width": width,
                    "height": height,
                    "is_main": screen == main_screen,
                    "display_id": stable_display_id(screen),
                }
            )
        except Exception:  # noqa: BLE001 - skip this one, keep the list
            continue
    return records


def screen_for_display_id(display_id: str) -> AppKit.NSScreen | None:
    """The attached screen whose EDID triad matches, or None."""
    if not display_id:
        return None
    try:
        screens = list(AppKit.NSScreen.screens())
    except Exception:  # noqa: BLE001
        return None
    for screen in screens:
        if stable_display_id(screen) == display_id:
            return screen
    return None


def get_view_screen(calibration: Calibration) -> AppKit.NSScreen | None:
    """Find the View's screen: the explicit choice if there is a usable
    one, otherwise resolution-match heuristic.

    Returns None and logs to stderr if the heuristic finds zero or more
    than one match, rather than guessing which one is the View.
    """
    display_id = getattr(calibration, "target_display_id", "")
    strategy = getattr(calibration, "target_strategy", "")
    if strategy == EXPLICIT_STRATEGY and display_id:
        chosen = screen_for_display_id(display_id)
        if chosen is not None:
            return chosen
        # Deliberately falls through rather than returning None. The
        # explicit choice being absent is the *expected* state whenever
        # the View is unplugged, and it is also what a non-stable id
        # would look like — and in both cases the heuristic is a better
        # answer than a dark screen. Logged once per attempt so a picker
        # choice that has quietly stopped matching is discoverable.
        print(
            f"display_target.py: no attached display matches the chosen "
            f"id {display_id!r}; falling back to the resolution "
            f"heuristic.",
            file=sys.stderr,
        )

    expected = (
        round(calibration.framebuffer_width),
        round(calibration.framebuffer_height),
    )
    main_screen = AppKit.NSScreen.mainScreen()
    all_screens = list(AppKit.NSScreen.screens())

    matches = [
        screen
        for screen in all_screens
        if screen != main_screen and _screen_pixel_size(screen) == expected
    ]

    if not matches:
        print(
            f"display_target.py: no non-main screen at "
            f"{expected[0]}x{expected[1]}px found among "
            f"{len(all_screens)} screen(s) — View may be unplugged.",
            file=sys.stderr,
        )
        return None

    if len(matches) > 1:
        print(
            f"display_target.py: {len(matches)} screens match "
            f"{expected[0]}x{expected[1]}px — refusing to guess which "
            f"one is the View.",
            file=sys.stderr,
        )
        return None

    return matches[0]


__all__ = [
    "EXPLICIT_STRATEGY",
    "get_view_screen",
    "screen_for_display_id",
    "screen_records",
    "stable_display_id",
]
