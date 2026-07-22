"""Scheduled blanking — schedule half, Step 4.

The blank state uses a split state model, explicit about the condition
attached to it:

    `blank_manual: bool | null` — `null` means "follow the schedule"
    effective state = `blank_manual` if non-null, else `in_schedule_window()`
    a manual override clears back to `null` at the next window *boundary*

    **If that surface gets cut from v1, cut scheduled blanking with it**
    — do not ship the one-flag version, and do not ship the split-state
    model with no way to turn the schedule on.

Step 1 shipped the tri-state field (`control.ControlState.blanked` is
already `bool | None`) with `effective_blanked()` documented as "the one
place a schedule gets wired in later". This module is that wiring, and
Step 4's settings window is the surface that makes it legitimate.

**"Clears back to null at the next boundary" is computed, not written.**
The obvious reading — someone rewrites `command.json` with `null` when a
boundary passes — has no correct owner. The one-writer rule forbids
the display from writing the command file, and putting it in the UI
would mean the override never clears when the menu bar is quit, which
must remain a supported way to run.

So the override expires *by comparison* instead: `written_at` is already
on the channel, and an override written before the most recent boundary
is simply no longer in force. That is a pure function of
`(blanked, written_at, schedule, now)` — no writes, no second writer, no
dependence on any process being alive at the moment a boundary passes,
and a machine that was asleep across 07:00 gets the right answer when it
wakes rather than the answer it would have computed at 07:00.

Everything here is defensive in this project's usual sense: a malformed
schedule is a disabled schedule, and nothing raises. A blank View that
cannot be explained is the worst outcome this feature has available, so
every ambiguous case resolves toward "not blanked".
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Mapping
from typing import Any

#: Minutes in a day. A window is expressed as two minutes-since-midnight
#: values in *local* time, which is what a person setting "9 PM" means —
#: not UTC, and not a fixed offset that would drift across a DST change.
MINUTES_PER_DAY = 24 * 60

#: Own example values, and a defensible default for the surface's
#: first use: the checkbox starts off, so these are only ever seen as the
#: pre-filled contents of two disabled fields.
DEFAULT_START_MINUTE = 21 * 60  # 9:00 PM
DEFAULT_END_MINUTE = 7 * 60  # 7:00 AM


@dataclasses.dataclass(frozen=True)
class BlankSchedule:
    """A nightly blanking window, in local minutes since midnight.

    Frozen, like every other config object in this project: it is a
    reading of a file another process may rewrite at any moment, and the
    display holds one across ticks.
    """

    enabled: bool = False
    start_minute: int = DEFAULT_START_MINUTE
    end_minute: int = DEFAULT_END_MINUTE

    @property
    def is_active(self) -> bool:
        """Whether this schedule can ever blank anything.

        A zero-length window (start == end) is treated as **never**, not
        as always. Both readings are defensible from the arithmetic; only
        one of them is safe. A user who fat-fingers both fields to the
        same value gets a View that keeps showing pictures and an
        obviously-wrong-looking row in the settings window, rather than a
        permanently dark device and no way to tell why.
        """
        return self.enabled and self.start_minute != self.end_minute

    def in_window(self, now: float | None = None) -> bool:
        """Whether `now` falls inside the window.

        Handles the wrapping case, which is the *normal* one here: a
        blanking window is nearly always overnight (21:00 to 07:00), so
        `start > end` is the shape to get right rather than the edge case.
        """
        if not self.is_active:
            return False
        minute = local_minute_of_day(now)
        if self.start_minute < self.end_minute:
            return self.start_minute <= minute < self.end_minute
        return minute >= self.start_minute or minute < self.end_minute

    def most_recent_boundary(self, now: float | None = None) -> float:
        """Timestamp of the latest window edge at or before `now`.

        The two edges recur daily, so the candidates are today's and
        yesterday's start and end; the answer is the largest that is not
        in the future. Yesterday's are needed because at 00:30 with a
        21:00–07:00 window the most recent boundary is *last night's*
        21:00, and an override written at 22:00 must still be in force.

        Each day's midnight is resolved through `localtime`/`mktime`
        separately rather than by subtracting 86400 from the other, so a
        DST transition shifts the boundary by the same hour the user's
        clock shifted instead of leaving it an hour out for a day.
        """
        moment = time.time() if now is None else float(now)
        candidates = []
        for day_offset in (0, -1):
            midnight = _local_midnight(moment + day_offset * 86400.0)
            for minute in (self.start_minute, self.end_minute):
                candidates.append(midnight + minute * 60.0)
        past = [edge for edge in candidates if edge <= moment]
        # `past` is only empty if both edges of both days are in the
        # future, which the arithmetic above cannot produce. Returning
        # -inf rather than raising keeps the "nothing here raises" rule
        # intact for an input that should not exist.
        return max(past) if past else float("-inf")

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "start": format_minute(self.start_minute),
            "end": format_minute(self.end_minute),
        }


def local_minute_of_day(now: float | None = None) -> int:
    moment = time.time() if now is None else float(now)
    parts = time.localtime(moment)
    return parts.tm_hour * 60 + parts.tm_min


def _local_midnight(moment: float) -> float:
    parts = time.localtime(moment)
    return time.mktime(
        (parts.tm_year, parts.tm_mon, parts.tm_mday, 0, 0, 0, 0, 0, -1)
    )


def format_minute(minute: object) -> str:
    """Minutes-since-midnight as `9:00 PM`.

    Twelve-hour with a meridiem because that is the form own
    status copy uses ("Turned back on until 7:00 AM") and the form the
    settings window shows in its two fields. `parse_minute` accepts both
    that and 24-hour, so nobody is forced to type a space and two capital
    letters to set a bedtime.
    """
    value = _coerce_minute(minute)
    if value is None:
        return ""
    hour, rest = divmod(value, 60)
    meridiem = "AM" if hour < 12 else "PM"
    display_hour = hour % 12 or 12
    return f"{display_hour}:{rest:02d} {meridiem}"


def parse_minute(text: object) -> int | None:
    """`"9:00 PM"`, `"9 PM"`, `"21:00"`, `"2100"` -> minutes. None if
    unparseable — never raises, and never guesses at something that is
    not obviously a time.

    Returning None rather than a fallback is deliberate: this parses a
    field the user is actively typing into, and silently substituting a
    default for `"9:0"` mid-keystroke would fight them. The caller keeps
    the last good value instead.
    """
    if isinstance(text, bool):
        return None
    if isinstance(text, (int, float)):
        return _coerce_minute(int(text))
    if not isinstance(text, str):
        return None
    cleaned = text.strip().upper().replace(".", "")
    if not cleaned:
        return None

    meridiem = None
    for suffix in ("AM", "PM"):
        if cleaned.endswith(suffix):
            meridiem = suffix
            cleaned = cleaned[: -len(suffix)].strip()
            break

    if ":" in cleaned:
        hour_text, _, minute_text = cleaned.partition(":")
    elif cleaned.isdigit() and len(cleaned) in (3, 4):
        # `2100` and `900`. Accepted because a numeric keypad makes this
        # the fastest thing to type and it is unambiguous at those two
        # lengths.
        hour_text, minute_text = cleaned[:-2], cleaned[-2:]
    else:
        hour_text, minute_text = cleaned, "0"

    try:
        hour = int(hour_text.strip())
        minute = int(minute_text.strip())
    except ValueError:
        return None
    if not (0 <= minute < 60):
        return None

    if meridiem is not None:
        if not (1 <= hour <= 12):
            return None
        hour = hour % 12
        if meridiem == "PM":
            hour += 12
    elif not (0 <= hour < 24):
        return None

    return hour * 60 + minute


def _coerce_minute(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = int(value)
    if not (0 <= number < MINUTES_PER_DAY):
        return None
    return number


def parse_schedule(data: object) -> BlankSchedule:
    """Validate a `blank_schedule` block. Never raises.

    A block that is present but unusable comes back **disabled** rather
    than as a default-shaped enabled schedule. Falling back to an
    enabled 21:00–07:00 would mean a typo in a hand-edited settings file
    could blank someone's View overnight with no trace of why, which is
    exactly the failure the split-state model guards against.
    """
    if not isinstance(data, Mapping):
        return BlankSchedule()
    start = parse_minute(data.get("start"))
    end = parse_minute(data.get("end"))
    if start is None or end is None:
        return BlankSchedule()
    enabled = data.get("enabled")
    return BlankSchedule(
        enabled=enabled if isinstance(enabled, bool) else False,
        start_minute=start,
        end_minute=end,
    )


def effective_blanked(
    manual: bool | None,
    written_at: float = 0.0,
    schedule: BlankSchedule | None = None,
    now: float | None = None,
) -> bool:
    """Effective state. The one place the two halves combine.

    - No active schedule: the manual flag alone, `None` meaning off.
      This is exactly Step 1's behaviour, so a user who never touches the
      new checkbox sees no change whatsoever.
    - Active schedule, no override: the window decides.
    - Active schedule with an override: the override holds until the next
      boundary, then the schedule resumes. That is "clears back to
      `null` at the next window boundary, so the schedule resumes tonight
      without the user remembering anything" — computed rather than
      written, per this module's docstring.

    An override carrying no usable `written_at` (0.0, the dataclass
    default, or a negative clock) is treated as **stale**. It cannot be
    placed relative to any boundary, and the standing instruction the
    user configured is a better guess than an undatable flag.
    """
    if schedule is None or not schedule.is_active:
        return bool(manual)
    if manual is None:
        return schedule.in_window(now)
    moment = time.time() if now is None else float(now)
    stamp = written_at if isinstance(written_at, (int, float)) else 0.0
    if isinstance(written_at, bool) or stamp <= 0.0:
        return schedule.in_window(now)
    if stamp <= schedule.most_recent_boundary(moment):
        return schedule.in_window(now)
    return bool(manual)


def describe(
    manual: bool | None,
    written_at: float = 0.0,
    schedule: BlankSchedule | None = None,
    now: float | None = None,
) -> str:
    """Status line: the effective blank state, in words.

    The two interesting strings are `Following schedule` and
    `Turned back on until 7:00 AM` — and the point of both is that the
    user can tell *why* the View looks the way it does. The remaining
    cases get plain equivalents rather than being left blank, because a
    status line that is sometimes empty is one the eye stops checking.
    """
    if schedule is None or not schedule.is_active:
        if manual:
            return "Blanked. Turn it back on from the menu bar."
        return "Showing pictures. No automatic blanking is set."

    overridden = _override_in_force(manual, written_at, schedule, now)
    if not overridden:
        if schedule.in_window(now):
            return (
                f"Following schedule. Blank until "
                f"{format_minute(schedule.end_minute)}."
            )
        return (
            f"Following schedule. Blank from "
            f"{format_minute(schedule.start_minute)}."
        )

    # An override is in force, so it holds until the *next* boundary,
    # which is the one thing the user needs to know: how long the thing
    # they just did lasts.
    until = format_minute(
        schedule.end_minute if schedule.in_window(now) else schedule.start_minute
    )
    if manual:
        return f"Blanked by hand until {until}."
    return f"Turned back on until {until}."


def _override_in_force(
    manual: bool | None,
    written_at: float,
    schedule: BlankSchedule,
    now: float | None,
) -> bool:
    if manual is None:
        return False
    stamp = written_at if isinstance(written_at, (int, float)) else 0.0
    if isinstance(written_at, bool) or stamp <= 0.0:
        return False
    moment = time.time() if now is None else float(now)
    return stamp > schedule.most_recent_boundary(moment)


__all__ = [
    "DEFAULT_END_MINUTE",
    "DEFAULT_START_MINUTE",
    "MINUTES_PER_DAY",
    "BlankSchedule",
    "describe",
    "effective_blanked",
    "format_minute",
    "local_minute_of_day",
    "parse_minute",
    "parse_schedule",
]
