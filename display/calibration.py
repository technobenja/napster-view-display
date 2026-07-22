"""Loads and validates calibration.json.

The canonical file is **`~/.viewlab/calibration.json`**:
it lives in the user's home directory and may be read by other tools, it
is what the calibration UI writes, and nothing writable may live inside
the app bundle. `display/config/
calibration.json` survives only as read-only seed data, copied into
`~/.viewlab/` on first run — see `load_calibration_resolved()` for full resolution order.

Phase 2 stated "there is no hot-reload for v1 (simpler than watching
file mtimes for an event that happens maybe twice ever)". Phase 3
reverses that, and for a reason that did not exist in phase 2: the
calibration UI nudges the circle live, so an edit has to be visible on
the device within ~1s without a restart. The watching itself lives in
config_store.WatchedConfig; `validate_calibration()` below is the
validator both it and startup share, so the running app can never accept
a file at reload time that it would have rejected at startup.

Never raises. A missing file, unparseable JSON, an unknown
`schema_version` major, or an out-of-range value all fall back — at
startup to the conservative default (centered, 90% radius), and at
reload time to the last-good in-memory value — and log loudly.
Drawing something safely round beats crashing or guessing a too-large
radius.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from display import paths
from display.config_store import ConfigSource, Resolved, WatchedConfig, resolve_config, schema_is_supported

_LABEL = "calibration.py"

# Read-only seed shipped in the bundle — NOT where the running app's
# calibration lives (that is paths.calibration_path()). Kept under its
# original name because the existing tests and window.py's standalone
# entry point both reference it.
DEFAULT_CALIBRATION_PATH = paths.bundled_calibration_path()

# The View's known framebuffer resolution (probe/DECISION.md §1), used only
# when calibration.json is missing or fails validation.
FALLBACK_FRAMEBUFFER_SIZE = 960.0
FALLBACK_SAFETY_MARGIN_PCT = 0.93


@dataclasses.dataclass(frozen=True)
class Calibration:
    framebuffer_width: float
    framebuffer_height: float
    center_x: float
    center_y: float
    radius_px: float
    safety_margin_pct: float
    #: `target_screen` block, which has been an unused slot in the
    #: schema since Step -1. Defaulted so that every existing
    #: construction site — and there are many, in tests especially —
    #: keeps working unchanged, and so that a calibration file without
    #: the block behaves exactly as it did before Step 4.
    #:
    #: The strategy is deliberately not an enum: this value comes out of
    #: a hand-editable file shared with another application, and
    #: anything other than the one explicit strategy falls through to the
    #: resolution heuristic rather than being rejected.
    target_strategy: str = "match_by_resolution_excluding_main"
    #: The EDID-derived stable id (**not** `CGDirectDisplayID`,
    #: which WindowServer reassigns on replug). Empty means "no explicit
    #: choice", which is the shipped seed's state.
    target_display_id: str = ""

    @property
    def effective_radius_px(self) -> float:
        """Derived at load time, never stored — the one number
        downstream drawing/masking code should consume."""
        return self.radius_px * self.safety_margin_pct


def _fallback_calibration() -> Calibration:
    size = FALLBACK_FRAMEBUFFER_SIZE
    return Calibration(
        framebuffer_width=size,
        framebuffer_height=size,
        center_x=size / 2,
        center_y=size / 2,
        radius_px=size / 2 * 0.9,
        safety_margin_pct=FALLBACK_SAFETY_MARGIN_PCT,
    )


def _validated_calibration(data: Mapping[str, Any]) -> Calibration | None:
    """Parse and range-check `data`. Returns None on any structural or
    range violation — never raises."""
    try:
        framebuffer = data["framebuffer"]
        width = float(framebuffer["width"])
        height = float(framebuffer["height"])
        circle = data["circle"]
        center_x = float(circle["center_x"])
        center_y = float(circle["center_y"])
        radius_px = float(circle["radius_px"])
        safety_margin_pct = float(
            data.get("safety_margin_pct", FALLBACK_SAFETY_MARGIN_PCT)
        )
    except (KeyError, TypeError, ValueError):
        return None

    if width <= 0 or height <= 0:
        return None
    if not (0 <= center_x <= width) or not (0 <= center_y <= height):
        return None
    if radius_px <= 0:
        return None
    if not (0 < safety_margin_pct <= 1):
        return None

    # radius_px must not exceed the distance from center to the nearest
    # framebuffer edge — otherwise the drawn circle would run off-canvas.
    nearest_edge = min(center_x, width - center_x, center_y, height - center_y)
    if radius_px > nearest_edge:
        return None

    # target_screen block. Read permissively and *after* every
    # range check above, because an unusable target must not be able to
    # invalidate an otherwise-good circle: the worst a bad value here can
    # do is fall back to the resolution heuristic, which is what every
    # calibration file did before this block was read at all.
    target = data.get("target_screen")
    strategy = "match_by_resolution_excluding_main"
    display_id = ""
    if isinstance(target, Mapping):
        raw_strategy = target.get("resolve_strategy")
        if isinstance(raw_strategy, str) and raw_strategy.strip():
            strategy = raw_strategy.strip()
        raw_id = target.get("display_id")
        if isinstance(raw_id, str) and raw_id.strip():
            display_id = raw_id.strip()

    return Calibration(
        framebuffer_width=width,
        framebuffer_height=height,
        center_x=center_x,
        center_y=center_y,
        radius_px=radius_px,
        safety_margin_pct=safety_margin_pct,
        target_strategy=strategy,
        target_display_id=display_id,
    )


def load_calibration(path: Path | str = DEFAULT_CALIBRATION_PATH) -> Calibration:
    """Load and validate calibration.json, falling back to a safe
    conservative default on any failure. Never raises."""
    path = Path(path)

    try:
        raw = path.read_text()
    except OSError as exc:
        print(
            f"calibration.py: cannot read {path} ({exc}); "
            f"using fallback default.",
            file=sys.stderr,
        )
        return _fallback_calibration()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(
            f"calibration.py: {path} is not valid JSON ({exc}); "
            f"using fallback default.",
            file=sys.stderr,
        )
        return _fallback_calibration()

    # Schema_version must actually be read. An unknown major means
    # the file was written by a differently-versioned reader or writer
    # (tools sharing this file version independently), so a parse that
    # "half-works" is
    # the worst available outcome — refuse it and log loudly instead.
    if not schema_is_supported(data, path, _LABEL):
        return _fallback_calibration()

    calibration = _validated_calibration(data)
    if calibration is None:
        print(
            f"calibration.py: {path} failed range validation; "
            f"using fallback default.",
            file=sys.stderr,
        )
        return _fallback_calibration()

    return calibration


def validate_calibration(data: Mapping[str, Any]) -> Calibration | None:
    """Public name for the validator, shared by the resolution order
    and the hot-reload watcher. Returns None on any structural or
    range violation; never raises."""
    return _validated_calibration(data)


def apply_preview(
    base: Calibration, preview: Mapping[str, Any] | None
) -> Calibration:
    """Overlay transient `preview_calibration` onto a loaded
    calibration, for live nudging from the calibration window.

    The result is **validated by the same rules as a file** — an overlay
    that produces an out-of-range circle returns `base` unchanged rather
    than a half-applied blend. That matters because the preview arrives
    from another process mid-keystroke: `control.PREVIEW_CALIBRATION_KEYS`
    already allow-lists the fields and rejects NaN, but nothing upstream
    checks that the *combination* still fits the framebuffer, and a
    radius that runs off-canvas is only visible as a picture that has
    quietly stopped being round.

    `framebuffer_width`/`height` are not overridable, here or in
    `control`: `display_target.get_view_screen()` keys off them, so a
    preview that could change them could move the overlay onto another
    monitor mid-nudge.

    Never raises. A `None` preview returns `base`, which is what makes
    "clear the preview" and "there was never a preview" the same code
    path on the display side.
    """
    if not preview:
        return base
    merged = dataclasses.replace(
        base,
        center_x=_preview_float(preview, "center_x", base.center_x),
        center_y=_preview_float(preview, "center_y", base.center_y),
        radius_px=_preview_float(preview, "radius_px", base.radius_px),
        safety_margin_pct=_preview_float(
            preview, "safety_margin_pct", base.safety_margin_pct
        ),
    )
    validated = _validated_calibration(
        {
            "framebuffer": {
                "width": merged.framebuffer_width,
                "height": merged.framebuffer_height,
            },
            "circle": {
                "center_x": merged.center_x,
                "center_y": merged.center_y,
                "radius_px": merged.radius_px,
            },
            "safety_margin_pct": merged.safety_margin_pct,
        }
    )
    if validated is None:
        print(
            f"calibration.py: preview {dict(preview)} is out of range for "
            f"this framebuffer; keeping the current circle.",
            file=sys.stderr,
        )
        return base
    return validated


def _preview_float(preview: Mapping[str, Any], key: str, default: float) -> float:
    """One preview field, or `default`. `bool` is excluded for the same
    reason it is everywhere else in this project — it is an `int`
    subclass, and `{"radius_px": true}` reading as 1.0 would blank the
    View rather than obviously failing."""
    value = preview.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        return default
    return number


def load_calibration_resolved() -> Resolved[Calibration]:
    """The resolution order, with provenance:

    1. `~/.viewlab/calibration.json` if present and valid
    2. else the bundled seed, **copied to `~/.viewlab/` on first read**
    3. else the conservative built-in fallback

    The returned `ConfigSource` is what app.py records in status.json so
    that a silent fallback is observable."""
    return resolve_config(
        user_path=paths.calibration_path(),
        bundled_path=paths.bundled_calibration_path(),
        validate=validate_calibration,
        fallback=_fallback_calibration,
        label=_LABEL,
    )


def calibration_watcher() -> WatchedConfig[Calibration]:
    """A watcher over `~/.viewlab/calibration.json` — the user path
    only, never the repo's `display/config/` (the LaunchAgent runs
    from the live tree, so watching the repo copy would act on a
    half-saved edit during development)."""
    return WatchedConfig(paths.calibration_path(), validate_calibration, _LABEL)


__all__ = [
    "Calibration",
    "ConfigSource",
    "DEFAULT_CALIBRATION_PATH",
    "apply_preview",
    "calibration_watcher",
    "load_calibration",
    "load_calibration_resolved",
    "validate_calibration",
]
