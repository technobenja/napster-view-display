"""What a *losing* launch is entitled to say about the winner.

When ImageView is double-clicked while a copy is already running, the
second process loses `ui.lock` and exits 0. That was silent by design and
it is how "the app will not launch" happened on 2026-09-08: the holder
was wedged, so there was no menu bar icon, no dialog, and no trace — the
one state in which the silence is exactly wrong.

This module is the part of the answer that can be wrong in an interesting
way, so it is the part that is separable and tested. Nothing here imports
AppKit, `ctypes`, Quartz or the filesystem; everything is a pure function
of the observations plus a clock reading. `ui/contention.py` is the shell
that gathers them.

**Every verdict is a statement plus an instruction, never an action.**
The plan that produced this deleted its own remedy: an About box was
MEASURED on 2026-09-10 to freeze `ui_heartbeat_at` for **51.2 seconds**,
so "the stamp is stale" is a strict superset of "the process is wedged".
Nothing downstream of this may kill, quit, or take over anything. The
single exception is SIGUSR1, which is a *read* — and it is fenced by a
precondition in `contention.py` because SIGUSR1's default disposition is
terminate.

🔵 **The measurement stands; the example has expired.** That sentence
used to end "and includes a user reading the version number", which was
true until Phase 5 (v1.1.6) made the About box a non-modal panel. The
superset still holds, and the windows that still produce it are an open
menu and the alerts in Settings and *Adjust the circle* — those two
windows are on screen when their alerts are, which is exactly why the
phase left them alone. **Anyone re-running that measurement through the
About box will find no freeze and must not conclude the premise broke.**

🔴 **THE STANDING RULE, and it generalises past the bug that produced
it: when the evidence is a SUPERSET of the fault, fail toward the message
that does not accuse the app.** Every "I could not tell" in here resolves
to the benign reading. The alternative was measured, not imagined: see
`STATUS_ITEM_LAYER` for the one-constant version of this mistake, which
would have told an owner to force-quit ImageView because they opened the
About box. (It would still do it, and now for a Settings alert instead —
the constant's job did not change when the About box stopped being a
modal, because `layer != 25` is a statement about *windows*, and the
panel the About box became is a window too.)

**The observations, and why each is here.**

1. **Identity** — the holder pid's executable path, and the *display*
   pid's. A pid proves only that something exists; pids are recycled, and
   `read_holder_pid` was annotated "never used for anything but a
   message" precisely because nothing checked. Without this, a recycled
   pid belonging to some unrelated process reads as a live, wedged
   ImageView and the notice tells a user to force-quit it.

2. **Presence** — what the window server says the holder has on screen.
   This is the observation no in-process signal can make, and it is what
   separates the two readings of "no icon appeared" that could not be
   separated on 2026-09-08: never created, or created and hidden. It is
   also what keeps the About-box case benign — a modal panel is a window,
   so a frozen-but-visible app is told to bring its window forward.

3. **Muteness** — whether `~/.viewlab/state/` can be written at all.
   Without it, the one case that is *certainly* a healthy process — one
   that cannot write its heartbeat file — reads identically to a dead
   one. The lock lives in `~/.viewlab/` and the heartbeat in
   `~/.viewlab/state/`, different directories deliberately, so an
   unwritable `state/` leaves the lock perfectly takeable and the stamp
   permanently stale.

4. **Liveness** — the age of `ui_heartbeat_at`, at
   `menubar_state.UI_UNRESPONSIVE_AFTER_S`, never at `STALE_AFTER_S`.
   See that constant: 5.0 is tuned for a menu title and has 0.2s of
   slack, and the threshold behind a user-visible accusation must not be
   the one tuned for a label.

**Precedence.** Identity first — every later row makes a claim *about the
holder*, and none of them may be made about a process this app cannot
identify. Then liveness, because a fresh stamp ends the question. Then
muteness, because it is the row that would otherwise read a healthy
process as dead. Only then the window server, which is what distinguishes
the rest.

**Eight verdicts, seven messages.** The classifier keeps `UNRESPONSIVE` and
`NEVER_ARRIVED` apart because the difference is worth everything to a
diagnostician reading `tools/status.py`; the person at the screen reads
one message, because the two states are identical in what they ask them
to do. `SERVING_VISIBLE` and `SERVING_HIDDEN` go the other way: one state
to a diagnostician, two genuinely different instructions to a human.
"""

from __future__ import annotations

import dataclasses
import enum
import os
from collections.abc import Mapping, Sequence

from ui import menubar_state as ms

#: `kCGWindowLayer` for an `NSStatusItem`.
#:
#: 🔴 **The test below is `layer != STATUS_ITEM_LAYER`, and it must never
#: become `layer == 0`.** The plan this phase implements says "layer 25 =
#: status item, layer 0 = a panel". The first half is right; the second
#: is wrong, and wrong in the direction that re-creates the accusation
#: the plan exists to prevent.
#:
#: MEASURED on this machine, 2026-09-10, from AppKit itself —
#: `kCGWindowLayer` is the `NSWindow` level, confirmed live against the
#: running app (the display agent reports 1000, matching its
#: `setLevel_(NSScreenSaverWindowLevel)`; the status items report 25):
#:
#:     NSNormalWindowLevel         0
#:     NSFloatingWindowLevel       3
#:     NSModalPanelWindowLevel     8   <- every NSAlert.runModal()
#:     NSMainMenuWindowLevel      24
#:     NSStatusWindowLevel        25
#:     NSPopUpMenuWindowLevel    101   <- an open NSMenu
#:     NSScreenSaverWindowLevel 1000   <- display/window.py
#:                              1001   <- calibrate_window, identify
#:
#: So `layer == 0` would have missed **the About box — the exact 51.2s
#: freeze M1 measured, when the About box was still an `NSAlert`** — and
#: with it every other modal in `menubar.py`, the calibration overlay and
#: the Identify flash, sending all of them to the "not responding, force
#: quit pid N" branch. Treating anything that is not a status item as a
#: window costs nothing in the other direction: the worst case is telling
#: someone to look at a window that turns out to be a menu.
#:
#: 🔵 **v1.1.6 moved windows between rows of that table and changed
#: nothing here, which is the point of writing the rule as `!= 25`.**
#: `menubar.py` and `first_run_window.py` now run no modal at all, so
#: their windows are a level-3 `NSPanel` and a sheet rather than level 8
#: — and both are still caught, because both are still not status items.
#: A `layer == 8` rule keyed to "modals freeze the stamp" would have
#: silently stopped seeing them.
STATUS_ITEM_LAYER = 25

#: Basename of the shipped executable inside `ImageView.app`.
BUNDLE_EXECUTABLE_NAME = "ImageView"

#: The bundle path fragment an ImageView executable sits behind.
BUNDLE_PATH_FRAGMENT = f"{BUNDLE_EXECUTABLE_NAME}.app/Contents/MacOS/"


@dataclasses.dataclass(frozen=True)
class WindowFact:
    """One entry from the window server, reduced to the fields that are
    readable without a Screen Recording grant.

    🔴 **`kCGWindowName` is deliberately absent and must stay absent.**
    MEASURED on this machine, 2026-09-10: of 40 on-screen windows, 6 had
    a readable name. Without Screen Recording the rest are redacted, and
    a classifier keyed on names would behave differently depending on a
    TCC grant the app neither has nor asks for. `kCGWindowOwnerPID`,
    `kCGWindowLayer`, `kCGWindowBounds` and `kCGWindowIsOnscreen` need no
    permission at all — measured in the same call.

    ⚠️ **`on_screen` is the window server's word, not a claim that a
    human can see it.** MEASURED: the live menu bar owns a status item on
    *every* attached screen, and the copy sitting on the Napster View
    reports `kCGWindowIsOnscreen: True` while lying underneath a 960x960
    layer-1000 window that covers the whole panel. `visible_status_items`
    is what turns this field into a statement about a person.
    """

    layer: int
    on_screen: bool
    x: float
    y: float
    width: float
    height: float

    def where(self) -> str:
        return f"{self.width:.0f}x{self.height:.0f} at {self.x:.0f}, {self.y:.0f}"

    def center(self) -> tuple[float, float]:
        return (self.x + self.width / 2.0, self.y + self.height / 2.0)

    def contains(self, other: WindowFact) -> bool:
        """Whether `other`'s centre lies inside this window's bounds.

        Centre containment rather than full overlap: a status item is
        tiny and a picture-display window fills its whole screen, so the
        item is either wholly on that screen or wholly elsewhere, and a
        centre test says which without needing to reason about edges.
        Verified against a real two-screen arrangement: the status item
        sharing a screen with the picture window is contained by it, and
        the one on the menu bar screen is not.
        """
        cx, cy = other.center()
        return (
            self.x <= cx <= self.x + self.width
            and self.y <= cy <= self.y + self.height
        )


@dataclasses.dataclass(frozen=True)
class Facts:
    """The observations, plus what is needed to phrase them.

    `windows` and `display_windows` are `None` for **"the window server
    could not be asked"**, and an empty tuple for **"it was asked and
    that process owns nothing on screen"**. Those are different answers
    and this project has paid for conflating them before: a
    `screencapture` that cannot see windows exits 0 and returns bare
    wallpaper, which reads exactly like an app that is not drawing.
    `classify` never returns `NEVER_ARRIVED` on a `None` — that verdict
    asserts something was looked for and was not there.

    `display_pid` is the *other* half of the app, read from
    `display.lock` and verified by the same executable-path check. It is
    `None` whenever it could not be verified, and is then omitted from
    every message rather than printed as an unbacked number: the whole
    reason it is here is that both processes are called `ImageView` in
    Activity Monitor, and an unverified pid in a force-quit instruction
    is worse than no pid at all.

    ⚠️ **The menu bar's pid can perfectly well be the HIGHER of the two**
    — observed, and it is the reverse of the intuitive reading, since the
    display agent starts at login and the menu bar may be launched later
    (or restarted on its own). Nothing here may order, compare or infer
    from pid magnitude, start order, or anything at all except the lock
    file that named the pid and what `proc_pidpath` says it is.
    """

    lock_path: str
    lock_pid: int | None
    holder_exe: str | None
    #: When the holder started, `(seconds, microseconds)`, or None.
    #: Signal-path only: it is the identity that survives pid reuse,
    #: which an executable path does not. See `contention.capture_stacks`.
    holder_started_at: tuple[int, int] | None
    our_exe: str | None
    display_pid: int | None
    heartbeat_at: float
    #: Whether `ui_status.json` exists and parsed at all — **not**
    #: whether the stamp in it is fresh. 🔴 Absent is not stale. A build
    #: older than v1.1.5 writes no such file *ever*, and this app
    #: upgrades by dragging a new bundle over a running old one, so the
    #: very first contended launch after an upgrade meets a holder that
    #: has never written one. Without this field that holder reads as
    #: "not responding, force quit pid N" — the accusation this whole
    #: phase exists to prevent, aimed at every user on upgrade day.
    #: FOUND by running the classifier against the live machine's own
    #: v1.1.4 menu bar, which is healthy and drew exactly that verdict.
    heartbeat_present: bool
    heartbeat_pid: int | None
    stacks_armed: bool
    stacks_path: str | None
    state_dir: str
    state_writable: bool
    state_detail: str | None
    windows: tuple[WindowFact, ...] | None
    display_windows: tuple[WindowFact, ...] | None
    now: float

    def age_s(self) -> float | None:
        """Seconds since the holder last serviced a default-mode timer,
        or None if it never wrote a readable stamp. A stamp in the future
        reports 0.0 rather than a negative number — clocks step
        backwards, and `is_stale` already treats a future stamp as fresh
        for the same reason."""
        if self.heartbeat_at <= 0.0:
            return None
        return max(0.0, self.now - self.heartbeat_at)

    def age_text(self) -> str:
        """A verb phrase, not a sentence: every caller supplies the
        subject, and an embedded "it" made three of the messages read as
        two clauses jammed together."""
        age = self.age_s()
        if age is None:
            return "has never reported working"
        return f"last reported working {age:.0f}s ago"

    def layers_seen(self) -> tuple[int, ...] | None:
        """The holder's on-screen window layers, or None if the window
        server could not be asked. Part of the structured payload so that
        a reader can second-guess the verdict without re-querying."""
        if self.windows is None:
            return None
        return tuple(sorted(w.layer for w in self.windows if w.on_screen))


class Verdict(enum.Enum):
    """The eight readings, in precedence order.

    Reordering these members is not cosmetic — `classify` is written to
    match, and `CAPTURE_STACKS_FOR` names two of them by identity.
    """

    #: Fresh stamp, and a status item somewhere a person can see it.
    SERVING_VISIBLE = "serving_visible"
    #: Fresh stamp, but no status item on a screen a person is looking
    #: at — either none at all, or only one parked on the picture
    #: display. A crowded menu bar drops icons silently.
    SERVING_HIDDEN = "serving_hidden"
    #: The lock is held by something this app cannot confirm is ImageView
    #: — absent pid, foreign executable, a pid that disagrees with
    #: `ui_status.json`, or a display pid equal to the holder's, which is
    #: impossible. Reported; nothing assumed, nothing signalled.
    UNIDENTIFIED = "unidentified"
    #: Stale, because the holder cannot write `~/.viewlab/state/` at all.
    #: It may be perfectly healthy, and it recovers on its own.
    MUTED = "muted"
    #: Stale, and the holder has a window on screen. Almost certainly a
    #: modal dialog waiting for an answer — a Settings alert, or *Adjust
    #: the circle* asking whether to save; measured at 51.2s around an
    #: About box, back when that was modal too. Look at your screen.
    WINDOW_OPEN = "window_open"
    #: Stale, its icon is on screen, and nothing else is. Also the answer
    #: when the window server could not be consulted, in which case the
    #: message makes no claim about windows.
    UNRESPONSIVE = "unresponsive"
    #: There is no `ui_status.json` at all, and nothing rules out a
    #: healthy holder — it owns something on screen, or the window server
    #: could not be asked. Almost certainly a build older than v1.1.5,
    #: which never wrote one — and possibly a current build wedged in the
    #: narrow window between creating its status item and its first timer
    #: tick. Nothing can tell those apart from outside, so the message
    #: names both and accuses neither.
    NEVER_REPORTED = "never_reported"
    #: Stale and the holder owns nothing on screen at all — it started
    #: and never reached the menu bar. One of the two readings of "no
    #: icon appeared" that nobody could separate on 2026-09-08. The user
    #: is told the same thing as `UNRESPONSIVE`; this distinction is for
    #: whoever reads `tools/status.py` afterwards.
    NEVER_ARRIVED = "never_arrived"


#: The verdicts for which capturing the holder's stacks is worth a
#: signal. Not `WINDOW_OPEN` — a visible dialog explains itself and the
#: stack would say `runModal`, which is already known. Not `MUTED` — that
#: process is probably fine. Not `UNIDENTIFIED` — signalling something
#: this app cannot identify is the one thing it must never do.
CAPTURE_STACKS_FOR = frozenset({Verdict.UNRESPONSIVE, Verdict.NEVER_ARRIVED})

#: The verdicts that mean "an instance is serving", for callers that do
#: not care which of the two.
SERVING = frozenset({Verdict.SERVING_VISIBLE, Verdict.SERVING_HIDDEN})


@dataclasses.dataclass(frozen=True)
class Capture:
    """What, if anything, came of asking the holder for its stacks."""

    #: True when the verdict called for a capture at all.
    wanted: bool = False
    #: True when SIGUSR1 was actually delivered.
    signalled: bool = False
    #: True when the stacks file grew afterwards.
    grew: bool = False
    #: Where it was written, when it was.
    path: str | None = None
    #: Why not, in a sentence a user can read. Set whenever `wanted` is
    #: True and `grew` is False.
    refused: str | None = None

    def sentence(self) -> str:
        if not self.wanted:
            return ""
        if self.grew and self.path:
            return f"Its thread stacks were captured to {self.path}."
        if self.signalled and self.path:
            return (
                f"Its thread stacks were requested; {self.path} did not grow "
                f"within the time allowed, which is itself consistent with a "
                f"process that cannot run any code."
            )
        return f"Its thread stacks could not be captured: {self.refused or 'unknown'}."


@dataclasses.dataclass(frozen=True)
class Notice:
    """One verdict, as a structured value.

    `headline` and `body` are the words; everything else is the evidence
    behind them, carried alongside rather than re-derived. That is the
    4a/4b seam: Phase 4b renders an `NSPanel` from these fields and
    decides nothing, and `tools/status.py` prints the same verdict
    without reimplementing a line of it.

    🔴 **A RULE PHASE 4B INHERITS, decided here so it is not
    rediscovered there.** `body` and `headline` contain strings read off
    disk — `exe` and `stacks_path` come out of `ui_status.json`, and
    `lock_path` out of the filesystem. Render them into a plain
    `NSTextField` with `setStringValue_`, and **never** through
    `NSAttributedString`'s HTML initializer
    (`initWithHTML:documentAttributes:`), never through a format string,
    never through anything that interprets markup. The HTML initializer
    in particular runs a full parser and can load remote resources; it is
    not a text renderer and must not be reached for because a message
    would look nicer with a bold pid. Nothing in these fields is
    attacker-controlled today — the same user wrote them — but "the
    document that describes the broken process" is the last place to
    assume the content is well formed.

    🔴 **A SECOND RULE, ADDED 2026-09-10 WHEN 4B WENT ON SCREEN: no
    message may name a screen coordinate.** `SERVING_VISIBLE` used to
    interpolate the icon's size and position into *"Its menu bar icon is
    on screen (...) — click it"*, and `WINDOW_OPEN` did the same with its
    window's bounds. Honest, and useless: nobody turns a coordinate into
    a place to look.

    The fix could not be "say *directly above this window*" here, and the
    reason is the whole shape of this seam — **these words have two
    consumers.** One is Phase 4b's panel, where that sentence is true. The
    other is `<role>.stderr.log` and `tools/status.py`, where there is no
    window at all and the sentence would be a lie. A wording true in one
    is false in the other, so the only wording this layer may use is one
    true in **both**: the icon is in the menu bar, click it.

    The spatial pointer belongs to whoever actually placed a window.
    `notice_state.pointer_sentence` owns it, returns it **only** when the
    panel really anchored under the icon unclamped, and the shell appends
    it as its own line. This module knows the verdict and cannot know
    where a window landed; that module knows the geometry and must not
    invent verdicts.

    `self_heals` is True when the condition clears on its own and no
    restart is called for. It is a field rather than a phrase buried in
    `body` so that a presentation layer can rely on it — today only
    `MUTED` sets it, and it is true for a specific, checkable reason:
    `menubar._write_ui_heartbeat` never latches its failure. It retries
    on every throttled tick and logs the transition back, so the moment
    the folder becomes writable the stamp resumes with no intervention.
    """

    verdict: Verdict
    headline: str
    body: str
    holder_pid: int | None = None
    display_pid: int | None = None
    heartbeat_age_s: float | None = None
    window_layers: tuple[int, ...] | None = None
    state_writable: bool = True
    stacks_path: str | None = None
    self_heals: bool = False

    def log_line(self) -> str:
        return f"menubar: contended launch — {self.verdict.value}: {self.body}"


def is_ours(holder_exe: str | None, our_exe: str | None) -> bool:
    """Whether `holder_exe` is an executable this app recognises as its
    own. False for None, for an empty string, and for anything else.

    Two acceptances, and the first is the one that matters in
    development:

    * **the same executable we are running.** Compared against our own
      `proc_pidpath()`, never `sys.executable`. MEASURED on this machine:
      a process started as `display/.venv/bin/python3` reports
      `/opt/homebrew/.../Python.app/Contents/MacOS/Python` — the venv
      launcher is not what the kernel records. Comparing against
      `sys.executable` would reject our own twin in every source-tree
      run, which is every run a developer makes.

    * **any `ImageView.app/Contents/MacOS/ImageView`.** So a source-tree
      launch can still identify an installed bundle as the holder, which
      is the realistic mixed case on a development machine.

    Deliberately a string comparison with no `realpath`: resolving a path
    is a filesystem call, and this module does not touch the filesystem.
    The cost is that a holder reached through a symlinked bundle reads as
    unidentified — the safe direction, since `UNIDENTIFIED` asserts
    nothing and signals nothing.
    """
    if not holder_exe:
        return False
    if our_exe and holder_exe == our_exe:
        return True
    return (
        os.path.basename(holder_exe) == BUNDLE_EXECUTABLE_NAME
        and BUNDLE_PATH_FRAGMENT in holder_exe
    )


def status_items(windows: Sequence[WindowFact] | None) -> tuple[WindowFact, ...]:
    """Every on-screen status-item window the holder owns, on any
    screen. See `visible_status_items` for the ones a person can see."""
    if windows is None:
        return ()
    return tuple(w for w in windows if w.on_screen and w.layer == STATUS_ITEM_LAYER)


def visible_status_items(
    windows: Sequence[WindowFact] | None,
    display_windows: Sequence[WindowFact] | None = None,
) -> tuple[WindowFact, ...]:
    """The status items a person could actually look at.

    macOS gives a status item a window on **every** attached screen, and
    this app's second screen is a 2.1-inch round panel with a full-screen
    picture on it. MEASURED on the live machine: the menu bar owns two
    layer-25 windows, one at (1169, 0) on the menu bar and one at
    (842, -960) sitting *underneath* the display agent's 960x960
    layer-1000 window — and the buried one reports
    `kCGWindowIsOnscreen: True` exactly like the other.

    So a count of status items is not an answer to "can the user see the
    icon", and `SERVING_HIDDEN` would never fire without this. Any status
    item whose centre falls inside a display-agent window is dropped.

    `display_windows` of `None` (or empty) means the display agent's
    windows could not be listed, and then nothing is dropped — a missed
    exclusion says "the icon is there, click it", which is the benign
    reading.
    """
    items = status_items(windows)
    if not display_windows:
        return items
    return tuple(
        item
        for item in items
        if not any(
            target.contains(item) for target in display_windows if target.on_screen
        )
    )


def visible_windows(windows: Sequence[WindowFact] | None) -> tuple[WindowFact, ...]:
    """The holder's on-screen windows that are *not* status items.

    🔴 `layer != STATUS_ITEM_LAYER`, never `layer == 0`, and never a
    test for the modal level either. See `STATUS_ITEM_LAYER` for the
    measured table, for what the `== 0` version would have done to
    someone who opened the About box, and for why v1.1.6 moving three
    windows off the modal level did not disturb this.
    """
    if windows is None:
        return ()
    return tuple(w for w in windows if w.on_screen and w.layer != STATUS_ITEM_LAYER)


def unidentified_reason(facts: Facts) -> str | None:
    """Why the holder cannot be identified, or None when it can.

    Split out from `classify` because the message has to name which of
    the five it was — "something holds the lock" with no reason is the
    kind of line that sends the next reader to the wrong process.
    """
    if facts.lock_pid is None or facts.lock_pid <= 0:
        # 🔴 `<= 0` is not tidiness. `kill(0, sig)` signals every process
        # in the caller's process group and `kill(-1, sig)` every process
        # the user may signal — which on this machine includes the
        # picture display. A lock file containing `-1` is not reachable
        # from `gather()` today (a non-positive pid fails `proc_pidpath`,
        # so `holder_exe` is None and the next clause fires), but that
        # safety is emergent in the *caller*, and `Facts` is a public
        # type with two new consumers. Make it local to the check.
        return f"{facts.lock_path} records no usable pid"
    if facts.holder_exe is None:
        return (
            f"pid {facts.lock_pid} does not exist, so the lock is held by a "
            f"process this app cannot look at"
        )
    if not is_ours(facts.holder_exe, facts.our_exe):
        return f"pid {facts.lock_pid} is {facts.holder_exe}, which is not ImageView"
    if facts.heartbeat_pid is not None and facts.heartbeat_pid != facts.lock_pid:
        return (
            f"the lock names pid {facts.lock_pid} but the heartbeat file names "
            f"pid {facts.heartbeat_pid}; they disagree"
        )
    if facts.display_pid is not None and facts.display_pid == facts.lock_pid:
        # Impossible: the two locks are different files guarding different
        # roles, precisely so that force-quitting one half leaves the
        # other running. One pid holding both means something this app's
        # model does not describe — and the one message that must never
        # be produced here is "leave the other one alone", because on
        # this evidence there is no other one.
        return (
            f"pid {facts.lock_pid} appears to hold both the menu bar and the "
            f"display lock, which this app cannot produce"
        )
    return None


def classify(facts: Facts) -> Verdict:
    """The eight verdicts, from the observations and nothing else.

    Pure, total, and never raises. Precedence is the module docstring's,
    and each step is here rather than in a message template so that the
    truth table is one function long.
    """
    if unidentified_reason(facts) is not None:
        return Verdict.UNIDENTIFIED
    if not ms.is_stale(facts.heartbeat_at, facts.now, ms.UI_UNRESPONSIVE_AFTER_S):
        if facts.windows is None:
            # Could not look. "Its icon is where icons are" is the benign
            # reading and the only one the evidence supports.
            return Verdict.SERVING_VISIBLE
        if visible_status_items(facts.windows, facts.display_windows):
            return Verdict.SERVING_VISIBLE
        return Verdict.SERVING_HIDDEN
    if not facts.state_writable:
        return Verdict.MUTED
    if visible_windows(facts.windows):
        return Verdict.WINDOW_OPEN
    if facts.windows is None:
        # The window server could not be asked. "Not responding" is still
        # true — the stamp is the evidence for it — but every verdict
        # below this line is a claim about windows, and "could not look"
        # is not "not found".
        return (
            Verdict.UNRESPONSIVE if facts.heartbeat_present else Verdict.NEVER_REPORTED
        )
    if status_items(facts.windows):
        # An icon on screen and **no heartbeat file whatsoever** is what
        # every pre-v1.1.5 build looks like, healthy. An icon on screen
        # and a file whose stamp has gone cold is a build that was
        # reporting and stopped. Only the second is evidence of anything.
        return (
            Verdict.UNRESPONSIVE if facts.heartbeat_present else Verdict.NEVER_REPORTED
        )
    # Nothing on screen at all, having actually looked. This is the
    # 2026-09-08 shape, and it reads the same whatever the holder's
    # vintage: a healthy older build still puts an icon in the menu bar.
    return Verdict.NEVER_ARRIVED


def _sentences(*parts: str) -> str:
    """Join the parts that have something to say, with one space.

    The optional parts — a capture result, a force-quit instruction — are
    empty strings when they do not apply, and an f-string that
    interpolates them directly leaves double spaces and trailing
    whitespace in a user-visible message.
    """
    return " ".join(part.strip() for part in parts if part.strip())


def _force_quit_instruction(facts: Facts) -> str:
    """🔴 Every instruction to force quit names the pid.

    Both halves of this app are called `ImageView` in Activity Monitor,
    and the other one is the process drawing pictures on the View. A
    README cannot know which is which; this can, because both pids were
    verified by executable path before any of these words were chosen.

    The display pid is named **only when it was verified**. An unverified
    number in a "leave this one alone" instruction is worse than no
    number: it is a specific, confident pointer at whatever happens to be
    running under that id.
    """
    pid = facts.lock_pid
    if pid is None:
        return ""
    if facts.display_pid is not None:
        return (
            f"If you want it gone, force quit pid {pid} specifically. Activity "
            f"Monitor lists both halves of ImageView under the same name, and "
            f"pid {facts.display_pid} is the one drawing your pictures — leave "
            f"that one running."
        )
    return (
        f"If you want it gone, force quit pid {pid} specifically: Activity "
        f"Monitor lists both halves of ImageView under the same name, and the "
        f"other one is the process drawing your pictures."
    )


def notice(facts: Facts, verdict: Verdict, capture: Capture | None = None) -> Notice:
    """Render one verdict into a structured `Notice`. Pure; never raises.

    Interpolates only `int`, `float` and `str` values that the `Facts`
    record has already normalised, so no f-string here can fail on a
    hostile value read off disk.
    """
    capture = capture or Capture()
    pid = facts.lock_pid
    pid_text = f"pid {pid}" if pid is not None else "an unknown pid"

    def built(headline: str, body: str, *, self_heals: bool = False) -> Notice:
        return Notice(
            verdict=verdict,
            headline=headline,
            body=body,
            holder_pid=pid,
            display_pid=facts.display_pid,
            heartbeat_age_s=facts.age_s(),
            window_layers=facts.layers_seen(),
            state_writable=facts.state_writable,
            stacks_path=capture.path if capture.grew else None,
            self_heals=self_heals,
        )

    if verdict is Verdict.UNIDENTIFIED:
        reason = unidentified_reason(facts) or "it could not be identified"
        return built(
            "Something else holds ImageView's lock",
            f"ImageView did not start a second copy, because {facts.lock_path} is "
            f"already held — but {reason}. Nothing has been signalled and nothing "
            f"assumed. If ImageView is not running, that lock file is safe to "
            f"delete once you are sure no ImageView process is alive.",
        )

    if verdict is Verdict.SERVING_VISIBLE:
        items = visible_status_items(facts.windows, facts.display_windows)
        if items:
            tail = "Its menu bar icon is on screen — click it."
        else:
            tail = (
                "Its menu bar icon should be at the right-hand end of the menu "
                "bar — click it."
            )
        return built(
            "ImageView is already running",
            f"ImageView is already running ({pid_text}) and responding. {tail} "
            f"This second copy is exiting; nothing else is needed.",
        )

    if verdict is Verdict.SERVING_HIDDEN:
        parked = len(status_items(facts.windows)) - len(
            visible_status_items(facts.windows, facts.display_windows)
        )
        why = (
            "its only icon is on the picture display, underneath the picture"
            if parked > 0
            else "it has no menu bar icon on screen"
        )
        return built(
            "ImageView is running, but its icon is hidden",
            f"ImageView is already running ({pid_text}) and responding, but "
            f"{why}. A crowded menu bar hides icons that do not fit, and a Mac "
            f"with a notch drops them silently. Make room by quitting another "
            f"app that has a menu bar icon, or by using a menu bar manager.",
        )

    if verdict is Verdict.MUTED:
        detail = f" ({facts.state_detail})" if facts.state_detail else ""
        return built(
            "ImageView cannot write its state folder",
            f"ImageView is running ({pid_text}), but it cannot write "
            f"{facts.state_dir}{detail} — so it cannot report whether it is "
            f"working, and it {facts.age_text()}. It may be perfectly healthy, "
            f"and it is still showing pictures. Free up disk space, or fix that "
            f"folder's permissions, depending on the reason above: ImageView "
            f"retries every couple of seconds and starts reporting again on its "
            f"own, so nothing needs restarting.",
            self_heals=True,
        )

    if verdict is Verdict.WINDOW_OPEN:
        return built(
            "An ImageView window is already open",
            f"ImageView ({pid_text}) has a window on screen and "
            f"{facts.age_text()} — which is what an open dialog does to it. "
            f"Look at your screen and bring that window forward; it may be "
            f"behind another one.",
        )

    if verdict is Verdict.NEVER_REPORTED:
        return built(
            "ImageView is already running",
            f"ImageView is already running ({pid_text}), and it has never "
            f"reported whether it is working — which is simply what versions "
            f"before 1.1.5 do, so this is most likely an older copy that is "
            f"perfectly fine. Click its menu bar icon: if the menu opens, "
            f"nothing is wrong and updating ImageView will make this message "
            f"go away. If the menu does not open, it is stuck: force quit "
            f"pid {pid} specifically, because Activity Monitor lists both "
            f"halves of ImageView under the same name.",
        )

    # UNRESPONSIVE and NEVER_ARRIVED deliberately share one message.
    # They differ in diagnosis — an icon that is there and dead, versus a
    # process that never reached the menu bar — and that difference is
    # worth everything to whoever reads `tools/status.py` afterwards and
    # nothing at all to the person at the screen, who is told the same
    # thing either way. The distinction lives in `verdict`, which this
    # `Notice` carries.
    if facts.windows is None:
        seen = (
            "The window server could not be consulted, so whether its icon is on "
            "screen is unknown."
        )
    elif status_items(facts.windows):
        seen = "Its menu bar icon is still there, but clicking it will do nothing."
    else:
        seen = "It has nothing on screen at all — not even a menu bar icon."
    return built(
        "ImageView is not responding",
        _sentences(
            f"ImageView ({pid_text}) {facts.age_text()} and is not responding.",
            seen,
            capture.sentence(),
            _force_quit_instruction(facts),
        ),
    )


def launched_by_agent(environ: Mapping[str, str], label: str) -> bool:
    """Whether this process was started by its own LaunchAgent.

    Only a **human** launch deserves a response. The UI LaunchAgent
    starts a menu bar at login; a user who then double-clicks
    `ImageView.app` is the case this whole phase exists for, and an agent
    respawn that loses the lock is routine housekeeping nobody should be
    told about — and must not cost the holder a SIGUSR1 on every login.

    The discriminator, as the plan measured it: a LaunchAgent-started
    process has `XPC_SERVICE_NAME` equal to its own label, while
    LaunchServices gives either no such variable or
    `application.<bundleid>.<n>.<n>`. **Exact match only** — a prefix or
    substring test would catch `application.<bundleid>...`, which is
    precisely the case that must not match.

    ⚠️ A third value exists that the plan does not mention, measured on
    this machine 2026-09-10: a process descended from a terminal session
    has `XPC_SERVICE_NAME=0`. It is not a problem — `0` is not the label,
    so exact matching answers False, which is the right answer for a
    hand-started run — but anyone re-measuring this should not be
    surprised by it, and an "is it set at all" test would have been wrong.

    The failure mode either way is benign and deliberately so: guessing
    "agent" for a human launch costs a notice nobody sees; guessing
    "human" for an agent respawn costs one notice at login. Neither is
    worth a more clever test.
    """
    return environ.get("XPC_SERVICE_NAME") == label


__all__ = [
    "BUNDLE_EXECUTABLE_NAME",
    "BUNDLE_PATH_FRAGMENT",
    "CAPTURE_STACKS_FOR",
    "SERVING",
    "STATUS_ITEM_LAYER",
    "Capture",
    "Facts",
    "Notice",
    "Verdict",
    "WindowFact",
    "classify",
    "is_ours",
    "launched_by_agent",
    "notice",
    "status_items",
    "unidentified_reason",
    "visible_status_items",
    "visible_windows",
]
