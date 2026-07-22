#!/usr/bin/env python3
"""Step 0 / Step 0b smoke test.

Purpose: answer exactly one question — can a PyObjC/AppKit process draw a
window on the Napster View, and does it land on the *right* physical
screen? Nothing else. No calibration, no Image Server, no rotation.

What it does:
  1. Enumerates NSScreen.screens(), finds every *non-main* screen whose
     pixel resolution matches the View's known framebuffer resolution
     (960x960 @ 60Hz, per probe/DECISION.md §1).
  2. Refuses to draw and exits non-zero if that match is not exactly one
     screen — mirrors the resolution-match heuristic the plan's
     `display_target.py` will implement later, rather than guessing
     which screen to use.
  3. Opens a borderless, non-activating NSWindow filled with a single flat
     magenta fill covering that screen's full frame, and keeps an
     NSApplication run loop alive until the process is killed.

Run this ONLY from a Terminal at the mini's own physical console, or a
Screen Sharing session with control — never over SSH. Per
probe/DECISION.md §8, an SSH-attached process has no window-server
identity; it will not error, it will just silently fail to draw anything
visible. See display/STEP0_INSTRUCTIONS.md.
"""

from __future__ import annotations

import signal
import sys
from typing import NoReturn

import AppKit
import Foundation

# The View's known framebuffer resolution, from probe/DECISION.md §1
# ("Reported framebuffer resolution | 960 x 960 @ 60Hz, non-mirrored").
# Hardcoded here deliberately — Step 0/0b is scoped narrowly and does not
# yet have calibration.json or display_target.py (those are Step 1/Step 2).
EXPECTED_WIDTH_PX = 960
EXPECTED_HEIGHT_PX = 960

# A color that could never be mistaken for a normal desktop, wallpaper, or
# app window: full-intensity magenta, fully opaque.
SMOKE_TEST_COLOR = AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(
    1.0, 0.0, 1.0, 1.0
)


def _screen_pixel_size(screen: AppKit.NSScreen) -> tuple[int, int]:
    """Return (width_px, height_px) for a screen's true pixel resolution.

    NSScreen.frame() is in points, not pixels. For non-Retina external
    displays backingScaleFactor is 1.0 and points == pixels, but computing
    this explicitly (rather than assuming 1.0) is correct for any screen.
    """
    frame = screen.frame()
    scale = screen.backingScaleFactor()
    width_px = round(frame.size.width * scale)
    height_px = round(frame.size.height * scale)
    return width_px, height_px


def find_target_screen() -> AppKit.NSScreen:
    """Find the single non-main screen matching the View's resolution.

    Exits the process non-zero (via die()) if zero or more than one screen
    matches, rather than guessing. This is the resolution-match heuristic.
    """
    main_screen = AppKit.NSScreen.mainScreen()
    all_screens = list(AppKit.NSScreen.screens())

    if not all_screens:
        die("NSScreen.screens() returned zero screens. This should never "
            "happen on a machine with any display attached — if you see "
            "this, something is structurally wrong with this session's "
            "window-server access (see probe/DECISION.md §8: SSH sessions "
            "get no window-server identity at all).")

    candidates: list[AppKit.NSScreen] = []
    print(f"Enumerating {len(all_screens)} screen(s):", file=sys.stderr)
    for screen in all_screens:
        is_main = screen == main_screen
        width_px, height_px = _screen_pixel_size(screen)
        print(
            f"  - frame={screen.frame()} "
            f"pixel_size=({width_px}x{height_px}) "
            f"is_main={is_main}",
            file=sys.stderr,
        )
        if is_main:
            continue
        if width_px == EXPECTED_WIDTH_PX and height_px == EXPECTED_HEIGHT_PX:
            candidates.append(screen)

    if len(candidates) == 0:
        die(
            f"No non-main screen matches the View's expected resolution "
            f"({EXPECTED_WIDTH_PX}x{EXPECTED_HEIGHT_PX}). Is the View "
            f"plugged in and powered? Has its resolution changed since "
            f"probe/DECISION.md was written? Refusing to guess which "
            f"screen to draw on."
        )
    if len(candidates) > 1:
        die(
            f"{len(candidates)} non-main screens match the View's expected "
            f"resolution ({EXPECTED_WIDTH_PX}x{EXPECTED_HEIGHT_PX}) — "
            f"ambiguous, refusing to guess which one is the View. This "
            f"needs a better disambiguation heuristic before "
            f"proceeding."
        )

    return candidates[0]


def die(message: str) -> NoReturn:
    print(f"FATAL: {message}", file=sys.stderr)
    sys.exit(1)


def build_window(screen: AppKit.NSScreen) -> AppKit.NSWindow:
    """Build a borderless, non-activating, all-spaces window filled with
    SMOKE_TEST_COLOR, sized to exactly cover `screen`'s frame."""
    window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_screen_(
        screen.frame(),
        AppKit.NSWindowStyleMaskBorderless,
        AppKit.NSBackingStoreBuffered,
        False,
        screen,
    )

    window.setBackgroundColor_(SMOKE_TEST_COLOR)
    window.setOpaque_(True)
    window.setHasShadow_(False)
    window.setIgnoresMouseEvents_(True)
    # Never becomes key window — this is a passive display surface, not
    # an interactive one, matching the real app's eventual design.
    window.setLevel_(AppKit.NSScreenSaverWindowLevel)
    window.setCollectionBehavior_(
        AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
        | AppKit.NSWindowCollectionBehaviorStationary
        | AppKit.NSWindowCollectionBehaviorIgnoresCycle
    )
    window.setFrame_display_(screen.frame(), True)
    return window


def install_signal_handlers(app: AppKit.NSApplication) -> None:
    """Let SIGINT/SIGTERM cleanly terminate the NSApplication run loop
    instead of hanging around, so both an interactive Ctrl+C and a
    `launchctl bootout` / kill signal shut this down promptly."""

    def _handler(signum: int, _frame: object) -> None:
        print(f"Received signal {signum}, terminating.", file=sys.stderr)
        AppKit.NSApplication.sharedApplication().terminate_(None)

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


def main() -> None:
    print("view-lab smoke test starting.", file=sys.stderr)
    print(
        f"Looking for a non-main screen at "
        f"{EXPECTED_WIDTH_PX}x{EXPECTED_HEIGHT_PX}px...",
        file=sys.stderr,
    )

    target_screen = find_target_screen()
    print(f"Target screen found: {target_screen.frame()}", file=sys.stderr)

    app = AppKit.NSApplication.sharedApplication()
    # Accessory: no Dock icon, no menu bar — this is a headless-ish
    # background utility, not a foreground app the user interacts with.
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

    window = build_window(target_screen)
    window.orderFrontRegardless()

    install_signal_handlers(app)

    print(
        "Window should now be visible: solid magenta, filling the target "
        "screen. Running until killed (Ctrl+C or SIGTERM).",
        file=sys.stderr,
    )

    app.run()
    print("view-lab smoke test exiting cleanly.", file=sys.stderr)


if __name__ == "__main__":
    main()
