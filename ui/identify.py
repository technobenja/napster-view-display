"""Identify: flash every attached display with its own name.

Extracted from `settings_window.py` when the first-run flow needed the
same action. It is the same question in both places — "which of these is
the little round one" — and two copies would be two chances for the
first-run flow to flash only the selected screen, which cannot answer
that question at all.

**Every display, not only the selected one**, for exactly that reason.
The View gets a plain text card, which the calibration flow already established as
legitimate on that screen.
"""

from __future__ import annotations

import sys
import traceback

import AppKit
from Foundation import NSMakeRect

#: How long the flash stays up. Long enough to look up at a 2.1-inch
#: display on the far side of a desk and register it, short enough not to
#: feel like a fault.
IDENTIFY_SECONDS = 2.0


class _FlashDismisser(AppKit.NSObject):
    """NSTimer needs an Objective-C target. Module-level singleton below,
    so it outlives the controller that asked for the flash — a settings
    window closed during the two seconds must not take the timer's target
    with it."""

    def dismissFlash_(self, timer) -> None:
        try:
            window = timer.userInfo()
            if window is not None:
                window.orderOut_(None)
        except Exception:
            print(f"identify: dismissFlash:\n{traceback.format_exc()}", file=sys.stderr)


_dismisser = None


def _shared_dismisser():
    global _dismisser
    if _dismisser is None:
        _dismisser = _FlashDismisser.alloc().init()
    return _dismisser


def flash_screens(seconds: float = IDENTIFY_SECONDS) -> None:
    """Flash every attached display. Never raises."""
    try:
        screens = list(AppKit.NSScreen.screens())
    except Exception:  # noqa: BLE001 - an AppKit action
        print(f"identify: could not enumerate:\n{traceback.format_exc()}", file=sys.stderr)
        return
    for screen in screens:
        try:
            _flash(screen, seconds)
        except Exception:  # noqa: BLE001 - one bad screen must not stop the rest
            print(f"identify: flash failed:\n{traceback.format_exc()}", file=sys.stderr)


def _flash(screen, seconds: float) -> None:
    frame = screen.frame()
    window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_screen_(
        frame,
        AppKit.NSWindowStyleMaskBorderless,
        AppKit.NSBackingStoreBuffered,
        False,
        screen,
    )
    window.setLevel_(AppKit.NSScreenSaverWindowLevel + 1)
    window.setOpaque_(False)
    window.setBackgroundColor_(
        AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(0.1, 0.45, 0.95, 0.85)
    )
    window.setIgnoresMouseEvents_(True)
    window.setHasShadow_(False)
    window.setCollectionBehavior_(
        AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
        | AppKit.NSWindowCollectionBehaviorStationary
        | AppKit.NSWindowCollectionBehaviorIgnoresCycle
    )
    try:
        name = str(screen.localizedName())
    except Exception:  # noqa: BLE001 - older macOS, or a stub
        name = "This screen"

    label = AppKit.NSTextField.alloc().init()
    label.setStringValue_(name)
    label.setBezeled_(False)
    label.setDrawsBackground_(False)
    label.setEditable_(False)
    label.setSelectable_(False)
    label.setFont_(AppKit.NSFont.systemFontOfSize_(42.0))
    label.setTextColor_(AppKit.NSColor.whiteColor())
    label.setAlignment_(AppKit.NSTextAlignmentCenter)
    label.cell().setWraps_(True)
    label.setFrame_(NSMakeRect(0, frame.size.height / 2 - 30.0, frame.size.width, 60.0))
    window.contentView().addSubview_(label)

    # Same trailing setFrame_display_ as the calibration overlay: the
    # contentRect passed to the initializer is not the last word on where
    # a borderless window lands (verified on this device).
    window.setFrame_display_(frame, True)
    window.orderFrontRegardless()
    # Retained by the timer's userInfo until it fires, which is what keeps
    # the window alive without an ivar per flash.
    AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        seconds, _shared_dismisser(), "dismissFlash:", window, False
    )


__all__ = ["IDENTIFY_SECONDS", "flash_screens"]
