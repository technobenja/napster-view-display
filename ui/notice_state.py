"""Where the contended-launch notice goes, and what it offers to do.

`ui/contention_state.py` decides *what a losing launch may say*;
`ui/notice_window.py` is the `NSPanel` that says it. This module is the
part in between that can be wrong in an interesting way — the geometry —
so it is the part that is separable and tested. Nothing here imports
AppKit, Quartz or the filesystem.

**The placement is the message.** The plan's first draft had the notice
print the holder's icon coordinates: *"its icon is at (1169, 0)"*. That
is honest and useless — nobody converts a screen coordinate into a place
to look. Putting the panel **directly underneath the icon**, anchored to
its centre, says the same thing in a form that needs no spatial
reasoning, and it survives a user who has never heard of a coordinate
system.

🔴 **A panel must never be placed on a screen that is the picture
display.** MEASURED on the live machine, 2026-09-10: macOS gives a status
item a window on **every** attached screen, and one of this app's screens
is a 2.1-inch *round* panel. The copy sitting there reports
`kCGWindowIsOnscreen: True` while buried under a 960x960 layer-1000
window. Anchor naively and the notice lands on the View — under the
slideshow, with half of it outside the circular mask, on the one screen
nobody is sitting in front of. `contention_state.visible_status_items`
already drops the buried icon; `allowed_screens` here drops the screen
itself, from the same evidence, so neither is re-derived from geometry
this module invented.

**Coordinates.** Two systems meet here and they disagree about which way
is up. The window server answers in *Quartz* coordinates — origin at the
top-left of the zero-origin screen, y increasing **downward**. `NSWindow`
and `NSScreen` use *Cocoa* coordinates — origin at the bottom-left of the
same screen, y increasing **upward**. `cocoa_rect` is the only conversion,
and every `Rect` below it is Cocoa. MEASURED on this machine: the menu
bar's status item at Quartz `(1169, 0, 34x24)` against a 1000pt-tall
zero-origin screen is Cocoa `(1169, 976, 34x24)`, whose top edge is
exactly the top of the screen.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence

from ui import contention_state as cstate

#: Fixed panel width, in points. Wide enough for the longest verdict body
#: at 11pt without a scroll view, narrow enough to sit under a 34pt icon
#: near either end of a menu bar without being clamped into nonsense.
PANEL_WIDTH = 360.0

#: How far the panel's top edge sits below the menu bar. `visibleFrame`
#: already excludes the menu bar, so this is a gap below `visible.max_y`
#: and not an assumption about the menu bar's height.
MENU_BAR_GAP = 8.0

#: Kept clear of every edge of `visibleFrame`.
EDGE_MARGIN = 12.0

#: How long the one benign notice stays up.
AUTO_DISMISS_AFTER_S = 8.0

#: 🔴 The **only** verdict that may dismiss itself.
#:
#: `SERVING_VISIBLE` is the whole of "it is already running, its icon is
#: on screen, click it" — a statement about a healthy app, carrying no
#: instruction the user has to act on later. Everything else carries one:
#: make room in the menu bar, fix that folder's permissions, look behind
#: another window, force quit *this specific pid*. A window that deletes
#: an instruction after eight seconds is worse than no window, because
#: the user now knows something was wrong and cannot read what.
#:
#: `NEVER_REPORTED` deliberately is **not** here even though its headline
#: is also "ImageView is already running": its body asks the reader to
#: click the icon and report back what happened, which is an instruction.
AUTO_DISMISS_VERDICTS = frozenset({cstate.Verdict.SERVING_VISIBLE})

#: The button that is always present, and the guaranteed dismissal path.
#: The panel may never become key — `becomesKeyOnlyIfNeeded` is True and
#: nothing here activates the app — so Cmd-W and Esc are conveniences
#: and this is the contract.
CLOSE_BUTTON = "Close"

#: Offered only when the notice carries evidence beyond its own words.
COPY_BUTTON = "Copy details"

#: The one spatial claim this app makes, and the only layer entitled to
#: make it.
#:
#: 🔴 `contention_state` must never say this, and this module must never
#: say it unconditionally. 4a's words have two consumers — this panel, and
#: `<role>.stderr.log` / `tools/status.py`, where there is no window and
#: the sentence would be a lie — so 4a says only what is true in both
#: ("the icon is in the menu bar, click it"). The pointer is added here,
#: by the layer that actually placed a window, and only when it placed it
#: where the sentence says.
POINTER_SENTENCE = "ImageView's menu bar icon is directly above this window."


@dataclasses.dataclass(frozen=True)
class Message:
    """Everything the panel renders, and the **only** thing it renders.

    Added in Phase 5, when a second caller arrived. Until then the panel
    took a `contention_state.Notice` directly, which was right while a
    contended launch was its only source — and wrong the moment
    `menubar._advise` and `first_run_window._advise` needed the same
    window for something that has no verdict, no holder pid, no
    heartbeat and no evidence to copy.

    🔴 **The alternative was to invent a ninth `Verdict`, and that would
    have been the bug.** `Verdict` is 4a's classifier output: `classify`
    is written to its precedence order, `CAPTURE_STACKS_FOR` names two
    members by identity, and `tools/status.py` reports on it. A member
    meaning "this is not a contended launch at all" would sit in an enum
    whose every other member is an answer to "what is the lock holder
    doing", and the first thing to break would be the classifier this
    project has already had to correct twice.

    So the seam moves down one layer instead: `message_for` turns a
    `Notice` into one of these, `advisory` builds one from two plain
    strings, and the shell knows about neither `Verdict` nor `Notice`.

    `dismiss_after_s` is `None` for "stays until it is closed", which is
    what every advisory gets — see `AUTO_DISMISS_VERDICTS` for why a
    window that deletes its own text after eight seconds is only ever
    right for one thing.

    `details` is the `Copy details` payload, and an **empty string means
    the button is not offered**; `buttons` is what decides that, so the
    two are constructed together rather than re-derived by the shell.
    """

    headline: str
    body: str
    buttons: tuple[str, ...] = (CLOSE_BUTTON,)
    details: str = ""
    dismiss_after_s: float | None = None


@dataclasses.dataclass(frozen=True)
class Rect:
    """A rectangle in Cocoa coordinates: `(x, y)` is the bottom-left."""

    x: float
    y: float
    width: float
    height: float

    @property
    def max_x(self) -> float:
        return self.x + self.width

    @property
    def max_y(self) -> float:
        return self.y + self.height

    @property
    def center_x(self) -> float:
        return self.x + self.width / 2.0

    @property
    def center_y(self) -> float:
        return self.y + self.height / 2.0

    def contains_point(self, px: float, py: float) -> bool:
        """Inclusive on every edge.

        Inclusive deliberately: a status item parked exactly on a screen's
        origin is on that screen, and an exclusive test would answer that
        it is on no screen at all — which routes it to the fallback and
        loses the anchor for the one case where the geometry is simplest.
        """
        return self.x <= px <= self.max_x and self.y <= py <= self.max_y

    def contains_center_of(self, other: Rect) -> bool:
        return self.contains_point(other.center_x, other.center_y)


@dataclasses.dataclass(frozen=True)
class Screen:
    """One attached display: its full `frame` and its `visibleFrame`.

    Both are needed and they answer different questions. Containment —
    "is the icon on this screen" — is about `frame`, because a status item
    lives in the menu bar, which `visibleFrame` deliberately excludes.
    Placement is about `visible_frame`, because that is what stays clear
    of the menu bar and the Dock.
    """

    frame: Rect
    visible_frame: Rect


@dataclasses.dataclass(frozen=True)
class Placement:
    """Where to put the panel, as a Cocoa origin plus its provenance.

    `anchored` is True when the origin was derived from a real status-item
    frame, and False when nothing pointable was found and the panel was
    centred instead. It is carried rather than recomputed so that a caller
    — or a log line — can say which of the two happened without guessing
    from the numbers.

    🔴 `clamped` is True when the anchor had to be pulled back inside the
    screen, and it exists **only** so that `pointer_sentence` can refuse.
    A clamped panel still sits under the icon's centre — that is proved by
    a test — but its centre is no longer under the icon's centre, and
    "directly above this window" is then a spatial claim stronger than
    what was established. That is the same class of fault as printing a
    coordinate: text asserting something the code did not work out.
    """

    screen_index: int
    x: float
    y: float
    anchored: bool
    clamped: bool = False


def cocoa_rect(
    quartz_x: float,
    quartz_y: float,
    width: float,
    height: float,
    zero_screen_height: float,
) -> Rect:
    """A window-server rectangle, in `NSWindow` coordinates.

    `zero_screen_height` is the height of the **zero-origin screen** — the
    one whose Cocoa frame origin is `(0, 0)` — because that screen's top
    edge is where Quartz puts its origin. It is not necessarily the screen
    the window is on, and it is not necessarily the largest one; passing
    the wrong screen's height flips every off-main-screen window into the
    wrong place, silently, which is why it is a parameter rather than
    something this module tries to work out.
    """
    return Rect(
        x=float(quartz_x),
        y=float(zero_screen_height) - float(quartz_y) - float(height),
        width=float(width),
        height=float(height),
    )


def rect_of(fact: cstate.WindowFact, zero_screen_height: float) -> Rect:
    """`cocoa_rect` for one of 4a's `WindowFact`s."""
    return cocoa_rect(fact.x, fact.y, fact.width, fact.height, zero_screen_height)


def icon_is_pointable(icon: Rect | None, screen_frame: Rect) -> bool:
    """Whether a status-item rectangle is worth placing a panel under.

    🔴 **UNMEASURED, AND THE ONE FUNCTION TO CORRECT WHEN IT IS
    MEASURED.** Whether a status item that a *notch* has pushed out of the
    menu bar still produces an on-screen window — and if it does, what
    bounds it reports — is **not known**. It cannot be measured on this
    machine: it needs a Mac with a notch, and no session has had one. The
    whole "your icon is hidden, make room for it" reading depends on the
    answer, and so does this placement: if a notch-hidden item still
    reports plausible bounds, the panel is anchored under an icon nobody
    can see and the app is pointing at an empty stretch of menu bar.

    Everything this can check today is a *shape* check, and the checks are
    deliberately weak rather than clever, because a strong guess about an
    unmeasured mechanism is worse than an honest fallback:

    * nothing at all to point at;
    * a degenerate rectangle — a zero width or height is not a place;
    * a centre that is not on the screen being placed on.

    When someone finally runs this on a notched Mac, the correction goes
    **here** and nowhere else: `placement_for` treats a False as "centre
    the panel instead", which is the benign reading in both directions.
    """
    if icon is None:
        return False
    if icon.width <= 0.0 or icon.height <= 0.0:
        return False
    return screen_frame.contains_center_of(icon)


def allowed_screens(
    screens: Sequence[Screen], avoid: Sequence[Rect] = ()
) -> tuple[int, ...]:
    """The indices of screens the notice may be placed on.

    A screen is dropped when it holds the centre of any `avoid`
    rectangle. `avoid` is the *display agent's* on-screen windows, read
    from the window server by 4a — so "this is the picture display" is a
    statement about what is actually drawing there, not a guess from a
    resolution or a display id. That matters: `display_target` can be
    configured to treat any attached screen as the View, and a geometry
    heuristic here would disagree with it the moment someone changed it.

    Returns **`(0,)`** rather than an empty tuple when every screen is
    excluded. That is a deliberate, documented retreat: it means the
    display agent has windows on every screen, in which case there is no
    screen left that satisfies the rule and showing nothing at all is
    worse than showing the notice where a person is most likely looking.
    """
    if not screens:
        return ()
    keep = tuple(
        index
        for index, screen in enumerate(screens)
        if not any(screen.frame.contains_center_of(target) for target in avoid)
    )
    return keep or (0,)


def _desired_x(icon_frame: Rect, panel_width: float) -> float:
    """The panel origin that puts its centre under the icon's, before any
    clamp. One function so that `anchor_for` and the clamp detection in
    `placement_for` cannot drift apart — two copies of this expression
    would eventually disagree, and the disagreement would show up as a
    pointer sentence that is wrong rather than as an obvious bug."""
    return icon_frame.center_x - panel_width / 2.0


def _clamp_x(desired_x: float, visible: Rect, panel_width: float) -> float:
    """Keep the panel inside `visible`, with `EDGE_MARGIN` to spare.

    When the panel is wider than the screen has room for, the clamp range
    inverts and `min`/`max` would answer something arbitrary; centring is
    the only sensible reading and it is what this does.
    """
    low = visible.x + EDGE_MARGIN
    high = visible.max_x - EDGE_MARGIN - panel_width
    if high < low:
        return visible.x + (visible.width - panel_width) / 2.0
    return min(max(desired_x, low), high)


def _top_y(visible: Rect, panel_height: float) -> float:
    """The origin y that puts the panel's top edge below the menu bar."""
    y = visible.max_y - MENU_BAR_GAP - panel_height
    floor = visible.y + EDGE_MARGIN
    if y >= floor:
        return y
    if panel_height <= visible.height:
        # It fits, but not with the full gap. Give up the gap, not the
        # bottom margin.
        return floor
    # Taller than the screen. Keep the TOP edge on screen — the headline
    # and the first lines of the body live there — and let the bottom
    # fall off, which is the half a reader can scroll to with their eyes.
    return visible.max_y - panel_height


def anchor_for(
    icon_frame: Rect, screen_visible_frame: Rect, panel_size: tuple[float, float]
) -> tuple[float, float]:
    """The panel's Cocoa origin, directly under `icon_frame`.

    The icon's centre x becomes the panel's centre x; the panel's top edge
    sits `MENU_BAR_GAP` below the menu bar; the result is clamped into
    `screen_visible_frame` with `EDGE_MARGIN` to spare on every side.

    **What the clamp guarantees, stated precisely, because the plan states
    it too strongly.** The plan says "with a 34 pt icon and a 360 pt panel
    the icon stays within the panel's horizontal span even when clamped
    hard to either edge". The *centre* always does, which is the property
    the placement's meaning rests on — the panel always sits under the
    icon. The icon's full span does **not**, at the extreme: an icon flush
    with the screen edge overhangs the clamped panel by exactly
    `EDGE_MARGIN`. An icon inset by `EDGE_MARGIN` or more — which every
    real menu bar item is, since the menu bar has its own trailing
    padding — is fully within the span. Both are pinned by tests, and the
    weaker of the two is the one that is universally true.
    """
    panel_width, panel_height = panel_size
    return (
        _clamp_x(_desired_x(icon_frame, panel_width), screen_visible_frame, panel_width),
        _top_y(screen_visible_frame, panel_height),
    )


def centred_origin(
    screen_visible_frame: Rect, panel_size: tuple[float, float]
) -> tuple[float, float]:
    """The fallback origin: horizontally centred, same height as anchored.

    Same vertical placement as `anchor_for` on purpose. The panel appears
    where the reader's eye already is — just under the menu bar — whether
    or not there was an icon to point at, so the two cases do not read as
    two different features.
    """
    panel_width, panel_height = panel_size
    return (
        _clamp_x(
            screen_visible_frame.center_x - panel_width / 2.0,
            screen_visible_frame,
            panel_width,
        ),
        _top_y(screen_visible_frame, panel_height),
    )


def placement_for(
    icons: Sequence[Rect],
    avoid: Sequence[Rect],
    screens: Sequence[Screen],
    panel_size: tuple[float, float],
) -> Placement | None:
    """Where the panel goes. `None` only when there are no screens at all.

    `icons` are the holder's **visible** status items, already filtered by
    `contention_state.visible_status_items` — the one buried under the
    picture is gone before this is called. The first one that is pointable
    and sits on an allowed screen wins; there is normally exactly one, and
    ordering beyond "the first" would be inventing a preference from
    evidence that does not carry one.
    """
    allowed = allowed_screens(screens, avoid)
    if not allowed:
        return None
    for icon in icons:
        for index in allowed:
            screen = screens[index]
            if not icon_is_pointable(icon, screen.frame):
                continue
            x, y = anchor_for(icon, screen.visible_frame, panel_size)
            # Exact equality, deliberately: `_clamp_x` returns the value
            # it was handed, unchanged, whenever it is in range — so this
            # is "did the clamp move it", not a float comparison with a
            # tolerance to argue about.
            return Placement(
                screen_index=index,
                x=x,
                y=y,
                anchored=True,
                clamped=x != _desired_x(icon, panel_size[0]),
            )
    index = allowed[0]
    x, y = centred_origin(screens[index].visible_frame, panel_size)
    return Placement(screen_index=index, x=x, y=y, anchored=False)


def pointer_sentence(placement: Placement | None) -> str | None:
    """The "look up" line, or `None` when this app has not earned it.

    🔴 **Returns `None` unless the panel really was placed under the
    icon.** Three ways that fails, and all three are the same fault as
    printing a coordinate — text asserting a spatial fact the code did not
    establish:

    * **no placement at all** — there were no screens, and AppKit centred
      the window itself;
    * **not anchored** — nothing pointable was found, so the panel is
      centred on a screen and there is no icon it is under. This covers
      the icon that was buried on the picture display, the icon on a
      screen excluded for being the picture display, and bounds that fell
      on no screen;
    * **clamped** — the anchor was pulled back inside the screen edge, so
      the panel's centre is no longer under the icon's.

    The last one is the subtle one and it is why `Placement.clamped`
    exists. A clamped panel still has the icon somewhere over it, which is
    *nearly* the claim — and "nearly" is exactly the standard this phase
    exists to raise. Silence costs a sentence; a wrong pointer sends
    someone to look at the wrong end of their menu bar while their app
    will not start.
    """
    if placement is None or not placement.anchored or placement.clamped:
        return None
    return POINTER_SENTENCE


def placement_log_line(placement: Placement | None, pointer: str | None) -> str:
    """One line for `<role>.stderr.log` saying where the panel went and
    whether it claimed anything.

    **Always returns a line, including for `placement is None`.** That is
    the whole reason this is a function rather than an f-string in the
    shell: the f-string was inside `if placement is not None`, so the one
    case where nothing could be worked out — no screens, AppKit centred
    the window itself — logged **nothing at all**. A diagnostic that goes
    quiet exactly when it could not tell is this project's oldest bug
    shape, and it was sitting in the phase built to fix it.

    Pure, so the wording is testable. Three conditionals inside a function
    no test can reach was the other half of the problem: an inverted word
    here misleads precisely the person this phase exists for.
    """
    if placement is None:
        return (
            "notice: panel placed by AppKit (no screens could be listed), "
            "pointer withheld."
        )
    where = "under the icon" if placement.anchored else "centred, nothing to point at"
    clamp = ", clamped to the screen edge" if placement.clamped else ""
    claim = "shown" if pointer else "withheld"
    return (
        f"notice: panel on screen {placement.screen_index} at "
        f"{placement.x:.0f}, {placement.y:.0f} ({where}{clamp}), pointer {claim}."
    )


def auto_dismiss_after(notice: cstate.Notice) -> float | None:
    """Seconds until this notice may close itself, or `None` for never."""
    if notice.verdict in AUTO_DISMISS_VERDICTS:
        return AUTO_DISMISS_AFTER_S
    return None


def _layers_text(layers: tuple[int, ...] | None) -> str:
    """"could not look" and "nothing there" must not print the same — the
    same distinction `contention._windows_text` keeps, for the same
    reason."""
    if layers is None:
        return "could not be listed"
    if not layers:
        return "none"
    return ", ".join(str(layer) for layer in layers)


def has_details(notice: cstate.Notice) -> bool:
    """Whether there is anything to copy beyond the words on screen."""
    return (
        notice.holder_pid is not None
        or notice.display_pid is not None
        or notice.heartbeat_age_s is not None
        or notice.window_layers is not None
        or notice.stacks_path is not None
    )


def buttons_for(notice: cstate.Notice) -> tuple[str, ...]:
    """The buttons, in left-to-right order.

    Close is last so it is rightmost, which is where a dismissal lives in
    every other window this app puts on screen. There is no third button
    and there is deliberately no *action* button: this window states and
    instructs, and the one thing it is allowed to do to the machine is put
    text on the clipboard.
    """
    if has_details(notice):
        return (COPY_BUTTON, CLOSE_BUTTON)
    return (CLOSE_BUTTON,)


def details_text(notice: cstate.Notice) -> str:
    """The `Copy details` payload: the words on screen, plus the evidence.

    Deliberately **not** home-abbreviated, unlike `tools/status.py`'s
    report. That one is written to be pasted into somebody else's issue
    tracker, so it shortens the user's home directory; this one is a copy
    of what is already on the reader's own screen, and the body above it
    names the same paths in full. A payload that disagreed with the window
    it came from would be the more surprising of the two.
    """
    age = notice.heartbeat_age_s
    lines = [
        "ImageView — a second copy did not start",
        f"verdict: {notice.verdict.value}",
        "",
        notice.headline,
        notice.body,
        "",
        f"holder pid: {notice.holder_pid if notice.holder_pid is not None else 'unknown'}",
        f"picture display pid: "
        f"{notice.display_pid if notice.display_pid is not None else 'not verified'}",
        f"heartbeat: "
        + (f"{age:.0f}s since it last reported working" if age is not None else "never reported"),
        f"on-screen window layers: {_layers_text(notice.window_layers)}",
        f"state folder writable: {'yes' if notice.state_writable else 'no'}",
        f"thread stacks: {notice.stacks_path or 'not captured'}",
    ]
    return "\n".join(lines)


def message_for(notice: cstate.Notice) -> Message:
    """The contended-launch `Notice`, as the thing the panel renders.

    A pure translation and nothing else: every field comes from a
    function above that was already tested on its own. It exists so that
    the shell never sees a `Verdict` — see `Message` for why inventing a
    verdict for the other caller would have been the worse trade.
    """
    return Message(
        headline=notice.headline,
        body=notice.body,
        buttons=buttons_for(notice),
        details=details_text(notice),
        dismiss_after_s=auto_dismiss_after(notice),
    )


def advisory(headline: str, body: str) -> Message:
    """What Phase 5's converted `NSAlert` sites show instead.

    🔴 **`dismiss_after_s` is `None`, unconditionally.** An advisory is
    always the app telling the user something it was asked to tell them
    — the version number, or why the pictures did not start — and a
    window that deletes that after eight seconds is the fault
    `AUTO_DISMISS_VERDICTS` documents at length. There is no verdict here
    to look up, so this is not a lookup that could be given the wrong
    table; it is a constant, and a test says so.

    🔴 **No `Copy details`, and that is not an omission.** The notice
    offers one because it carries evidence the reader cannot see — a
    holder pid, a heartbeat age, a stacks path. An advisory's evidence
    *is* the two strings on screen, and they are selectable, so a second
    button offering to copy what is already copyable would be furniture.

    The button says `Close` rather than the `OK` the `NSAlert` had.
    `OK` acknowledges a question, and this window no longer asks one:
    nothing waits on the answer, and the caller has already returned by
    the time it is read.
    """
    return Message(
        headline=headline,
        body=body,
        buttons=(CLOSE_BUTTON,),
        details="",
        dismiss_after_s=None,
    )


def advisory_log_line(message: Message) -> str:
    """One line for `<role>.stderr.log` recording that an advisory went
    on screen.

    The `NSAlert` this replaced logged **nothing at all**, in an app
    whose entire diagnostic story is "it writes to stderr and nowhere
    else". A user reporting "it said it couldn't start
    showing pictures" left no trace anyone could read afterwards.

    🔴 **The body's newlines are collapsed, and that is the load-bearing
    half.** `menubar_state.about_text` joins its paragraphs with `\\n\\n`,
    so logging the body verbatim would write a six-line entry into a log
    whose every other entry is one timestamped line — and every tool that
    reads it, starting with a human scrolling, treats a line as an event.
    """
    return f"notice: advisory on screen — {message.headline} {' '.join(message.body.split())}"


__all__ = [
    "AUTO_DISMISS_AFTER_S",
    "AUTO_DISMISS_VERDICTS",
    "CLOSE_BUTTON",
    "COPY_BUTTON",
    "EDGE_MARGIN",
    "MENU_BAR_GAP",
    "PANEL_WIDTH",
    "POINTER_SENTENCE",
    "Message",
    "Placement",
    "Rect",
    "Screen",
    "advisory",
    "advisory_log_line",
    "allowed_screens",
    "anchor_for",
    "auto_dismiss_after",
    "buttons_for",
    "centred_origin",
    "cocoa_rect",
    "details_text",
    "has_details",
    "icon_is_pointable",
    "message_for",
    "placement_for",
    "placement_log_line",
    "pointer_sentence",
    "rect_of",
]
