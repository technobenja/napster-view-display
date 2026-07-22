"""Borderless NSWindow + circular-masked image drawing.

The one module in this project that's *supposed* to depend on AppKit for
drawing — calibration.py, display_target.py, rotation.py, cache.py,
and image_pool.py all deliberately don't, so they stay testable without a
window-server session. This is where those pieces converge: given a
Calibration and a file path, draw a cover-fit, circularly-clipped image into a borderless window on the View's screen, falling back to
a solid fill — never a crash, never a blank/black frame — when there's
nothing to show.

Standalone verification (Step 5):

    ./.venv/bin/python3 window.py path/to/image.png
    ./.venv/bin/python3 window.py            # draws the empty-pool fallback

Run only from a Terminal at the mini's own physical console, or Screen
Sharing with control — same rule as smoke_test.py and pattern.py.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import AppKit
import objc

from display.calibration import Calibration, load_calibration_resolved
from display.display_target import get_view_screen
from display.smoke_test import die, install_signal_handlers

# "Solid muted charcoal fill, not black — black reads as
# off/disconnected, charcoal reads as on, nothing to show yet."
#
# Blanking wants precisely the read this colour was chosen to
# avoid, which is why it draws **true black** and not this: charcoal
# means "on, nothing to show yet", and a blanked View is asking to look
# off. The two states are visually distinct on purpose, and the blank
# branch in drawRect_ sits *ahead of* the empty-fill branch so a blanked
# empty pool reads as blanked rather than as empty.
EMPTY_FILL_COLOR = AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(
    0.16, 0.16, 0.16, 1.0
)


def source_crop_rect(image_size: tuple[float, float]) -> AppKit.NSRect:
    """Centered square crop, independent of destination size. A
    scale factor computed from the destination and divided back out would
    algebraically cancel — this never needs to know the radius, which is
    why it's kept as a separate function from destination_rect rather
    than one signature that looks radius-dependent but isn't (an earlier
    plan draft made exactly that mistake)."""
    src_w, src_h = image_size
    side = min(src_w, src_h)
    return AppKit.NSMakeRect((src_w - side) / 2.0, (src_h - side) / 2.0, side, side)


def destination_rect(calibration: Calibration) -> AppKit.NSRect:
    """Where the cropped square gets drawn — the one piece of this that
    *does* depend on radius."""
    r = calibration.effective_radius_px
    return AppKit.NSMakeRect(
        calibration.center_x - r, calibration.center_y - r, 2 * r, 2 * r
    )


def _clip_path(calibration: Calibration) -> AppKit.NSBezierPath:
    return AppKit.NSBezierPath.bezierPathWithOvalInRect_(destination_rect(calibration))


CROSSFADE_FPS = 30.0


def _load_image(path: str | Path | None) -> AppKit.NSImage | None:
    """A decode failure — missing file, corrupt data — returns None,
    which callers treat identically to "no image" (fallback fill),
    never raising."""
    if path is None:
        return None
    return AppKit.NSImage.alloc().initWithContentsOfFile_(str(path))


class CircularImageView(AppKit.NSView):
    """Draws either a cover-fit, circularly-clipped image, or the solid
    fallback fill when there's no image to show. `calibration` is set
    as a plain attribute after construction, matching this project's
    existing PatternView convention (pattern.py).

    Crossfade support ("treat crossfade as a later experiment the
    owner explicitly watches happen once on the real device") is opt-in
    via transitionToImagePath_duration_ — setImagePath_ remains a plain
    instant swap, unrelated to and unaffected by any in-progress fade.

    Blanking is a **separate** pair,
    fadeToBlackWithDuration_/restoreToImagePath_duration_, with its own
    flag, progress value and timer. It is not expressible in terms of the
    crossfade, and the plan records why after the behaviour was verified
    on the real code: `transitionToImagePath_duration_(None, 2.0)` does
    *not* fade out. `drawRect_`'s blend branch is skipped when `_image`
    is None, so that call draws the outgoing picture at **full opacity**
    for the entire duration and then snaps to the charcoal empty-fill —
    a hard cut wearing a crossfade's clothes, and to the wrong colour.

    Keeping the two independent is also what makes "rotation is
    paused, not hidden" true at the renderer: blanking never touches
    `_image`, so restoring shows the same picture you left rather than
    reloading or advancing.
    """

    calibration: Calibration | None = None
    _image: AppKit.NSImage | None = None
    _fade_from_image: AppKit.NSImage | None = None
    _fade_progress: float = 1.0  # 1.0 = fully on _image, no fade in progress
    _fade_timer: AppKit.NSTimer | None = None
    _fade_start: float = 0.0
    _fade_duration: float = 0.0
    # `_blanked` is the desired state (set the instant a blank is
    # requested, cleared the instant a restore is); `_blank_progress` is
    # where the animation currently is, 0.0 = picture, 1.0 = true black.
    # They are separate because the answer to "is this View blanked?" must
    # not depend on whether a 2-second animation has finished yet.
    _blanked: bool = False
    _blank_progress: float = 0.0
    _blank_timer: AppKit.NSTimer | None = None
    _blank_from: float = 0.0
    _blank_to: float = 0.0
    _blank_start: float = 0.0
    _blank_duration: float = 0.0

    def isFlipped(self) -> bool:
        # Matches calibration.json's coordinate convention (top-left
        # origin, y increases downward) — see pattern.py's PatternView
        # for the same override and why it matters: get this wrong and
        # the mask is vertically mirrored relative to the calibration.
        return True

    def setImagePath_(self, path: str | Path | None) -> None:
        """Instant swap, no fade. Cancels any in-progress crossfade."""
        if self._fade_timer is not None:
            self._fade_timer.invalidate()
            self._fade_timer = None
        self._fade_from_image = None
        self._fade_progress = 1.0
        self._image = _load_image(path)
        self.setNeedsDisplay_(True)

    def transitionToImagePath_duration_(self, path: str | Path | None, duration: float) -> None:
        """Crossfade from whatever's currently shown to a new image over
        `duration` seconds. A duration <= 0 behaves like setImagePath_."""
        new_image = _load_image(path)
        if duration <= 0:
            self.setImagePath_(path)
            return

        if self._fade_timer is not None:
            self._fade_timer.invalidate()
            self._fade_timer = None

        self._fade_from_image = self._image  # may itself be None (empty fill) - handled in draw
        self._image = new_image
        self._fade_progress = 0.0
        self._fade_start = time.monotonic()
        self._fade_duration = duration
        self._fade_timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1.0 / CROSSFADE_FPS, self, "fadeTick:", None, True
        )

    def fadeTick_(self, timer: AppKit.NSTimer) -> None:
        elapsed = time.monotonic() - self._fade_start
        self._fade_progress = min(1.0, elapsed / self._fade_duration)
        self.setNeedsDisplay_(True)
        if self._fade_progress >= 1.0:
            timer.invalidate()
            self._fade_timer = None
            self._fade_from_image = None

    # -- blanking -------------------------------------------------

    def isBlanked(self) -> bool:
        """Desired state, not animation state: True from the moment a
        blank is requested, regardless of how far the fade has run."""
        return self._blanked

    @objc.python_method
    def _animate_blank(self, target: float, duration: float) -> None:
        """Drive `_blank_progress` toward `target` over `duration`.

        @objc.python_method per this project's convention for private
        helpers on an NSObject subclass — a prior bug here was a
        snake_case underscore-prefixed method colliding with an internal
        AppKit selector."""
        if self._blank_timer is not None:
            self._blank_timer.invalidate()
            self._blank_timer = None
        if duration <= 0:
            self._blank_progress = target
            self.setNeedsDisplay_(True)
            return
        self._blank_from = self._blank_progress
        self._blank_to = target
        self._blank_start = time.monotonic()
        self._blank_duration = duration
        self._blank_timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1.0 / CROSSFADE_FPS, self, "blankTick:", None, True
        )

    def blankTick_(self, timer: AppKit.NSTimer) -> None:
        elapsed = time.monotonic() - self._blank_start
        fraction = min(1.0, elapsed / self._blank_duration)
        self._blank_progress = (
            self._blank_from + (self._blank_to - self._blank_from) * fraction
        )
        self.setNeedsDisplay_(True)
        if fraction >= 1.0:
            self._blank_progress = self._blank_to
            timer.invalidate()
            self._blank_timer = None

    def fadeToBlackWithDuration_(self, duration: float) -> None:
        """Fade the View to **true black** over `duration` seconds.

        Deliberately does not touch `_image`, `orderOut_` the window, or
        stop anything: the picture underneath must stay
        exactly where it is (ordering the window out would reveal the
        desktop wallpaper on the View, which is the opposite of blank).
        Rotation being held is `app.py`'s and `Rotation`'s business.

        Idempotent — calling it on an already-blanked, already-settled
        View does nothing rather than restarting the animation, so the
        level-triggered control channel can apply desired state as
        often as it likes."""
        if self._blanked and self._blank_timer is None and self._blank_progress >= 1.0:
            return
        self._blanked = True
        self._animate_blank(1.0, duration)

    def restoreToImagePath_duration_(
        self, path: str | Path | None, duration: float
    ) -> None:
        """Bring the picture back over `duration` seconds.

        `path` is optional and exists for the case where the picture
        changed while the View was dark (the display agent restarted, the
        pool was re-synced). Pass None — the normal case — to restore
        whatever is already loaded, which is what makes "restoring
        shows the same picture you left" true without a reload or a
        re-decode.

        Idempotent in the same way as its counterpart."""
        if path is not None:
            self._image = _load_image(path)
        if not self._blanked and self._blank_timer is None and self._blank_progress <= 0.0:
            self.setNeedsDisplay_(True)
            return
        self._blanked = False
        self._animate_blank(0.0, duration)

    @objc.python_method
    def _draw_image(self, image: AppKit.NSImage, fraction: float, operation: int) -> None:
        size = image.size()
        # respectFlipped=True is required here: the plain 4-arg
        # drawInRect:fromRect:operation:fraction: does not account for
        # the destination view's isFlipped (True, above) and draws the
        # source image upside down as a result - a well-documented
        # AppKit quirk, confirmed the hard way against the physical
        # device (Step 5, 2026-07-17).
        image.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
            destination_rect(self.calibration),
            source_crop_rect((size.width, size.height)),
            operation,
            fraction,
            True,
            None,
        )

    @objc.python_method
    def _draw_content(self, clip_path: AppKit.NSBezierPath) -> None:
        """The picture (or the empty-fill) inside the circle — every
        drawing case that is not blanking."""
        fading = self._fade_progress < 1.0
        if self._image is None and not (fading and self._fade_from_image is not None):
            EMPTY_FILL_COLOR.set()
            clip_path.fill()
            return

        AppKit.NSGraphicsContext.saveGraphicsState()
        clip_path.addClip()

        if fading and self._fade_from_image is not None:
            # Base layer first (opaque), new image blended over it at the
            # current fade fraction - a standard linear crossfade.
            self._draw_image(
                self._fade_from_image, 1.0, AppKit.NSCompositingOperationCopy
            )
            if self._image is not None:
                self._draw_image(
                    self._image,
                    self._fade_progress,
                    AppKit.NSCompositingOperationSourceOver,
                )
        elif self._image is not None:
            self._draw_image(self._image, 1.0, AppKit.NSCompositingOperationCopy)

        AppKit.NSGraphicsContext.restoreGraphicsState()

    def drawRect_(self, dirty_rect: AppKit.NSRect) -> None:
        AppKit.NSColor.blackColor().set()
        AppKit.NSBezierPath.fillRect_(self.bounds())

        if self.calibration is None:
            return

        clip_path = _clip_path(self.calibration)

        # ordering requirement: the blank branch sits **ahead of**
        # the empty-fill branch. The whole view was just filled with
        # black above, so a fully-blanked View is already correct and
        # returning here is what keeps a blanked *empty* pool reading as
        # blanked (true black) rather than as empty (charcoal).
        if self._blank_progress >= 1.0:
            return

        self._draw_content(clip_path)

        if self._blank_progress > 0.0:
            # Mid-animation: the content above, then black over it inside
            # the same circle at the current opacity. Composited rather
            # than interpolated per-pixel because the content underneath
            # may itself be a crossfade in progress, and one alpha over
            # the finished result is both simpler and correct.
            AppKit.NSGraphicsContext.saveGraphicsState()
            AppKit.NSColor.blackColor().colorWithAlphaComponent_(
                self._blank_progress
            ).set()
            clip_path.fill()
            AppKit.NSGraphicsContext.restoreGraphicsState()


def build_window(screen: AppKit.NSScreen, calibration: Calibration) -> AppKit.NSWindow:
    """Borderless, non-activating, all-spaces window covering `screen`,
    with a CircularImageView as its content view. Mirrors
    smoke_test.py/pattern.py's build_window() window scaffolding."""
    window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_screen_(
        screen.frame(),
        AppKit.NSWindowStyleMaskBorderless,
        AppKit.NSBackingStoreBuffered,
        False,
        screen,
    )
    window.setBackgroundColor_(AppKit.NSColor.blackColor())
    window.setOpaque_(True)
    window.setHasShadow_(False)
    window.setIgnoresMouseEvents_(True)
    window.setLevel_(AppKit.NSScreenSaverWindowLevel)
    window.setCollectionBehavior_(
        AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
        | AppKit.NSWindowCollectionBehaviorStationary
        | AppKit.NSWindowCollectionBehaviorIgnoresCycle
    )

    view = CircularImageView.alloc().initWithFrame_(
        AppKit.NSMakeRect(
            0.0, 0.0, screen.frame().size.width, screen.frame().size.height
        )
    )
    view.calibration = calibration
    window.setContentView_(view)
    window.setFrame_display_(screen.frame(), True)
    return window


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Draw a real image, cover-fit and circularly masked to the "
            "calibrated circle, on the View. Step 5 verification tool."
        )
    )
    parser.add_argument(
        "image_path",
        nargs="?",
        default=None,
        help="Path to an image file to draw. Omit to draw the empty-pool "
        "fallback fill instead.",
    )
    args = parser.parse_args()

    print("view-lab window.py starting.", file=sys.stderr)
    # Resolved via order (`~/.viewlab/` first) so this standalone
    # verification tool draws the same circle the running service does —
    # before Step -1 it read the bundled seed directly and would have
    # silently disagreed with the service after any calibration edit.
    calibration = load_calibration_resolved().value
    print(
        f"  calibration: center=({calibration.center_x}, "
        f"{calibration.center_y}) radius={calibration.radius_px} "
        f"effective_radius={calibration.effective_radius_px:.1f}",
        file=sys.stderr,
    )

    target_screen = get_view_screen(calibration)
    if target_screen is None:
        die(
            "No target screen resolved (see display_target.py's log "
            "above) — is the View plugged in?"
        )
    print(f"Target screen found: {target_screen.frame()}", file=sys.stderr)

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

    window = build_window(target_screen, calibration)
    window.contentView().setImagePath_(args.image_path)
    window.orderFrontRegardless()

    install_signal_handlers(app)

    if args.image_path is None:
        print(
            "No image given — drawing the empty-pool fallback fill. "
            "Running until killed (Ctrl+C or SIGTERM).",
            file=sys.stderr,
        )
    else:
        print(
            "Image should now be visible, cover-fit and circularly "
            "masked. Running until killed (Ctrl+C or SIGTERM).",
            file=sys.stderr,
        )
    app.run()
    print("view-lab window.py exiting cleanly.", file=sys.stderr)


if __name__ == "__main__":
    main()
