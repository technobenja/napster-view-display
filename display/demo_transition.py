"""Live on-device crossfade demo — "treat crossfade as a later
experiment the owner explicitly watches happen once on the real device."
This is that experiment, not the real app. Cycles through the real
starred pool via cache.py + rotation.py (the same components Step 6's
app.py will eventually use), crossfading between images on a short demo
interval so the effect can be watched end-to-end without waiting on the
real ~15-minute production interval.

    ./.venv/bin/python3 demo_transition.py [--interval 8] [--fade 2]

Run only from a Terminal at the mini's own physical console, or Screen
Sharing with control — same rule as every other display/*.py script.
"""

from __future__ import annotations

import argparse
import sys

import AppKit
import objc

from display.cache import ImageCache
from display.calibration import load_calibration_resolved
from display.display_target import get_view_screen
from display.rotation import Rotation
from display.smoke_test import die, install_signal_handlers
from display.window import build_window


class DemoDriver(AppKit.NSObject):
    """Owns the demo's own timer (separate from CircularImageView's fade
    timer) that decides *when* to advance to the next image."""

    def initWithView_cache_rotation_interval_fade_(
        self, view, cache, rotation, interval, fade_duration
    ):
        self = objc.super(DemoDriver, self).init()
        if self is None:
            return None
        self._view = view
        self._cache = cache
        self._rotation = rotation
        self._interval = interval
        self._fade_duration = fade_duration
        return self

    def advance_(self, timer) -> None:
        image_id = self._rotation.next_image()
        path = self._cache.get_path(image_id) if image_id else None
        print(
            f"  -> transitioning to {image_id} ({path}), "
            f"fade={self._fade_duration}s",
            file=sys.stderr,
        )
        self._view.transitionToImagePath_duration_(path, self._fade_duration)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--interval", type=float, default=8.0,
        help="Seconds between transitions (demo pacing, not the real "
        "~15 min production interval). Default 8.",
    )
    parser.add_argument(
        "--fade", type=float, default=2.0,
        help="Crossfade duration in seconds. Default 2.",
    )
    args = parser.parse_args()

    print("view-lab crossfade demo starting.", file=sys.stderr)
    # The resolution order — same circle the running service draws.
    calibration = load_calibration_resolved().value

    cache = ImageCache()
    ids = list(cache.known_ids())
    if not ids:
        die(
            "the image cache is empty — run Step 3's population step "
            "first (image_pool.py + cache.py against the starred pool)."
        )
    print(f"  {len(ids)} cached images available.", file=sys.stderr)

    rotation = Rotation(ids)

    target_screen = get_view_screen(calibration)
    if target_screen is None:
        die("No target screen resolved — is the View plugged in?")
    print(f"Target screen found: {target_screen.frame()}", file=sys.stderr)

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

    window = build_window(target_screen, calibration)
    view = window.contentView()

    first_id = rotation.current()
    view.setImagePath_(cache.get_path(first_id) if first_id else None)
    window.orderFrontRegardless()

    driver = DemoDriver.alloc().initWithView_cache_rotation_interval_fade_(
        view, cache, rotation, args.interval, args.fade
    )
    demo_timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        args.interval, driver, "advance:", None, True
    )

    install_signal_handlers(app)

    print(
        f"Cycling every {args.interval}s with a {args.fade}s crossfade. "
        f"Running until killed (Ctrl+C or SIGTERM).",
        file=sys.stderr,
    )
    app.run()
    demo_timer.invalidate()
    print("view-lab crossfade demo exiting cleanly.", file=sys.stderr)


if __name__ == "__main__":
    main()
