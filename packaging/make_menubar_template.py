"""Generate `packaging/menubar-template.pdf` — the status item icon.

Run: `python3 packaging/make_menubar_template.py`

**Why a PDF and not an SVG or a PNG.** `NSImage` has no native SVG
support, and a PNG at 18x18 is one fixed raster that the menu bar
will resample on any scale factor it was not drawn for. A PDF is vector,
so the same file is crisp at 1x, 2x, and whatever comes next.

**Why one path with an even-odd fill, and not two drawing operations.**
The icon is "one shape with one internal cut, not two nested shapes",
and says why: at 18pt there is about 16pt of usable art, so a frame drawn
*around* a filled circle collapses to a ~1.5pt stroke that aliases into a
grey smudge. Building the circle and the horizon bar into a single path
and filling it even-odd makes the bar a genuine hole in the disc — the
cut is the absence of ink, at any resolution, with no second colour, no
blend mode, and nothing that a template image's alpha-only rendering can
misinterpret. `kCGBlendModeClear` would have been the obvious
alternative and is not reliable in a PDF context, where there is no
backdrop to clear to.

**Template images are alpha-only.** macOS discards the colour entirely
and re-renders the shape black, white, or highlighted to match the menu
bar. So this draws in flat black and cares only about coverage. Note that
loading by *path* does not apply the `...Template` filename convention —
only `imageNamed:` does — so `menubar.py` calls `setTemplate_(True)`
explicitly. That one line is the light/dark inversion.
"""

from __future__ import annotations

import sys
from pathlib import Path

import Quartz

#: The menu bar's usable icon box ("at 18x18pt there is ~16pt of
#: usable art").
CANVAS_PT = 18.0

#: Diameter 16 inside an 18pt box: 1pt of breathing room top and bottom,
#: which is what keeps the disc from touching the menu bar's own padding
#: and reading as a filled square.
RADIUS_PT = 8.0

#: Bold enough to survive being re-rendered at menu bar weight, thin
#: enough that the disc still reads as a disc rather than as two stacked
#: shapes. Below about 1.2pt it closes up at 1x.
CUT_HEIGHT_PT = 1.5

#: "a horizon-line cut through the lower third". The circle spans
#: y=1..17; a third of the way up from its bottom is y≈6.33.
CUT_CENTER_FRACTION = 1.0 / 3.0

OUTPUT = Path(__file__).resolve().parent / "menubar-template.pdf"


def disc_rect() -> Quartz.CGRect:
    center = CANVAS_PT / 2.0
    return Quartz.CGRectMake(
        center - RADIUS_PT, center - RADIUS_PT, RADIUS_PT * 2.0, RADIUS_PT * 2.0
    )


def cut_rect() -> Quartz.CGRect:
    """The horizon bar, deliberately wider than the canvas.

    Overhang is correct *because the fill is clipped to the disc*: the
    bar only has to be guaranteed to reach both edges of the circle, and
    sizing it to the chord width at some particular y would leave a
    hairline of ink at one end of the cut (the chord is wider at the top
    of a 1.5pt bar than at its bottom, so no single width fits both).
    Overhang plus clip is exact; overhang alone is not, and the first
    render of this file showed why — two black stubs poking out of the
    disc where the bar's corners fell outside it.
    """
    center = CANVAS_PT / 2.0
    cut_center_y = (center - RADIUS_PT) + (2.0 * RADIUS_PT * CUT_CENTER_FRACTION)
    return Quartz.CGRectMake(
        -1.0, cut_center_y - CUT_HEIGHT_PT / 2.0, CANVAS_PT + 2.0, CUT_HEIGHT_PT
    )


def build_path() -> Quartz.CGMutablePathRef:
    """The disc plus the horizon bar, as one path, filled even-odd so the
    bar is a hole in the disc rather than a second shape drawn on top."""
    path = Quartz.CGPathCreateMutable()
    Quartz.CGPathAddEllipseInRect(path, None, disc_rect())
    Quartz.CGPathAddRect(path, None, cut_rect())
    return path


def write_pdf(output: Path = OUTPUT) -> Path:
    media_box = Quartz.CGRectMake(0.0, 0.0, CANVAS_PT, CANVAS_PT)
    url = Quartz.CFURLCreateWithFileSystemPath(
        None, str(output), Quartz.kCFURLPOSIXPathStyle, False
    )
    context = Quartz.CGPDFContextCreateWithURL(url, media_box, None)
    if context is None:
        raise RuntimeError(f"could not open {output} for writing")

    Quartz.CGContextBeginPage(context, media_box)
    Quartz.CGContextSetRGBFillColor(context, 0.0, 0.0, 0.0, 1.0)
    # Clip to the disc first; see cut_rect() for why the bar overhangs
    # and why the clip is what makes that correct.
    Quartz.CGContextAddEllipseInRect(context, disc_rect())
    Quartz.CGContextClip(context)
    Quartz.CGContextAddPath(context, build_path())
    # Even-odd, not winding: winding would fill the union of the two
    # sub-paths and produce a plain disc with no cut at all -- which
    # would look almost right in a thumbnail and wrong in the menu bar.
    Quartz.CGContextEOFillPath(context)
    Quartz.CGContextEndPage(context)
    Quartz.CGPDFContextClose(context)
    return output


if __name__ == "__main__":
    written = write_pdf()
    print(f"wrote {written} ({written.stat().st_size} bytes)", file=sys.stderr)
