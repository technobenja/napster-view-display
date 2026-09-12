"""Everything the menu bar decides, with no AppKit in it.

`menubar.py` is the AppKit shell: a status item, a menu, and a timer. This
module is the part that can be wrong in an interesting way, so it is the
part that is separable and tested. Nothing here imports AppKit, touches a
run loop, or knows what an `NSMenuItem` is; everything is a pure function
of a decoded `status.json` document plus a clock reading.

Three things live here:

**State precedence.** Six actionable states, evaluated in one
fixed order, one shown at a time. The order is not cosmetic — it is the
plan's own reasoning encoded: `Not showing pictures` outranks everything
because a dead heartbeat means *nothing else in `status.json` can be
trusted*, so showing a possibly-hours-stale `View not connected` would
actively mislead. Everything after that is ordered most-fundamental
first: you cannot be `No pictures` in a way worth saying if the View
isn't plugged in, and `Blanked`/`Paused` are user-caused and therefore
last.

**Staleness.** `heartbeat_at` older than `STALE_AFTER_S` means the
display process is not running. A missing or non-numeric `heartbeat_at`
is stale too — "I cannot tell" and "it is dead" get the same answer,
because the menu that results is the useful one either way (it offers
`Start showing pictures`, which is idempotent).

**The menu bar's own heartbeat**, `write_ui_heartbeat` /
`read_ui_heartbeat`, judged by the *same* `is_stale()`. Until this
existed the liveness primitive pointed one way only: `display/app.py`
writes `heartbeat_at` and this module reads it, so a wedged display agent
was detectable — but a wedged menu bar, which is what actually happened
on 2026-09-08, was indistinguishable from a healthy one. `ui.lock` proves
the holder *exists*; `flock` cannot say more than that, because the
kernel releases it only on process death. The heartbeat is what says the
holder is *serving*.

Deliberately no second staleness predicate: `is_stale()` is already
NaN-safe and backwards-clock-safe, and two of them would drift.

**Command construction.** The UI's writes are desired *state*
plus a monotonic `advance` counter, never a queue. Building the next
`ControlState` from the current one is arithmetic, so it is here and not
tangled into a click handler.

The transient-title rule is also here, in `TitleTracker`: the
label is shown when the UI **observes `last_shown_id` change**, never
when a menu item is clicked. Click-driven text would confirm a change
that never happened whenever the agent is wedged — which is precisely
what the heartbeat exists to catch — and would flash three names when
counter collapses three fast clicks into one apply.
"""

from __future__ import annotations

import dataclasses
import enum
import json
import math
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from display.atomic_io import atomic_write_json

# "Heartbeat stale >5s -> title reads `Not showing pictures`".
# app.py writes the heartbeat every 2s, so this is 2.5x margin: two
# consecutive heartbeats must be missed before the UI calls the display
# dead. That margin is what keeps a busy machine from flickering the
# title during a slow poll.
STALE_AFTER_S = 5.0

# How often the menu bar writes its own heartbeat, mirroring
# `app.py`'s HEARTBEAT_INTERVAL_S and for the same arithmetic: 2.0s
# against STALE_AFTER_S = 5.0 is 2.5x margin, so two consecutive
# heartbeats must be missed before anything calls this process dead.
#
# Kept here, next to the threshold that judges it, rather than in
# menubar.py — the display's pair is split across two processes and has
# to be, but both halves of this one live in `ui/`, so the 2.5x margin
# can be (and is) asserted by a test that imports no AppKit. The poll
# timer that carries it runs at 0.4s, so this is written on every fifth
# tick, not on every tick: at 4 writes a second, status.json's own
# comment counts ~345,000 write-and-rename pairs a day on a machine meant
# to sit quietly in someone's home for years.
UI_HEARTBEAT_INTERVAL_S = 2.0

# The threshold for *accusing* the menu bar of not responding, as
# distinct from the one that picks a menu title. Read only by
# `ui/contention_state.py`.
#
# It is deliberately not STALE_AFTER_S. That constant is 5.0 against a
# realized worst-case write gap of 2.4s, i.e. 0.2s of slack, and it is
# tuned for a label that costs nothing when it flickers. This one drives
# a sentence that tells a human their app is broken and names a pid to
# force-quit, so it must not be the tight one.
#
# 20.0 costs nothing in responsiveness — the age is read instantly from a
# stamp, never waited for — and the human who double-clicked did so
# because nothing had happened for a while, so the fault is already older
# than any transient. It is still well under the 51.2s an About box was
# MEASURED to freeze this stamp for on 2026-09-10 — a reading taken
# before v1.1.6 made that particular window non-modal, and one the
# alerts still left modal in Settings and Adjust the circle can produce
# just as easily. That is why nothing
# downstream of it may act destructively: crossing this threshold is
# necessary for the "not responding" verdicts and never sufficient on its
# own. See `contention_state.classify`, where the window-server check is
# what separates "modally busy in front of the user" from "wedged".
UI_UNRESPONSIVE_AFTER_S = 20.0

# The transient label sits in the menu bar for about three seconds
# after the picture actually changes.
TITLE_HOLD_S = 3.0

# `display_label` is capped at ~28 characters at the source boundary.
# Re-applied here rather than trusted, because this string arrives from a
# file on disk that a stranger's source populated, and the menu bar is
# not the place to discover that someone's folder contains a 400-
# character filename.
LABEL_MAX_CHARS = 28

# The ellipsis is a real one-character U+2026, not "...", so truncation
# costs one column rather than three. Not an icon and not decoration --
# no-emoji rule is about pictographs, and this is punctuation.
ELLIPSIS = "…"


class State(enum.Enum):
    """The persistent title states, **declared in precedence
    order** — `evaluate_state` relies on that, so reordering these
    members changes behaviour and is not a cosmetic edit.

    `NORMAL` is first and is not a state anyone displays; it is the
    "nothing to say" answer, and the normal case
    shows the icon alone.
    """

    NORMAL = ""
    NOT_SHOWING = "Not showing pictures"
    VIEW_NOT_CONNECTED = "View not connected"
    SETUP_NEEDED = "Setup needed"
    NO_PICTURES = "No pictures"
    BLANKED = "Blanked"
    PAUSED = "Paused"

    @property
    def title(self) -> str:
        """The exact string that goes beside the icon. Empty for
        `NORMAL`, which is how the shell knows to show no text."""
        return self.value

    @property
    def is_actionable(self) -> bool:
        return self is not State.NORMAL


@dataclasses.dataclass(frozen=True)
class Status:
    """One reading of `status.json`, reduced to what the menu bar uses.

    Frozen and defaulted throughout: this is a parse of a file written by
    *another process*, and every field has to survive that file being
    absent, half-written, hand-edited, or produced by a newer build. The
    defaults are chosen so that an all-defaults `Status` describes the
    honest worst case — nothing running, nothing connected, no pictures —
    rather than a cheerful one.
    """

    heartbeat_at: float = 0.0
    view_connected: bool = False
    image_count: int = 0
    blanked: bool = False
    paused: bool = False
    last_shown_id: str | None = None
    display_label: str = ""
    source_label: str = ""
    last_error: str | None = None

    #: False only when the file could not be read at all. Distinct from
    #: "read it and everything was default": the shell shows a different
    #: menu for "no status file has ever existed" (first run) than for
    #: "the display wrote one and then died".
    present: bool = False


def _read_text(path: Path | str) -> str:
    """Read a state file as text. Raises only `OSError`.

    🔴 `errors="replace"`, and it is the whole point of this helper.
    `Path.read_text()` with the default strict codec raises
    `UnicodeDecodeError` on a file with one bad byte — and
    `UnicodeDecodeError` is a **`ValueError`, not an `OSError`**, so it
    sails straight through an `except OSError` written to make a reader
    total. Both readers below promise in bold that they never raise, and
    both did, on any `ui_status.json` or `status.json` containing a
    single invalid byte. A half-written file interrupted by a crash is
    exactly how that happens.

    Replacing the bad bytes keeps the read total; the mangled text then
    fails `json.loads` and lands on the degradation path that was always
    meant to catch it. The callers' `UnicodeDecodeError` handlers are
    gone with this, and they were provably dead anyway: `json.loads` can
    only raise it when handed *bytes*, and it is handed a `str`.

    Nine other `read_text()` calls in this project have the same shape.
    They are outside this change and are not touched here; this one is
    fixed because this phase put a new reader and a new tool in front of
    it.
    """
    return Path(path).read_text(encoding="utf-8", errors="replace")


def _as_float(value: object, default: float = 0.0) -> float:
    """`bool` is excluded explicitly — it is an `int` subclass in Python,
    and `{"heartbeat_at": true}` reading as 1.0 (i.e. 1970) would be a
    silently wrong *timestamp* rather than an obviously wrong one."""
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        number = float(value)
        # NaN fails every comparison, so a NaN heartbeat would read as
        # "not stale" forever -- the one answer that leaves the user with
        # a menu whose actions all silently do nothing.
        if number == number and number not in (float("inf"), float("-inf")):
            return number
    return default


def _as_int(value: object, default: int = 0) -> int:
    """`bool` is excluded for the same reason `_as_float` excludes it.

    🔴 And non-finite floats are excluded explicitly, which is the thing
    this function was missing while its sibling had it: JSON permits
    `NaN` and `1e400`, `float("1e400")` is `inf`, and `int(inf)` raises
    **OverflowError** while `int(nan)` raises **ValueError**. Neither is
    an `OSError`, so both escaped every caller written to be total — and
    this phase routes `ui_status.json`'s `pid` through here, which is a
    number that decides which process gets a signal. The asymmetry with
    `_as_float`, which has carried a NaN guard and a comment explaining
    it, was the tell.
    """
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value == int(value):
        return int(value)
    return default


def _as_bool(value: object, default: bool = False) -> bool:
    return value if isinstance(value, bool) else default


def _as_str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _as_optional_str(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def parse_status(data: object) -> Status:
    """Turn arbitrary already-decoded JSON into a `Status`.

    Never raises, for any input — including `None`, a list, or a dict of
    hostile types — and degrades per field rather than wholesale, the
    same rule `control.parse_control` follows on the other end of the
    channel. One bad `image_count` must not cost the heartbeat reading
    that tells the user whether anything is running at all.
    """
    if not isinstance(data, Mapping):
        return Status()
    return Status(
        heartbeat_at=_as_float(data.get("heartbeat_at")),
        view_connected=_as_bool(data.get("view_connected")),
        image_count=_as_int(data.get("image_count")),
        blanked=_as_bool(data.get("blanked")),
        paused=_as_bool(data.get("paused")),
        last_shown_id=_as_optional_str(data.get("last_shown_id")),
        display_label=_as_str(data.get("display_label")),
        source_label=_as_str(data.get("source_label")),
        last_error=_as_optional_str(data.get("last_error")),
        present=True,
    )


def read_status(path: Path | str) -> Status:
    """Read and parse `status.json`. Never raises.

    An unreadable or non-JSON file returns `Status(present=False)` rather
    than `None`: every caller would immediately have to substitute
    defaults anyway, and `present` carries the distinction that actually
    matters to the menu.
    """
    try:
        raw = _read_text(path)
    except OSError:
        return Status()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return Status()
    return parse_status(data)


def is_stale(heartbeat_at: float, now: float, threshold: float = STALE_AFTER_S) -> bool:
    """`now - heartbeat_at > threshold`.

    A *future* heartbeat is deliberately not stale. Clocks move backwards
    — NTP steps, timezone-adjacent daylight handling, a VM resuming — and
    the failure mode of treating a future timestamp as stale is that the
    UI declares a perfectly healthy display dead and offers to start a
    second one.
    """
    if heartbeat_at <= 0.0:
        return True
    return (now - heartbeat_at) > threshold


@dataclasses.dataclass(frozen=True)
class UiStatus:
    """Everything `ui_status.json` carries, as the reader sees it.

    **Every field is optional in the document and defaulted here**, in
    both directions. A v1.1.5 menu bar writes only `ui_heartbeat_at`, so
    a v1.1.6 reader must tolerate the other four being absent — that is
    what the defaults are for. And a future version may add keys this
    reader has never heard of, which it ignores rather than rejecting;
    `read_ui_status` reads named keys and never enumerates the document.

    `present` is "the file parsed as an object", not "the process is
    healthy". A file with `present=True` and `heartbeat_at=0.0` is a
    document that exists and carries no usable stamp.

    The three process fields describe the process that **last served**,
    which is exactly the point of putting them here rather than in the
    lock file: the timer is the sole writer, so the file exists only
    because something serviced a default-mode timer, and a *stale* file
    then names the process that was serving when it stopped. That is the
    question a diagnostician arrives with.

    `stacks_armed` is the one field with a safety job. `SIGUSR1`'s
    default disposition is **terminate**, and the handler that makes it a
    diagnostic instead exists only from v1.1.3. Anything deciding whether
    to signal this pid must read this field and must treat its absence as
    False, or the "capture the evidence first" path kills the process it
    was preserving.
    """

    heartbeat_at: float = 0.0
    pid: int | None = None
    exe: str | None = None
    stacks_armed: bool = False
    stacks_path: str | None = None
    present: bool = False


def read_ui_status(path: Path | str) -> UiStatus:
    """Read and parse `ui_status.json`. **Never raises.**

    Same degradation rule as `read_status`: absent, unreadable, truncated
    mid-write, not JSON, or not an object all return the default record,
    whose `heartbeat_at` of `0.0` makes `is_stale()` answer True. A
    process whose heartbeat cannot be read is not one anything should
    treat as serving.

    One parser, not two. `read_ui_heartbeat` is implemented on top of
    this rather than beside it — a second code path answering the same
    question does not fail loudly when it drifts, it answers confidently
    and differently (this project has the scar: a Settings *Test* button
    that built its own request reported "no starred pictures" while fifty
    of them were rotating on the glass).
    """
    try:
        raw = _read_text(path)
    except OSError:
        return UiStatus()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return UiStatus()
    if not isinstance(data, Mapping):
        return UiStatus()
    pid = _as_int(data.get("pid"), default=0)
    return UiStatus(
        heartbeat_at=_as_float(data.get("ui_heartbeat_at")),
        # A pid of 0 or below is not a pid anything may signal or look
        # up, so it degrades to "the document did not say" rather than
        # travelling onwards as a number.
        pid=pid if pid > 0 else None,
        exe=_as_optional_str(data.get("exe")),
        stacks_armed=_as_bool(data.get("stacks_armed")),
        stacks_path=_as_optional_str(data.get("stacks_path")),
        present=True,
    )


def read_ui_heartbeat(path: Path | str) -> float:
    """The menu bar's own `ui_heartbeat_at`, or `0.0`. Never raises.

    A bare float, deliberately: callers that only want to ask
    `is_stale()` should not have to hold a record to do it. Anything
    needing the pid, the executable or the stack-dump state reads
    `read_ui_status` instead — this is that record's `heartbeat_at` and
    nothing more.
    """
    return read_ui_status(path).heartbeat_at


def write_ui_heartbeat(
    path: Path | str,
    now: float | None = None,
    *,
    pid: int | None = None,
    exe: str | None = None,
    stacks_armed: bool | None = None,
    stacks_path: str | None = None,
) -> bool:
    """Stamp `ui_status.json`. **Never raises.**

    That is not a nicety, it is the whole safety argument for putting
    this on a live timer. `display/app.py` considered and explicitly
    REVERTED reusing its signal-responsiveness timer for the display's
    heartbeat, because an exception off an NSTimer selector kills the run
    loop — and the failure mode of a *raising heartbeat* is specifically
    losing the ability to stop the service (`launchctl kickstart` then
    hangs indefinitely, confirmed the hard way). A heartbeat that can
    wedge the process it is meant to prove healthy is worse than no
    heartbeat at all.

    So this function swallows everything an `atomic_write_json` can
    produce and reports by return value, the same contract
    `control.write_control` and `app.merge_status` already follow.
    `OSError` is the live one — a full or read-only disk, a directory
    where the file should be. `TypeError` / `ValueError` cannot arise
    from the app's caller today, and are caught anyway so that a future
    field added to this document cannot turn a heartbeat into a crash;
    `merge_status` has the same pair for the same reason, and got them
    after an unserializable status value escaped into a caller's broad
    except and discarded an entirely successful poll. **That argument is
    no longer hypothetical here**: `exe` and `stacks_path` are strings
    this function is handed by a caller, and a caller that hands it
    something else gets `False` rather than a dead run loop.

    **The four optional fields are omitted when they are None**, so a
    caller that passes only a timestamp writes byte-for-byte what v1.1.5
    wrote. That keeps the no-information case honest — an absent `pid`
    means "nobody said", never "pid null" — and keeps this function
    usable from a test that is only interested in staleness.

    Still written whole, not merged into whatever is already on disk the
    way `merge_status` does. There is one writer, so a
    read-modify-write would buy nothing and cost a read every 2 seconds
    — and a merge would faithfully preserve a stale key written by some
    future version, which is the opposite of what a heartbeat wants.
    **With five fields instead of one, whole-file writing is what keeps
    them consistent**: the pid and the stamp are true of the same
    instant, and a reader can never catch a new stamp against a previous
    process's pid.

    The write is atomic (temp file + `os.replace`) for the same reason
    `status.json`'s is: a reader that caught a half-written file would
    read `0.0` and report a healthy process as dead.
    """
    moment = time.time() if now is None else now
    document: dict[str, Any] = {"ui_heartbeat_at": moment}
    if pid is not None:
        document["pid"] = pid
    if exe is not None:
        document["exe"] = exe
    if stacks_armed is not None:
        document["stacks_armed"] = stacks_armed
    if stacks_path is not None:
        document["stacks_path"] = stacks_path
    try:
        atomic_write_json(Path(path), document)
    except (OSError, TypeError, ValueError):
        return False
    return True


def setup_needed(settings_data: object) -> bool:
    """Whether the user has never chosen a picture source.

    Deliberately *not* "the resolved source is invalid".
    `source_settings.source_from_settings_data` always returns something
    — its last resort is a folder source pointing at `~/Pictures` — so
    asking it whether setup is needed would answer "no" for a user who
    has never opened the app. The honest question is whether the settings
    document carries a source the user (or a migration) actually put
    there: an explicit `source` block, or the legacy flat
    `image_studio_base_url` key that Step 0's migration reads.
    """
    if not isinstance(settings_data, Mapping):
        return True
    if isinstance(settings_data.get("source"), Mapping):
        return False
    return not isinstance(settings_data.get("image_studio_base_url"), str)


def evaluate_state(
    status: Status,
    *,
    now: float | None = None,
    needs_setup: bool = False,
) -> State:
    """Precedence, top to bottom, one state at a time.

    The order below is load-bearing and is the plan's, unchanged:

    1. `Not showing pictures` — a dead heartbeat means every other field
       in `status.json` is of unknown age, so nothing after this point
       may be trusted enough to display.
    2. `View not connected` — the device is unplugged; what is in the
       rotation is moot.
    3. `Setup needed` — there is a View but nothing to put on it.
    4. `No pictures` — configured, but the source came back empty.
    5. `Blanked`, 6. `Paused` — user-caused, both persist across restarts, and both are otherwise indistinguishable from a hung
       process, which is exactly why they are in this set at all.

    A failed poll or a skipped corrupt file is **not** in the list, on
    purpose: a 3am network hiccup must leave no text in the
    menu bar all morning. `last_error` is parsed and carried for the
    settings window and is not consulted here.
    """
    moment = time.time() if now is None else now
    if is_stale(status.heartbeat_at, moment):
        return State.NOT_SHOWING
    if not status.view_connected:
        return State.VIEW_NOT_CONNECTED
    if needs_setup:
        return State.SETUP_NEEDED
    if status.image_count <= 0:
        return State.NO_PICTURES
    if status.blanked:
        return State.BLANKED
    if status.paused:
        return State.PAUSED
    return State.NORMAL


def truncate_label(label: str, limit: int = LABEL_MAX_CHARS) -> str:
    """Cap a `display_label` for the menu bar, ellipsising if it had to
    be cut.

    The ellipsis replaces the last kept character rather than being
    appended, so the result is never longer than `limit` — a truncation
    that overflows its own limit is the bug this function exists to
    prevent. Whitespace is collapsed first: a filename with a newline in
    it is legal on macOS and would otherwise break the status item's
    single-line layout.
    """
    if limit <= 0:
        return ""
    collapsed = " ".join(str(label).split())
    if len(collapsed) <= limit:
        return collapsed
    if limit == 1:
        return ELLIPSIS
    return collapsed[: limit - 1].rstrip() + ELLIPSIS


class TitleTracker:
    """Decides when the transient picture label is showing.

    The trigger is **observation, not action**: `observe` is handed each
    poll's `last_shown_id`, and the label appears only when that value
    *changes* from a previously-known one. Three consequences the plan
    asks for fall out of that directly — a wedged agent shows no
    confirmation, three fast Next clicks that collapses into one
    apply produce one label rather than three, and the label reflects
    what is genuinely on the device rather than what was requested.

    The very first observation is deliberately not a change. The UI
    launching while a picture has been up for an hour must not announce
    it as new.
    """

    def __init__(self, hold_s: float = TITLE_HOLD_S) -> None:
        self._hold_s = max(0.0, float(hold_s))
        self._last_id: str | None = None
        self._seen_any = False
        self._shown_until = 0.0
        self._label = ""

    @property
    def label(self) -> str:
        """The label currently being held, or "" once it has expired.
        Reading this does not expire it — `active_label` does the
        clock comparison, so that a caller cannot get a stale answer by
        reading the wrong property."""
        return self._label

    def observe(self, image_id: str | None, label: str, now: float | None = None) -> bool:
        """Record one poll. Returns True if this started a new hold.

        `label` is truncated on the way in rather than on the way out, so
        the value the shell reads back is always the value it can render
        directly.
        """
        moment = time.time() if now is None else now
        changed = self._seen_any and image_id is not None and image_id != self._last_id
        if image_id is not None:
            self._last_id = image_id
            self._seen_any = True
        if not changed:
            return False
        text = truncate_label(label)
        if not text:
            # A picture changed but the source gave us nothing to call
            # it. `Picture 12 of 47` fallback is computed on the
            # display side, so an empty label here means that failed too
            # -- and a blank rectangle appearing in the menu bar for
            # three seconds is worse than no feedback at all.
            return False
        self._label = text
        self._shown_until = moment + self._hold_s
        return True

    def active_label(self, now: float | None = None) -> str:
        """The label if its hold has not expired, otherwise "".

        Expiry clears the stored label as a side effect so that a later
        `label` read cannot resurrect it.
        """
        moment = time.time() if now is None else now
        if self._label and moment < self._shown_until:
            return self._label
        self._label = ""
        return ""


def title_for(
    state: State,
    transient_label: str = "",
) -> str:
    """What goes beside the icon, given both candidates.

    An actionable state wins over the transient label. The states in that
    set are conditions the user has to *do something about*; a picture
    name is a courtesy. Losing three seconds of courtesy to keep
    `View not connected` on screen is the right trade, and the reverse
    would hide the one string that explains why Next appeared to do
    nothing.
    """
    if state.is_actionable:
        return state.title
    return transient_label


# -- command construction -----------------------------------------
#
# These take and return plain dicts rather than `control.ControlState`,
# so that this module stays importable and testable with no dependency on
# the display package's flat-module import layout. `menubar.py` converts
# at the boundary, which is also the only place that knows where the file
# lives.


def _current(command: Mapping[str, Any] | None) -> dict[str, Any]:
    base: dict[str, Any] = {
        "blanked": None,
        "paused": False,
        "paused_on_id": None,
        "advance": 0,
        "refresh": 0,
        "preview_calibration": None,
    }
    if isinstance(command, Mapping):
        for key in base:
            if key in command:
                base[key] = command[key]
    return base


def command_set_blanked(
    command: Mapping[str, Any] | None, blanked: bool
) -> dict[str, Any]:
    """Blank or un-blank. Desired state, so writing it twice is
    writing it once.

    Sets an explicit `True`/`False`, never `None`. `None` means "follow
    the schedule" (`blank_manual`), and there is no schedule to
    follow: the split-state model and the surface
    that configures it ship together or neither does. Writing `None` here
    would be indistinguishable from "un-blanked" today and would mean
    something different the moment the settings UI lands.
    """
    state = _current(command)
    state["blanked"] = bool(blanked)
    return state


def command_set_paused(
    command: Mapping[str, Any] | None,
    paused: bool,
    paused_on_id: str | None = None,
) -> dict[str, Any]:
    """Pause on the current picture, or resume.

    Pausing records *which* picture it is pinned to, because that is what
    Pause means. Resuming clears the pin: leaving a stale id behind would
    make the next Pause re-pin to a picture that scrolled by minutes ago.
    Blanking is untouched in both directions: pausing a
    blanked View is coherent and must not un-blank it.
    """
    state = _current(command)
    state["paused"] = bool(paused)
    state["paused_on_id"] = paused_on_id if paused else None
    return state


def command_advance(
    command: Mapping[str, Any] | None, steps: int = 1
) -> dict[str, Any]:
    """Next (`steps=1`) or Previous (`steps=-1`) — counter.

    Increments rather than setting, which is the whole mechanism: two
    clicks inside one 250ms poll window move the counter by two and the
    display takes two steps, even though it only ever observed the
    endpoint.

    Un-blanks, because rule is "any action un-blanks, except
    Pause" — a Next that silently advanced a picture nobody can see is
    the class of silent no-op that rule exists to eliminate. Pause is
    deliberately *not* cleared: Next and Previous while paused
    move the pause to the next picture and never resume rotation. Only
    `Resume rotation` does that.
    """
    state = _current(command)
    state["advance"] = _as_int(state.get("advance")) + int(steps)
    if state.get("blanked"):
        state["blanked"] = False
    return state


def command_refresh(command: Mapping[str, Any] | None) -> dict[str, Any]:
    """`Check for new pictures now` — second counter.

    A counter rather than a flag, for the reason `ControlState.refresh`
    documents: a flag cannot distinguish "asked again" from "still
    asking", so it would either re-poll forever or fire once and never
    again after a UI restart. Incrementing inherits `advance`'s
    replay-safety and its `!=` comparison for free.

    Un-blanks, like `command_advance` and for the same reason: a
    refresh whose results nobody can see is a silent no-op. Pause is
    left alone — new pictures arriving is not a reason to move off the
    one the user pinned.
    """
    state = _current(command)
    state["refresh"] = _as_int(state.get("refresh")) + 1
    if state.get("blanked"):
        state["blanked"] = False
    return state


#: Who made this. Kept here rather than in `menubar.py` so the About box
#: is composed by a pure function and can be asserted in tests, like every
#: other piece of user-facing text in this app.
AUTHORS = ("Techno", "Opus")

APP_NAME = "ImageView"

ABOUT_SUMMARY = (
    "Shows a rotating, circular-masked slideshow on a Napster View, "
    "instead of the app it ships with."
)

#: The trademark line. This app drives someone else's hardware and says so
#: everywhere else it is described; an About box that omitted it would be
#: the one place the claim went missing.
ABOUT_DISCLAIMER = (
    "Not affiliated with, endorsed by, or connected to Napster or its "
    "hardware partners. \u201cNapster\u201d and \u201cNapster View\u201d are "
    "the trademarks of their respective owners."
)


def about_text(version: object) -> tuple[str, str]:
    """`(title, body)` for the About box.

    🔴 `version` is passed in, never hardcoded here. It comes from the
    running bundle's `Info.plist`, so the About box cannot drift from the
    thing the user actually installed — which is the same failure the
    release gate's version-agreement check exists to catch, and it would
    be absurd to reintroduce it in the one window whose entire job is to
    state the version.

    Degrades rather than raising: an unreadable version shows as unknown,
    because an About box is never worth a crash.
    """
    if isinstance(version, str) and version.strip():
        shown = version.strip()
    else:
        shown = "unknown version"
    title = f"{APP_NAME} {shown}"
    body = "\n\n".join(
        (
            ABOUT_SUMMARY,
            "By " + " and ".join(AUTHORS) + ".",
            ABOUT_DISCLAIMER,
            "MIT licensed.",
        )
    )
    return title, body


__all__ = [
    "ABOUT_DISCLAIMER",
    "ABOUT_SUMMARY",
    "APP_NAME",
    "AUTHORS",
    "about_text",
    "ELLIPSIS",
    "LABEL_MAX_CHARS",
    "STALE_AFTER_S",
    "TITLE_HOLD_S",
    "UI_HEARTBEAT_INTERVAL_S",
    "UI_UNRESPONSIVE_AFTER_S",
    "State",
    "Status",
    "TitleTracker",
    "UiStatus",
    "command_advance",
    "command_refresh",
    "command_set_blanked",
    "command_set_paused",
    "evaluate_state",
    "is_stale",
    "parse_status",
    "read_status",
    "read_ui_heartbeat",
    "read_ui_status",
    "setup_needed",
    "title_for",
    "truncate_label",
    "write_ui_heartbeat",
]
