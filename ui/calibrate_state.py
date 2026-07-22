"""Everything the calibration window decides, with no AppKit in it.

`calibrate_window.py` is the AppKit shell: two windows, some text fields,
a key monitor and a file write. This module is the part that can be wrong
in an interesting way — where the two rings actually land, what a nudge
does at the edge of the framebuffer, what Cmd-Z restores, whether there
is anything to save — so it is the part that is separable and tested.
Same split as `menubar_state.py` is to `menubar.py`, for the same reason:
none of this needs a window server to be verified, and all of it needs to
be right before anyone is asked to look at a 2.1-inch screen and judge a
ring against a bezel.

**Three floats.** The undoable state is "three floats", and that
is exactly `CircleValues`. `safety_margin_pct` is deliberately *not*
editable here: it is a policy value (how much of the visible circle
pictures are allowed to claim), not a measurement of the device, and
whole argument is that the user must be able to see its effect
without being able to mistake it for the thing they are calibrating.

**Clamping is per-field, not global**, and that is a decision worth
stating because the obvious alternative silently destroys data. Moving
the center toward an edge eventually makes the current radius
un-drawable. Clamping *radius* at that moment would quietly shrink the
one number the user spent the most effort on. So instead the center is
clamped to keep the current radius drawable, and the radius is clamped to
the current center — each field's own edit is the one that gives way.
`clamp_values` applies both, radius first, and exists for typed input
where there is no "which field moved" to key off.

Everything here is a pure function of plain floats. Nothing reads a file,
nothing writes one, nothing imports AppKit.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Mapping
from typing import Any

#: The three editable fields, in the order the window lays them out.
#: Used by the key monitor to map a focused text field back to a name, so
#: the tuple order is UI order and changing it changes the window.
FIELDS: tuple[str, ...] = ("center_x", "center_y", "radius_px")

#: A circle smaller than this is not a calibration, it is a typo. The
#: clamp floor exists so that a held-down arrow key cannot walk the
#: radius to zero and leave the user looking at a blank View with no
#: obvious way to understand what they did.
MIN_RADIUS_PX = 8.0

#: "Arrow keys nudge by 1, Shift+arrow by 10."
NUDGE_STEP = 1.0
NUDGE_STEP_LARGE = 10.0

#: Undo is a nudge stack, and a nudge is one keypress, so the stack grows
#: one entry per press. Bounded so that a session someone left open with
#: a key repeating does not grow without limit; 2000 entries is far more
#: than any real calibration pass and costs a few hundred kilobytes.
MAX_UNDO_DEPTH = 2000

#: Schema. Written back on save so that a file this window
#: produces is one both this app and any other reader will accept.
SCHEMA_VERSION = 1


@dataclasses.dataclass(frozen=True)
class CircleValues:
    """The three floats that make up the undoable state.

    Frozen: every mutation in this module produces a new instance, which
    is what makes the undo stack a plain list of snapshots rather than a
    list of reversible operations. Reversible operations would have to
    know about clamping, and a clamped operation is not reversible.
    """

    center_x: float
    center_y: float
    radius_px: float

    def replace(self, field: str, value: float) -> CircleValues:
        """Set one field by name. Raises `KeyError` for an unknown field
        — unlike everything that parses a file, this one takes its input
        from this module's own `FIELDS`, so an unknown name is a
        programming error and should be loud."""
        if field not in FIELDS:
            raise KeyError(field)
        return dataclasses.replace(self, **{field: float(value)})

    def get(self, field: str) -> float:
        if field not in FIELDS:
            raise KeyError(field)
        return float(getattr(self, field))

    def as_dict(self) -> dict[str, float]:
        return {
            "center_x": self.center_x,
            "center_y": self.center_y,
            "radius_px": self.radius_px,
        }


@dataclasses.dataclass(frozen=True)
class Bounds:
    """The framebuffer the circle has to fit inside.

    Taken from the loaded calibration rather than from `NSScreen`, on
    purpose: `display_target.get_view_screen()` already keys the choice
    of monitor off these numbers, so the window and the display agree
    about which rectangle they are talking about by construction.
    """

    width: float
    height: float

    @property
    def max_radius(self) -> float:
        """The largest radius that can be centered anywhere at all."""
        return max(MIN_RADIUS_PX, min(self.width, self.height) / 2.0)


def _finite(value: object, default: float = 0.0) -> float:
    """Coerce to a finite float, or `default`.

    NaN and infinity are excluded explicitly. Either would propagate
    through `ring_geometry` into an AppKit rect, and an NSRect containing
    NaN does not raise — it draws nothing, silently, which reads to the
    user as "the calibration window is broken" rather than "that value
    was rejected"."""
    if isinstance(value, bool):
        return default
    if not isinstance(value, (int, float)):
        return default
    number = float(value)
    if not math.isfinite(number):
        return default
    return number


def clamp_center(value: float, radius: float, extent: float) -> float:
    """Keep a center coordinate far enough from both edges that `radius`
    still fits.

    When the radius is larger than half the extent there is no satisfying
    answer; the window's midpoint is returned rather than an arbitrary
    edge, because a circle centered and clipped is recognisable as
    too-big while a circle jammed into a corner reads as a bug.
    """
    if radius * 2.0 >= extent:
        return extent / 2.0
    return min(max(value, radius), extent - radius)


def clamp_radius(radius: float, center_x: float, center_y: float, bounds: Bounds) -> float:
    """Keep the radius inside the nearest framebuffer edge.

    This is the same rule `calibration._validated_calibration` enforces
    at load time. Duplicated deliberately rather than imported: this
    module has no dependency on the display package, and a window that
    can only produce values the loader will accept is worth more than the
    dozen lines saved. The two are pinned together by a test.
    """
    nearest_edge = min(
        center_x, bounds.width - center_x, center_y, bounds.height - center_y
    )
    ceiling = max(MIN_RADIUS_PX, nearest_edge)
    return min(max(radius, MIN_RADIUS_PX), ceiling)


def clamp_values(values: CircleValues, bounds: Bounds) -> CircleValues:
    """Clamp all three at once — radius first, then the center to it.

    For typed input and for values arriving from a file, where there is
    no "which field the user moved" to preserve. Radius first because it
    is the number with a hard ceiling of its own (`Bounds.max_radius`);
    clamping the center first could leave a radius with no legal center.
    """
    radius = min(
        max(_finite(values.radius_px, MIN_RADIUS_PX), MIN_RADIUS_PX), bounds.max_radius
    )
    center_x = clamp_center(_finite(values.center_x), radius, bounds.width)
    center_y = clamp_center(_finite(values.center_y), radius, bounds.height)
    return CircleValues(center_x=center_x, center_y=center_y, radius_px=radius)


def set_field(
    values: CircleValues, field: str, raw: object, bounds: Bounds
) -> CircleValues:
    """Set one field and clamp *that* field only (see module docstring).

    A non-numeric `raw` leaves the value untouched, which is what makes
    the text fields safe to re-parse on every keystroke: a half-typed
    `-` or `4.` is not a value yet and must not be treated as one.
    """
    current = values.get(field)
    number = _finite(raw, current)
    if field == "radius_px":
        clamped = clamp_radius(number, values.center_x, values.center_y, bounds)
    elif field == "center_x":
        clamped = clamp_center(number, values.radius_px, bounds.width)
    else:
        clamped = clamp_center(number, values.radius_px, bounds.height)
    return values.replace(field, clamped)


def nudge(
    values: CircleValues, field: str, delta: float, bounds: Bounds
) -> CircleValues:
    """Arrow-key nudge: add `delta` to one field, then clamp it."""
    return set_field(values, field, values.get(field) + _finite(delta), bounds)


# -- two rings ---------------------------------------------------


@dataclasses.dataclass(frozen=True)
class RingGeometry:
    """Where both rings land, in framebuffer coordinates.

    Two radii from one set of values — this dataclass is that annulus expressed
    as a type. There is no way to ask this module for "the radius"; every
    caller gets both or neither, which is the structural version of the
    plan's "always shows both".
    """

    center_x: float
    center_y: float
    outer_radius: float
    inner_radius: float

    @property
    def gap_px(self) -> float:
        """The annulus: 33px at the shipped numbers. The
        window shows this so the user can see that the gap is intended
        and roughly how big it is *before* they go looking for it on the
        device and mistake it for an error."""
        return self.outer_radius - self.inner_radius

    def outer_rect(self) -> tuple[float, float, float, float]:
        """`(x, y, width, height)` for the bright ring — a plain tuple so
        that the AppKit side owns the only `NSMakeRect` call and this
        module stays importable without a window server."""
        return _rect(self.center_x, self.center_y, self.outer_radius)

    def inner_rect(self) -> tuple[float, float, float, float]:
        return _rect(self.center_x, self.center_y, self.inner_radius)


def _rect(center_x: float, center_y: float, radius: float) -> tuple[float, float, float, float]:
    return (center_x - radius, center_y - radius, radius * 2.0, radius * 2.0)


def ring_geometry(values: CircleValues, safety_margin_pct: float) -> RingGeometry:
    """The bright ring is `radius_px`; the dim one is
    `radius_px * safety_margin_pct` — the radius pictures are actually
    drawn at, matching `Calibration.effective_radius_px`.

    An out-of-range margin falls back to 1.0, i.e. the two rings
    coincide. That is the honest degradation: a margin this function
    cannot trust must not be allowed to draw an inner ring the user would
    take as authoritative, and two rings on top of each other is visibly
    odd rather than quietly wrong.
    """
    margin = _finite(safety_margin_pct, 1.0)
    if not (0.0 < margin <= 1.0):
        margin = 1.0
    return RingGeometry(
        center_x=values.center_x,
        center_y=values.center_y,
        outer_radius=values.radius_px,
        inner_radius=values.radius_px * margin,
    )


# -- session: undo, dirty state, revert --------------------------


class CalibrationSession:
    """The whole editable state of one calibration window.

    Holds three `CircleValues`: what is on disk (`saved`), what the app
    ships (`defaults`), and what the user is currently looking at
    (`values`). Every transition between them goes through `_apply`, so
    "Revert to saved" and "Reset to defaults" are themselves undoable —
    which matters more than it sounds, because those two buttons sit next
    to each other, and people are explicitly expected to hit the wrong
    one.

    `dirty` is `values != saved`, not a flag that gets set. A flag would
    have to be cleared by undo, and "undo back to exactly the saved
    numbers" must leave nothing to save.
    """

    def __init__(
        self,
        saved: CircleValues,
        defaults: CircleValues,
        bounds: Bounds,
        safety_margin_pct: float,
    ) -> None:
        self._bounds = bounds
        self._safety_margin_pct = safety_margin_pct
        self._saved = clamp_values(saved, bounds)
        self._defaults = clamp_values(defaults, bounds)
        self._values = self._saved
        self._undo: list[CircleValues] = []
        self._redo: list[CircleValues] = []

    # -- reading -------------------------------------------------------

    @property
    def values(self) -> CircleValues:
        return self._values

    @property
    def saved(self) -> CircleValues:
        return self._saved

    @property
    def defaults(self) -> CircleValues:
        return self._defaults

    @property
    def bounds(self) -> Bounds:
        return self._bounds

    @property
    def safety_margin_pct(self) -> float:
        return self._safety_margin_pct

    @property
    def dirty(self) -> bool:
        return self._values != self._saved

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def geometry(self) -> RingGeometry:
        return ring_geometry(self._values, self._safety_margin_pct)

    # -- editing -------------------------------------------------------

    def _apply(self, new_values: CircleValues) -> bool:
        """Push the current values onto the undo stack and adopt
        `new_values`. Returns whether anything actually changed.

        A no-op edit — an arrow key at a clamp boundary, a text field
        re-committing the value it already had — must not push an undo
        entry. Otherwise holding Left at the edge of the framebuffer
        silently builds a stack of hundreds of identical states and Cmd-Z
        appears to do nothing several hundred times.
        """
        if new_values == self._values:
            return False
        self._undo.append(self._values)
        if len(self._undo) > MAX_UNDO_DEPTH:
            del self._undo[0]
        self._redo.clear()
        self._values = new_values
        return True

    def nudge(self, field: str, delta: float) -> bool:
        return self._apply(nudge(self._values, field, delta, self._bounds))

    def set_field(self, field: str, raw: object) -> bool:
        return self._apply(set_field(self._values, field, raw, self._bounds))

    def set_values(self, values: CircleValues) -> bool:
        return self._apply(clamp_values(values, self._bounds))

    def revert_to_saved(self) -> bool:
        """"Revert to saved" — the one people actually want, and
        the reason it is listed above "Reset to defaults"."""
        return self._apply(self._saved)

    def reset_to_defaults(self) -> bool:
        return self._apply(self._defaults)

    def undo(self) -> bool:
        if not self._undo:
            return False
        self._redo.append(self._values)
        self._values = self._undo.pop()
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        self._undo.append(self._values)
        self._values = self._redo.pop()
        return True

    def mark_saved(self) -> None:
        """Record that the current values are now on disk.

        The undo stack is deliberately **not** cleared. The nudges that
        led here are still the user's work, and a Save followed by Cmd-Z
        should walk back through them exactly as before — leaving the
        session dirty again, which is true, and offering Save again,
        which is what fixes it.
        """
        self._saved = self._values

    # -- transient preview --------------------------------------

    def preview_payload(self) -> dict[str, float]:
        """What goes in the control file's `preview_calibration`.

        Carries `safety_margin_pct` as well as the three floats even
        though the user cannot edit it, so that the display's inner
        circle is computed from the same margin this window drew its dim
        ring with. Without it the two would agree only as long as neither
        side's fallback ever fired.
        """
        payload = self._values.as_dict()
        payload["safety_margin_pct"] = self._safety_margin_pct
        return payload


# -- the file this window writes ---------------------------------------


def circle_from_document(data: object) -> CircleValues | None:
    """Pull the three floats out of a decoded `calibration.json`.

    Returns `None` rather than raising for anything unusable — this reads
    a file that may have been hand-edited, written by another tool, or
    left half-written by a crash, and the window's job in that case is to
    fall back, not to fail to open.
    """
    if not isinstance(data, Mapping):
        return None
    circle = data.get("circle")
    if not isinstance(circle, Mapping):
        return None
    try:
        values = CircleValues(
            center_x=float(circle["center_x"]),
            center_y=float(circle["center_y"]),
            radius_px=float(circle["radius_px"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
    for number in (values.center_x, values.center_y, values.radius_px):
        if not math.isfinite(number):
            return None
    return values


def bounds_from_document(data: object, default: Bounds) -> Bounds:
    """Framebuffer dimensions from a decoded document, or `default`."""
    if not isinstance(data, Mapping):
        return default
    framebuffer = data.get("framebuffer")
    if not isinstance(framebuffer, Mapping):
        return default
    try:
        width = float(framebuffer["width"])
        height = float(framebuffer["height"])
    except (KeyError, TypeError, ValueError):
        return default
    if not (math.isfinite(width) and math.isfinite(height)):
        return default
    if width <= 0 or height <= 0:
        return default
    return Bounds(width=width, height=height)


def calibration_document(
    values: CircleValues,
    *,
    safety_margin_pct: float,
    bounds: Bounds,
    previous: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the document to write to `~/.viewlab/calibration.json`.

    schema, and **v1 is additive-only**, so unknown keys carried by
    `previous` are preserved rather than dropped: this file is shared with
    an independently-versioned second app, and a window that silently
    deleted a key it did not recognise would be exactly the
    cross-version breakage `schema_version` exists to prevent.

    Two keys are exceptions, both deliberate:

    - `target_screen` is preserved as-is. This window does not choose a
      monitor (that is Step 4's picker), and the
      choice must not diverge between the two apps.
    - `calibration_source` is **dropped**. The live file's copy names a
      photograph and a measurement date describing the *old* numbers; the
      moment this window writes new ones that provenance is a false
      statement about the values beside it. The resolution order wants provenance out of
      the shipped seed anyway; removing it here means the one file that
      still carries it stops carrying it as soon as anyone recalibrates.
    """
    document: dict[str, Any] = {}
    if isinstance(previous, Mapping):
        for key, value in previous.items():
            if key == "calibration_source":
                continue
            document[key] = value
    document["schema_version"] = SCHEMA_VERSION
    document["framebuffer"] = {"width": bounds.width, "height": bounds.height}
    document["circle"] = {
        "center_x": values.center_x,
        "center_y": values.center_y,
        "radius_px": values.radius_px,
    }
    document["safety_margin_pct"] = safety_margin_pct
    return document


def format_value(number: float) -> str:
    """Render a float for a text field.

    Whole numbers lose the trailing `.0`: calibration values are read
    aloud, typed, and compared against a plan document that writes them
    as `472`, and a field reading `472.0` invites someone to wonder what
    the extra precision means.
    """
    value = _finite(number)
    if value == int(value):
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


__all__ = [
    "Bounds",
    "CalibrationSession",
    "CircleValues",
    "FIELDS",
    "MAX_UNDO_DEPTH",
    "MIN_RADIUS_PX",
    "NUDGE_STEP",
    "NUDGE_STEP_LARGE",
    "RingGeometry",
    "SCHEMA_VERSION",
    "bounds_from_document",
    "calibration_document",
    "circle_from_document",
    "clamp_center",
    "clamp_radius",
    "clamp_values",
    "format_value",
    "nudge",
    "ring_geometry",
    "set_field",
]
