"""The desired-state control file.

`~/.viewlab/state/command.json`. **Written by the UI, read by the
display, never the other way around** (one-writer-per-file rule).
The display's own outbound channel is `status.json`; nothing in this
module writes to `command_path()` on the display's behalf, and
`ControlChannel` — the display-side reader — has no write method at all.

    { "blanked": null, "paused": false, "paused_on_id": null,
      "advance": 42, "preview_calibration": null,
      "written_at": 1752800000.0 }

**Why this shape rather than `{seq, action}`**. A command *queue*
gets both halves of the problem wrong at once: two Next clicks inside one
250ms poll window collapse into a single observed value, and replay
across a restart is undefined — an in-memory `last_seen` re-fires an
hour-old command on every launch, while a persisted one wedges the whole
channel the first time the UI's counter restarts at zero. So the file is
split by *shape*, not by action:

**Desired state** — `blanked`, `paused`, `paused_on_id`,
`preview_calibration`. Level-triggered: the display reads what the world
should look like and makes it so. Idempotent by construction (applying it
twice is applying it once), and replay-safe for the same reason. `blanked`
surviving a restart is then a property of the file rather than a feature
anyone has to implement.

**An incremental counter** — `advance`. The display persists
`last_seen_advance` and applies `advance - last_seen_advance` steps. Two
fast clicks move the counter by two and the display takes two steps, even
though it only ever observed the endpoint. The delta is clamped
(`MAX_ADVANCE_STEPS`) so no single tick can walk the rotation an
unbounded distance.

Three rules that are each load-bearing:

- **Compare `!=`, never `>`.** A UI that restarts and begins counting
  from zero again must not wedge the channel forever, which is exactly
  what `if new > last_seen` would do.
- **On startup, adopt whatever is in the file as already-seen.** Desired
  state is applied (that is the point); the counter delta is not. Without
  this, every launch replays an hour-old Next.
- **Read defensively.** Allow-list keys, type-check each one
  individually, ignore unknowns, never raise. This file is on disk in the
  user's home directory and is exactly the sort of thing that gets
  hand-edited, half-written, or left over from a future version.

`blanked` is deliberately tri-state (`bool | None`), matching `blank_manual`: `None` means "no manual override — follow the schedule".
Scheduled blanking is not built (it lands with settings surface,
and the two ship together or neither does), so
`effective_blanked()` treats `None` as "not blanked" today and is the one
place a schedule gets wired in later.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from pathlib import Path
from typing import Any

from display import paths
from display.atomic_io import atomic_write_json

logger = logging.getLogger(__name__)

#: Ceiling on how many rotation steps one observed counter delta may
#: apply. A human cannot meaningfully queue more than a handful of clicks
#: inside a 250ms window, so a larger jump is far more likely to be a
#: counter glitch (a reset, a hand-edit, a stale file from another
#: machine) than intent — and `!=` rule means a glitch *is*
#: reachable by construction. Clamping is what bounds its blast radius to
#: "a few pictures went by" instead of "the rotation teleported".
MAX_ADVANCE_STEPS = 5

#: Every key this reader will look at. Anything else in the file is
#: ignored rather than rejected: a newer UI writing a field this build
#: does not know about must degrade to "the fields we understand still
#: work", not to "the whole channel is dead".
ALLOWED_KEYS = frozenset(
    {
        "blanked",
        "paused",
        "paused_on_id",
        "advance",
        "refresh",
        "preview_calibration",
        "written_at",
    }
)

#: The calibration fields a transient preview (live nudging) is
#: allowed to override. Framebuffer dimensions are deliberately absent:
#: `display_target.get_view_screen()` keys off them, so a preview that
#: could change them could move the overlay onto another monitor
#: mid-nudge.
PREVIEW_CALIBRATION_KEYS = frozenset(
    {"center_x", "center_y", "radius_px", "safety_margin_pct"}
)


@dataclasses.dataclass(frozen=True)
class ControlState:
    """One parsed snapshot of the control file.

    Frozen because this is a *reading* of someone else's file, not
    mutable local state — the display applies it and keeps its own
    applied-state separately.
    """

    #: `None` = no manual override (follow the schedule, when one
    #: exists). `True`/`False` = the user said so explicitly.
    blanked: bool | None = None
    paused: bool = False
    #: Which picture the pause is pinned to. Advisory: the display
    #: re-pins from this only when it *changes* or when `paused` newly
    #: becomes True, because Next-while-paused moves the display's pin
    #: before the UI's next write lands (see `ControlChannel.poll`).
    paused_on_id: str | None = None
    advance: int = 0
    #: `Check for new pictures now`. A second monotonic counter,
    #: deliberately the same shape as `advance` rather than a `bool`
    #: flag: a flag has no way to distinguish "asked again" from "still
    #: asking", so it would either re-poll on every tick until someone
    #: cleared it (and the display may not write this file) or fire once
    #: and never again after a UI restart. The counter has neither
    #: problem, and it inherits `advance`'s replay-safety for free.
    refresh: int = 0
    preview_calibration: dict[str, float] | None = None
    written_at: float = 0.0

    def effective_blanked(
        self,
        schedule: object | None = None,
        now: float | None = None,
    ) -> bool:
        """Whether the View should currently be dark.

        seam, now wired. With no schedule this is `bool(blanked)`
        — byte-for-byte Step 1's behaviour, which is what keeps every
        existing caller and test correct — and with one it defers to
        `blank_schedule.effective_blanked`, which owns the override
        expiry rule.

        The schedule is passed in rather than read here because it lives
        in `settings.json` and this object is a parse of `command.json`.
        Reaching across to the other file would give this module a
        second, hidden input and make the whole thing untestable without
        a filesystem.
        """
        if schedule is None:
            return bool(self.blanked)
        from display import blank_schedule

        return blank_schedule.effective_blanked(
            self.blanked, self.written_at, schedule, now
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "blanked": self.blanked,
            "paused": self.paused,
            "paused_on_id": self.paused_on_id,
            "advance": self.advance,
            "refresh": self.refresh,
            "preview_calibration": self.preview_calibration,
            "written_at": self.written_at,
        }


@dataclasses.dataclass(frozen=True)
class ControlUpdate:
    """What one successful poll observed.

    `steps` is already the clamped, signed delta — positive for Next,
    negative for Previous — so no caller has to remember the arithmetic
    or the clamp.
    """

    state: ControlState
    steps: int = 0
    #: Whether this poll observed a *new* `refresh` counter value, i.e.
    #: the user pressed `Check for new pictures now`. Not a count: two
    #: presses inside one tick are one re-poll, because unlike `advance`
    #: the action is idempotent and re-listing twice in 250ms would only
    #: hit the source harder for the same answer.
    refresh_requested: bool = False


# -- defensive parsing --------------------------------------------------


def _as_optional_bool(value: object) -> bool | None:
    """Tri-state, and deliberately strict: only a real `bool` counts.

    `bool(value)` would read a leftover string `"false"` as True — the
    exact failure that leaves someone's View dark with no way to explain
    why. Anything that is not a bool or `None` is treated as "no opinion".
    """
    if isinstance(value, bool):
        return value
    return None


def _as_bool(value: object, default: bool = False) -> bool:
    return value if isinstance(value, bool) else default


def _as_optional_str(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _as_int(value: object, default: int = 0) -> int:
    """`bool` is a subclass of `int` in Python, so it is excluded
    explicitly — `{"advance": true}` must not read as 1."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value == int(value):
        return int(value)
    return default


def _as_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    return default


def _as_preview_calibration(value: object) -> dict[str, float] | None:
    """Allow-list `PREVIEW_CALIBRATION_KEYS` with finite numeric values.

    An empty result is `None`, not `{}`: "a preview with nothing in it"
    and "no preview" are the same thing to every caller, and collapsing
    them here means no caller has to check both.
    """
    if not isinstance(value, dict):
        return None
    preview: dict[str, float] = {}
    for key in PREVIEW_CALIBRATION_KEYS:
        raw = value.get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        number = float(raw)
        # NaN/inf would propagate straight into destination_rect() and
        # out into AppKit as a garbage frame.
        if number != number or number in (float("inf"), float("-inf")):
            continue
        preview[key] = number
    return preview or None


def parse_control(data: object) -> ControlState:
    """Turn arbitrary already-decoded JSON into a `ControlState`.

    Never raises, for any input, including `None`, a list, or a dict of
    hostile types. Each field falls back independently: one bad value
    costs that field, not the whole file — the same "degrade minimally,
    not maximally" rule `cache.py` applies per manifest entry.
    """
    if not isinstance(data, dict):
        return ControlState()

    unknown = set(data) - ALLOWED_KEYS
    if unknown:
        logger.debug(
            "control: ignoring unrecognized key(s) %s in the command file.",
            sorted(unknown),
        )

    return ControlState(
        blanked=_as_optional_bool(data.get("blanked")),
        paused=_as_bool(data.get("paused")),
        paused_on_id=_as_optional_str(data.get("paused_on_id")),
        advance=_as_int(data.get("advance")),
        refresh=_as_int(data.get("refresh")),
        preview_calibration=_as_preview_calibration(data.get("preview_calibration")),
        written_at=_as_float(data.get("written_at")),
    )


def read_control(path: Path | str) -> ControlState | None:
    """Read and parse the control file.

    Returns `None` — distinct from a default `ControlState` — when the
    file is absent, unreadable, or not JSON. The distinction matters: a
    caller must never treat "I could not read the file" as "the user
    wants nothing", because that would silently un-blank a blanked View
    the first time a read hiccups.
    """
    try:
        raw = Path(path).read_text()
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.warning("control: %s is not valid JSON; ignoring it.", path)
        return None
    return parse_control(data)


def write_control(path: Path | str, state: ControlState) -> bool:
    """Write desired state atomically. **For the UI only.**

    Lives here so the schema has exactly one definition rather than one
    per process, but nothing on the display side calls it — see the
    module docstring. Returns False rather than raising on a write
    failure, matching every other writer in this project.
    """
    try:
        atomic_write_json(Path(path), state.to_dict())
    except OSError as exc:
        logger.warning("control: could not write %s (%s).", path, exc)
        return False
    return True


# -- the display side ---------------------------------------------------


class ControlChannel:
    """The display's read-only end of the channel.

    Owns the two things that make `advance` behave: the persisted
    `last_seen_advance`, and the startup adoption that keeps an hour-old
    counter value from replaying as a burst of Next presses the moment
    the service comes back.

    Change detection is `(st_mtime_ns, st_size, st_ino)` — the same
    triple used for config hot-reload, and for the same reason:
    mtime alone has coarse enough resolution on some filesystems to miss
    a write that lands in the same tick as the one before it. Unchanged
    means no JSON parse at all, which is what makes running this four
    times a second free.
    """

    def __init__(
        self,
        path: Path | str | None = None,
        max_steps: int = MAX_ADVANCE_STEPS,
    ) -> None:
        self._path = Path(path) if path is not None else paths.command_path()
        self._max_steps = max(0, int(max_steps))
        self._signature: tuple[int, int, int] | None = None
        self._last_seen_advance = 0
        self._last_seen_refresh = 0
        self._state = ControlState()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def state(self) -> ControlState:
        """The last successfully-parsed state. A failed or skipped read
        never mutates this — a corrupt file leaves the last good desired
        state in place, exactly as config hot-reload leaves the last good config."""
        return self._state

    @property
    def last_seen_advance(self) -> int:
        return self._last_seen_advance

    @property
    def last_seen_refresh(self) -> int:
        return self._last_seen_refresh

    def _stat_signature(self) -> tuple[int, int, int] | None:
        try:
            stat = self._path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size, stat.st_ino)

    def adopt_current(self) -> ControlState:
        """Startup: read the file, adopt its counter as already-seen, and
        return the desired state so the caller can apply it.

        This is the whole of "on startup, adopt current state as
        already-seen and never replay". Note the asymmetry, which is the
        point: **desired state is applied, the counter delta is not.** A
        View that was blanked when the machine went to sleep comes back
        blanked; a Next pressed before the machine went to sleep is gone,
        which is what anyone would expect of a keypress.
        """
        self._signature = self._stat_signature()
        parsed = read_control(self._path)
        if parsed is not None:
            self._state = parsed
            self._last_seen_advance = parsed.advance
            # Adopted for the same reason as `advance`, and it matters
            # more here: the display polls its source at startup anyway,
            # so replaying an old refresh would be a duplicate poll on
            # every single launch.
            self._last_seen_refresh = parsed.refresh
        return self._state

    def poll(self) -> ControlUpdate | None:
        """One control tick's worth of work.

        Returns `None` when there is nothing to do — the file is
        unchanged since the last look, or it is absent, or it is corrupt.
        All three cases deliberately produce the *same* answer: do
        nothing, keep the last good state, and do not log again next
        tick. A corrupt file's stat signature is recorded before the
        parse is attempted, so a file that stays corrupt is parsed (and
        complained about) exactly once rather than four times a second
        forever.
        """
        signature = self._stat_signature()
        if signature == self._signature:
            return None
        self._signature = signature
        if signature is None:
            # The file is gone. Deliberately NOT a reset to defaults:
            # deleting command.json must not silently un-blank a blanked
            # View or un-pause a paused one. The last good state stands
            # until something writes a new one.
            return None

        parsed = read_control(self._path)
        if parsed is None:
            return None

        steps = self._steps_for(parsed.advance)
        self._last_seen_advance = parsed.advance
        # `!=`, not `>`, for the reason `_steps_for` documents at length:
        # a UI that reinstalls and restarts its counter at zero must not
        # wedge this permanently.
        refresh_requested = parsed.refresh != self._last_seen_refresh
        self._last_seen_refresh = parsed.refresh
        self._state = parsed
        return ControlUpdate(
            state=parsed, steps=steps, refresh_requested=refresh_requested
        )

    def _steps_for(self, advance: int) -> int:
        """`!=`, not `>`.

        Under `>`, a UI that reinstalls and starts counting from zero
        never again produces a delta the display will act on: the channel
        is wedged permanently, with no error anywhere and no way for the
        user to discover why Next stopped working. Under `!=` the same
        event costs at most `MAX_ADVANCE_STEPS` pictures of unexpected
        movement, once.

        A negative delta is a real, intended value: Previous decrements
        the same counter. See this module's `__all__` note and the
        report accompanying Step 1.2 shows only one counter field
        while both Next and Previous must survive fast
        clicking, and a signed delta on the single documented field is
        the reading that satisfies both without inventing a field the
        plan does not describe.
        """
        if advance == self._last_seen_advance:
            return 0
        delta = advance - self._last_seen_advance
        if delta > self._max_steps:
            return self._max_steps
        if delta < -self._max_steps:
            return -self._max_steps
        return delta


__all__ = [
    "ALLOWED_KEYS",
    "MAX_ADVANCE_STEPS",
    "PREVIEW_CALIBRATION_KEYS",
    "ControlChannel",
    "ControlState",
    "ControlUpdate",
    "parse_control",
    "read_control",
    "write_control",
]
