#!/usr/bin/env python3
"""Step 1 — calibration test pattern + validation-ring mode.

Purpose: render the 960x960 calibration test pattern so the owner can
photograph the physical
View and read off center/radius offsets by hand (Mode 1), then re-render
the same pattern with a single green validation ring at the computed
values so the owner can confirm the reading by eye (Mode 2).

Two modes, selected by CLI argument:

    python3 pattern.py                                       # Mode 1
    python3 pattern.py --validate CENTER_X CENTER_Y RADIUS    # Mode 2

Deliberately reuses smoke_test.py's screen-resolution-match heuristic
(`find_target_screen`) and signal-handling (`install_signal_handlers`)
rather than reimplementing them — this script is still narrowly-scoped
Step 1 work and does not yet have the shared `display_target.py`
module the plan reserves for Step 2.

Run this ONLY from a Terminal at the mini's own physical console, or a
Screen Sharing session with control — never over SSH. Per
probe/DECISION.md §8, an SSH-attached process has no window-server
identity; it will not error, it will just silently fail to draw anything
visible. See display/STEP1_INSTRUCTIONS.md.
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from typing import Sequence

import AppKit

from display.smoke_test import die, find_target_screen, install_signal_handlers

# ---------------------------------------------------------------------------
# Canvas geometry
# ---------------------------------------------------------------------------

# The View's known framebuffer resolution (see smoke_test.py and
# probe/DECISION.md §1). find_target_screen() already only matches a
# screen at this exact pixel resolution, and — same assumption smoke_test
# makes — the View is a non-Retina external display, so points == pixels
# and the window's content-view frame can be drawn against directly as a
# 960x960 canvas with no extra scale factor.
CANVAS_SIZE = 960.0
CENTER: tuple[float, float] = (480.0, 480.0)

# Coordinate convention (important, applies to every function below):
# PatternView.isFlipped() returns True, so (0, 0) is the *top-left*
# corner of the canvas and y increases *downward* — this matches both
# the framebuffer/photo convention the corner labels use, and how the
# calibration workflow formulas read offsets off a photo (e.g.
# `center_y_offset = (bottom - top) / 2`: a positive offset here means
# "shifted toward the bottom of the image", which is exactly what
# center_y = 480 + center_y_offset expects).

RING_RADII: tuple[int, ...] = tuple(range(40, 481, 40))  # 40..480, 12 rings
COMB_RADII: frozenset[int] = frozenset({440, 480})
TICK_STEP = 40
TICK_LENGTH = 10.0
LINE_WIDTH = 2.0
CORNER_SIZE = 20.0
COMB_ANGLE_STEP_DEG = 5
COMB_TOOTH_HALF_LENGTH = 4.0  # 8px total tooth length, centered on the ring
COMB_TOOTH_WIDTH = 2.0
VALIDATION_RING_WIDTH = 3.0

TICK_LABEL_FONT = AppKit.NSFont.boldSystemFontOfSize_(13.0)
RING_LABEL_FONT = AppKit.NSFont.boldSystemFontOfSize_(15.0)
CORNER_LABEL_FONT = AppKit.NSFont.systemFontOfSize_(12.0)

RING_COLOR_CYCLE: tuple[AppKit.NSColor, ...] = (
    AppKit.NSColor.whiteColor(),
    AppKit.NSColor.redColor(),
    AppKit.NSColor.yellowColor(),
    AppKit.NSColor.cyanColor(),
)
COMB_COLOR_A = AppKit.NSColor.whiteColor()
COMB_COLOR_B = AppKit.NSColor.redColor()
VALIDATION_RING_COLOR = AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(
    0.0, 1.0, 0.0, 1.0
)

CORNER_MARKERS: tuple[tuple[tuple[float, float], tuple[float, float], str, str], ...] = (
    # (square_origin, label_point, label_text, label_anchor)
    ((0.0, 0.0), (24.0, 10.0), "(0,0)", "left-center"),
    ((CANVAS_SIZE - CORNER_SIZE, 0.0), (CANVAS_SIZE - 24.0, 10.0), "(960,0)", "right-center"),
    ((0.0, CANVAS_SIZE - CORNER_SIZE), (24.0, CANVAS_SIZE - 10.0), "(0,960)", "left-center"),
    (
        (CANVAS_SIZE - CORNER_SIZE, CANVAS_SIZE - CORNER_SIZE),
        (CANVAS_SIZE - 24.0, CANVAS_SIZE - 10.0),
        "(960,960)",
        "right-center",
    ),
)


@dataclass(frozen=True, slots=True)
class ValidationRing:
    """Mode 2's single overlay ring — whatever three numbers the owner
    computed from the Mode 1 photo, drawn as-is. This script does not
    know or care whether the radius already has a safety margin applied
    (that's calibration.py's concern)."""

    center_x: float
    center_y: float
    radius: float


# ---------------------------------------------------------------------------
# Drawing primitives
# ---------------------------------------------------------------------------


def _point_on_circle(
    center: tuple[float, float], radius: float, angle_deg: float
) -> tuple[float, float]:
    """A point on a circle of `radius` around `center`, at `angle_deg`
    measured clockwise from 12 o'clock (0 deg = straight up), in the
    flipped (y-down) canvas coordinate system."""
    cx, cy = center
    theta = math.radians(angle_deg)
    return (cx + radius * math.sin(theta), cy - radius * math.cos(theta))


def _text_size(text: str, font: AppKit.NSFont) -> tuple[float, float]:
    attrs = {AppKit.NSFontAttributeName: font}
    s = AppKit.NSAttributedString.alloc().initWithString_attributes_(text, attrs)
    size = s.size()
    return size.width, size.height


def _draw_label(
    text: str,
    point: tuple[float, float],
    font: AppKit.NSFont,
    color: AppKit.NSColor,
    *,
    anchor: str = "center",
    halo: bool = True,
) -> None:
    """Draw `text` anchored at `point` per `anchor`, clamped to stay
    fully inside the canvas, optionally with a 1px black halo/outline
    for legibility where it crosses gridlines/rings/comb teeth."""
    width, height = _text_size(text, font)
    x, y = point

    if anchor == "top-center":
        origin = (x - width / 2, y)
    elif anchor == "bottom-center":
        origin = (x - width / 2, y - height)
    elif anchor == "left-center":
        origin = (x, y - height / 2)
    elif anchor == "right-center":
        origin = (x - width, y - height / 2)
    elif anchor == "center":
        origin = (x - width / 2, y - height / 2)
    else:
        raise ValueError(f"unknown label anchor {anchor!r}")

    # Clamp so extreme-offset labels (e.g. the +-480 ticks, which sit
    # right at the canvas edge) never get silently clipped by the view's
    # drawing bounds.
    ox = max(0.0, min(origin[0], CANVAS_SIZE - width))
    oy = max(0.0, min(origin[1], CANVAS_SIZE - height))

    if halo:
        attrs = {
            AppKit.NSFontAttributeName: font,
            AppKit.NSForegroundColorAttributeName: color,
            AppKit.NSStrokeColorAttributeName: AppKit.NSColor.blackColor(),
            # Negative stroke width is the standard Cocoa trick for
            # "stroke outline + fill in one draw call" (positive values
            # draw stroke-only, hollow text).
            AppKit.NSStrokeWidthAttributeName: -3.0,
        }
    else:
        attrs = {
            AppKit.NSFontAttributeName: font,
            AppKit.NSForegroundColorAttributeName: color,
        }

    s = AppKit.NSAttributedString.alloc().initWithString_attributes_(text, attrs)
    s.drawAtPoint_((ox, oy))


def _draw_crosshair() -> None:
    cx, cy = CENTER
    AppKit.NSColor.whiteColor().set()

    h_line = AppKit.NSBezierPath.bezierPath()
    h_line.moveToPoint_((0.0, cy))
    h_line.lineToPoint_((CANVAS_SIZE, cy))
    h_line.setLineWidth_(LINE_WIDTH)
    h_line.stroke()

    v_line = AppKit.NSBezierPath.bezierPath()
    v_line.moveToPoint_((cx, 0.0))
    v_line.lineToPoint_((cx, CANVAS_SIZE))
    v_line.setLineWidth_(LINE_WIDTH)
    v_line.stroke()


def _tick_offsets() -> list[int]:
    return [o for o in range(-480, 481, TICK_STEP) if o != 0]


def _draw_ticks() -> None:
    """Tick marks + signed-offset-from-center labels along both axes,
    every 40px ("+40...+480, -40...-480")."""
    cx, cy = CENTER
    AppKit.NSColor.whiteColor().set()

    for dx in _tick_offsets():
        x = cx + dx
        tick = AppKit.NSBezierPath.bezierPath()
        tick.moveToPoint_((x, cy - TICK_LENGTH / 2))
        tick.lineToPoint_((x, cy + TICK_LENGTH / 2))
        tick.setLineWidth_(LINE_WIDTH)
        tick.stroke()
        _draw_label(
            f"{dx:+d}",
            (x, cy + TICK_LENGTH / 2 + 3),
            TICK_LABEL_FONT,
            AppKit.NSColor.whiteColor(),
            anchor="top-center",
        )

    for dy in _tick_offsets():
        y = cy + dy
        tick = AppKit.NSBezierPath.bezierPath()
        tick.moveToPoint_((cx - TICK_LENGTH / 2, y))
        tick.lineToPoint_((cx + TICK_LENGTH / 2, y))
        tick.setLineWidth_(LINE_WIDTH)
        tick.stroke()
        _draw_label(
            f"{dy:+d}",
            (cx + TICK_LENGTH / 2 + 5, y),
            TICK_LABEL_FONT,
            AppKit.NSColor.whiteColor(),
            anchor="left-center",
        )


def _draw_solid_ring(radius: float, color: AppKit.NSColor) -> None:
    cx, cy = CENTER
    rect = AppKit.NSMakeRect(cx - radius, cy - radius, 2 * radius, 2 * radius)
    path = AppKit.NSBezierPath.bezierPathWithOvalInRect_(rect)
    path.setLineWidth_(LINE_WIDTH)
    color.set()
    path.stroke()


def _draw_comb_ring(radius: float) -> None:
    """Overscan/clipping comb: radial tick 'teeth' every 5 deg instead of
    a solid stroke, alternating white/red, at the two outermost rings."""
    for i, angle in enumerate(range(0, 360, COMB_ANGLE_STEP_DEG)):
        color = COMB_COLOR_A if i % 2 == 0 else COMB_COLOR_B
        inner = _point_on_circle(CENTER, radius - COMB_TOOTH_HALF_LENGTH, angle)
        outer = _point_on_circle(CENTER, radius + COMB_TOOTH_HALF_LENGTH, angle)
        tooth = AppKit.NSBezierPath.bezierPath()
        tooth.moveToPoint_(inner)
        tooth.lineToPoint_(outer)
        tooth.setLineWidth_(COMB_TOOTH_WIDTH)
        color.set()
        tooth.stroke()


def _draw_rings() -> None:
    for i, radius in enumerate(RING_RADII):
        color = RING_COLOR_CYCLE[i % len(RING_COLOR_CYCLE)]
        if radius in COMB_RADII:
            _draw_comb_ring(radius)
        else:
            _draw_solid_ring(radius, color)

        # Label at 12 o'clock, nudged left of the vertical crosshair so
        # it doesn't collide with that axis's tick labels (which sit at
        # the same y position, just to the right of the line).
        label_x, label_y = _point_on_circle(CENTER, radius, 0.0)
        _draw_label(
            f"R{radius}",
            (label_x - 6.0, label_y),
            RING_LABEL_FONT,
            color,
            anchor="right-center",
        )


def _draw_corner_markers() -> None:
    for (ox, oy), label_point, text, anchor in CORNER_MARKERS:
        AppKit.NSColor.whiteColor().set()
        AppKit.NSBezierPath.fillRect_(AppKit.NSMakeRect(ox, oy, CORNER_SIZE, CORNER_SIZE))
        _draw_label(text, label_point, CORNER_LABEL_FONT, AppKit.NSColor.whiteColor(), anchor=anchor)


def _draw_validation_ring(ring: ValidationRing) -> None:
    rect = AppKit.NSMakeRect(
        ring.center_x - ring.radius,
        ring.center_y - ring.radius,
        2 * ring.radius,
        2 * ring.radius,
    )
    path = AppKit.NSBezierPath.bezierPathWithOvalInRect_(rect)
    path.setLineWidth_(VALIDATION_RING_WIDTH)
    VALIDATION_RING_COLOR.set()
    path.stroke()


# ---------------------------------------------------------------------------
# NSView
# ---------------------------------------------------------------------------


class PatternView(AppKit.NSView):
    """Draws the full calibration pattern into a 960x960 canvas,
    plus an optional green validation ring (Mode 2). `validation_ring`
    is set as a plain attribute after construction — see build_window().
    """

    validation_ring: ValidationRing | None = None

    def isFlipped(self) -> bool:
        # Top-left origin, y increases downward — see the CANVAS_SIZE
        # comment above for why this matches the framebuffer/photo
        # convention the pattern's labels are defined against.
        return True

    def drawRect_(self, rect: AppKit.NSRect) -> None:
        AppKit.NSColor.blackColor().set()
        AppKit.NSBezierPath.fillRect_(self.bounds())

        _draw_crosshair()
        _draw_ticks()
        _draw_rings()
        _draw_corner_markers()

        if self.validation_ring is not None:
            _draw_validation_ring(self.validation_ring)


# ---------------------------------------------------------------------------
# CLI / bootstrap
# ---------------------------------------------------------------------------


def parse_args(argv: Sequence[str]) -> ValidationRing | None:
    parser = argparse.ArgumentParser(
        description=(
            "Render the view-lab calibration test pattern (Mode 1), or "
            "the same pattern plus a green validation ring at a "
            "previously-computed center/radius (Mode 2)."
        )
    )
    parser.add_argument(
        "--validate",
        nargs=3,
        type=float,
        metavar=("CENTER_X", "CENTER_Y", "RADIUS"),
        help="Mode 2: draw a green validation ring at these framebuffer coordinates.",
    )
    args = parser.parse_args(argv)

    if args.validate is None:
        return None

    center_x, center_y, radius = args.validate
    if radius <= 0:
        die(f"--validate radius must be positive, got {radius!r}.")
    return ValidationRing(center_x=center_x, center_y=center_y, radius=radius)


def build_window(screen: AppKit.NSScreen, validation_ring: ValidationRing | None) -> AppKit.NSWindow:
    """Borderless, non-activating, all-spaces window covering `screen`,
    with a PatternView as its content view. Mirrors smoke_test.py's
    build_window() window-scaffolding, swapping the flat color fill for
    the pattern view."""
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

    view = PatternView.alloc().initWithFrame_(
        AppKit.NSMakeRect(0.0, 0.0, screen.frame().size.width, screen.frame().size.height)
    )
    view.validation_ring = validation_ring
    window.setContentView_(view)
    window.setFrame_display_(screen.frame(), True)
    return window


def main() -> None:
    validation_ring = parse_args(sys.argv[1:])
    mode = "Mode 2 (base pattern + validation ring)" if validation_ring else "Mode 1 (base pattern)"
    print(f"view-lab calibration pattern starting — {mode}.", file=sys.stderr)
    if validation_ring is not None:
        print(
            f"  validation ring: center=({validation_ring.center_x}, "
            f"{validation_ring.center_y}) radius={validation_ring.radius}",
            file=sys.stderr,
        )

    target_screen = find_target_screen()
    print(f"Target screen found: {target_screen.frame()}", file=sys.stderr)

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

    window = build_window(target_screen, validation_ring)
    window.orderFrontRegardless()

    install_signal_handlers(app)

    print(
        "Pattern should now be visible on the target screen. Running "
        "until killed (Ctrl+C or SIGTERM).",
        file=sys.stderr,
    )
    app.run()
    print("view-lab calibration pattern exiting cleanly.", file=sys.stderr)


if __name__ == "__main__":
    main()
