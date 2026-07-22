"""Generate `ImageView.icns` — the app icon.

The motif is the device itself: a round, lit display set into a dark body.
That is what a View *is*, and it distinguishes the app from every other
icon in the Dock without needing a literal picture-frame metaphor.

Two constraints shaped the drawing:

- **It must read at 16pt.** At that size the only things that survive are
  the silhouette and one strong value contrast, so the design is
  deliberately "dark rounded square, bright circle" and nothing else.
  Detail (the bezel highlight, the inner shadow, the gradient inside the
  screen) is there for 512/1024 and is *allowed* to disappear small — it
  must not be load-bearing.
- **macOS Big Sur+ geometry.** The 1024 canvas is not filled edge to
  edge: the art sits in an 824x824 rounded rect with a ~185pt corner
  radius, centred, which is what makes an icon sit correctly next to
  system icons rather than looking oversized. `iconutil` needs the full
  size ladder — a partial iconset builds without complaint and
  renders blurry at exactly the sizes users actually see.

Run from the repo root:

    display/.venv/bin/python3 packaging/make_app_icon.py

Writes `packaging/ImageView.iconset/` and `packaging/ImageView.icns`.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import AppKit
import Quartz

CANVAS = 1024.0
# Big Sur proportions: art occupies 824/1024 of the canvas, radius 185.4.
ART = 824.0
RADIUS = 185.4
# The screen as a fraction of the art square. 0.62 keeps a substantial
# body visible so the silhouette still reads as "device", not "circle".
SCREEN_FRACTION = 0.62

ICONSET_SIZES = [16, 32, 64, 128, 256, 512, 1024]


def _srgb(r: float, g: float, b: float, a: float = 1.0) -> AppKit.NSColor:
    return AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, a)


def _draw(size: float) -> AppKit.NSImage:
    """Render the icon at `size` x `size`. Everything is expressed as a
    fraction of the canvas so a single code path serves 16pt and 1024pt."""
    image = AppKit.NSImage.alloc().initWithSize_(AppKit.NSMakeSize(size, size))
    image.lockFocus()

    ctx = AppKit.NSGraphicsContext.currentContext().CGContext()
    s = size / CANVAS

    inset = (CANVAS - ART) / 2.0 * s
    art_rect = AppKit.NSMakeRect(inset, inset, ART * s, ART * s)
    body = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        art_rect, RADIUS * s, RADIUS * s
    )

    # -- body: a vertical gradient, lighter at the top so the shape reads
    # as a lit object rather than a flat swatch.
    AppKit.NSGraphicsContext.saveGraphicsState()
    body.addClip()
    AppKit.NSGradient.alloc().initWithStartingColor_endingColor_(
        _srgb(0.16, 0.17, 0.20), _srgb(0.06, 0.06, 0.08)
    ).drawInRect_angle_(art_rect, 90.0)

    # A faint top rim, the standard cue that a surface is catching light.
    rim = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        AppKit.NSMakeRect(
            art_rect.origin.x,
            art_rect.origin.y + 1.5 * s,
            art_rect.size.width,
            art_rect.size.height,
        ),
        RADIUS * s,
        RADIUS * s,
    )
    _srgb(1, 1, 1, 0.10).set()
    rim.setLineWidth_(3.0 * s)
    rim.stroke()
    AppKit.NSGraphicsContext.restoreGraphicsState()

    # -- the screen
    screen_d = ART * SCREEN_FRACTION * s
    cx = size / 2.0
    cy = size / 2.0
    screen_rect = AppKit.NSMakeRect(
        cx - screen_d / 2.0, cy - screen_d / 2.0, screen_d, screen_d
    )
    screen = AppKit.NSBezierPath.bezierPathWithOvalInRect_(screen_rect)

    # Glow beneath the screen — sells "this thing emits light". Skipped
    # below 64pt, where it would just muddy the edge.
    if size >= 64:
        AppKit.NSGraphicsContext.saveGraphicsState()
        Quartz.CGContextSetShadowWithColor(
            ctx,
            Quartz.CGSizeMake(0, 0),
            26.0 * s,
            _srgb(1.0, 0.62, 0.30, 0.55).CGColor(),
        )
        _srgb(0.10, 0.10, 0.12).set()
        screen.fill()
        AppKit.NSGraphicsContext.restoreGraphicsState()

    # Screen content: a warm diagonal gradient standing in for the
    # generated artwork the app actually rotates. Chosen for value
    # contrast against the body first, hue second — that contrast is the
    # only thing that survives at 16pt.
    AppKit.NSGraphicsContext.saveGraphicsState()
    screen.addClip()
    AppKit.NSGradient.alloc().initWithColors_(
        [
            _srgb(1.00, 0.82, 0.42),
            _srgb(0.98, 0.48, 0.30),
            _srgb(0.55, 0.24, 0.48),
            _srgb(0.16, 0.26, 0.55),
        ]
    ).drawInRect_angle_(screen_rect, 55.0)

    # A soft highlight in the upper-left third, so the disc reads as
    # convex glass instead of a flat sticker.
    if size >= 64:
        AppKit.NSGradient.alloc().initWithStartingColor_endingColor_(
            _srgb(1, 1, 1, 0.30), _srgb(1, 1, 1, 0.0)
        ).drawInRect_angle_(
            AppKit.NSMakeRect(
                screen_rect.origin.x,
                screen_rect.origin.y + screen_d * 0.45,
                screen_d,
                screen_d * 0.55,
            ),
            270.0,
        )
    AppKit.NSGraphicsContext.restoreGraphicsState()

    # Bezel: a thin bright ring, the visual seam between glass and body.
    if size >= 32:
        _srgb(1, 1, 1, 0.22).set()
        screen.setLineWidth_(max(1.0, 5.0 * s))
        screen.stroke()

    image.unlockFocus()
    return image


def _png(image: AppKit.NSImage, path: Path) -> None:
    tiff = image.TIFFRepresentation()
    rep = AppKit.NSBitmapImageRep.imageRepWithData_(tiff)
    data = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {})
    path.write_bytes(bytes(data))


def main() -> int:
    out = Path(__file__).resolve().parent
    iconset = out / "ImageView.iconset"
    if iconset.exists():
        shutil.rmtree(iconset)
    iconset.mkdir()

    # The full ladder of icon sizes. Each logical size needs its 1x and
    # the 2x that is literally the next size up — iconutil rejects an
    # iconset with gaps, and macOS renders the missing ones blurry.
    for size in ICONSET_SIZES:
        image = _draw(float(size))
        if size <= 512:
            _png(image, iconset / f"icon_{size}x{size}.png")
        if size >= 32:
            half = size // 2
            _png(image, iconset / f"icon_{half}x{half}@2x.png")

    icns = out / "ImageView.icns"
    result = subprocess.run(
        ["iconutil", "-c", "icns", str(iconset), "-o", str(icns)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"iconutil failed: {result.stderr}", file=sys.stderr)
        return 1

    print(f"wrote {icns} ({icns.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
