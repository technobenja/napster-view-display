"""The first-run flow's decisions, with no AppKit in it.

`first_run_window.py` is the AppKit shell; this module is the part that
can be wrong in an interesting way. Same split as `settings_state.py`,
`menubar_state.py` and `calibrate_state.py`, for the same reason.

**This module is deliberately thin.** Step 4 already built everything the
flow *shows*: `settings_state.probe` is the Test button and its copy,
`settings_state.display_options`/`nothing_matched_note` are the display
picker, `SourceForm.from_settings`/`to_settings` are the form model, and
`menubar_state.setup_needed` is the trigger. Re-implementing any of them
here would give the flow a second, drifting definition of the same
decision. What is genuinely new — and therefore what lives here — is
**sequencing**: which step comes next, which steps may be left, when the
flow is finished, and what gets written when it is.

**The order is display -> pictures -> confirm, and the order is the
point.** The flow was reordered away from display -> calibrate -> folder
because that asked a new user for fine-motor alignment against an empty
black circle before ever seeing the device do the thing they installed it
for, and only *then* let them discover their folder was empty. Choosing
pictures before confirming means the confirm step has real content behind
it.

**The confirm step is one click.** It is "skippable" and worded
the button "Looks fine — skip". The button here reads `Looks good`, with
`Adjust the circle` as the plain secondary — see `CONFIRM_PRIMARY`.

**The user-facing copy is named constants here**, not string literals in
the window, so that it is testable and cannot drift between the flow and
the tests that assert on it. Several of these are specified
strings verbatim.

No emoji, no icons.
"""

from __future__ import annotations

import dataclasses
import enum
from collections.abc import Mapping
from typing import Any

from display import blank_schedule, settings as display_settings, source_settings
from display.source_settings import SourceSettings
from ui import menubar_state, settings_state

# -- the steps ---------------------------------------------------------


class Step(enum.Enum):
    """Three steps, in the order they are shown.

    The values are stable identifiers rather than indices so that a
    future step inserted in the middle does not renumber the others.
    """

    DISPLAY = "display"
    PICTURES = "pictures"
    CONFIRM = "confirm"


#: The one place the order is written down. `advance`/`back` walk this,
#: and `STEP_NUMBERS` derives "Step 2 of 3" from it, so adding a step
#: means editing this tuple and nothing else.
STEP_ORDER: tuple[Step, ...] = (Step.DISPLAY, Step.PICTURES, Step.CONFIRM)


def step_number(step: Step) -> int:
    """1-based position, for "Step 2 of 3"."""
    return STEP_ORDER.index(step) + 1


def step_count() -> int:
    return len(STEP_ORDER)


def progress_label(step: Step) -> str:
    return f"Step {step_number(step)} of {step_count()}"


# -- the copy ----------------------------------------------------------

WINDOW_TITLE = "Set up your View"

#: `Setup needed` title promotes this item into the menu. Kept
#: here rather than in `menubar.py` so the flow owns every string that
#: names it, and the ellipsis is the real character (a menu item that
#: opens a window takes one, and three periods is not one).
MENU_ITEM = "Finish setup…"

STEP_TITLES: dict[Step, str] = {
    Step.DISPLAY: "Which screen is your View?",
    Step.PICTURES: "Where should your pictures come from?",
    Step.CONFIRM: "One last thing",
}

#: "Display picking never blocks." The body says so in as many
#: words, because a picker that lists a screen the user cannot identify
#: is only unblocking if they believe guessing is allowed.
DISPLAY_BODY = (
    "Every screen attached to this Mac is listed below. If you are not "
    "sure which one is the View, use Identify to flash each screen, or "
    "just pick the one that looks right — you can change this later."
)
DISPLAY_CHECK_AGAIN = "Check again"
DISPLAY_IDENTIFY = "Identify"
#: What a user sees before they have picked anything, and what the flow
#: falls back to if they never do: the automatic resolution heuristic.
DISPLAY_SKIP_NOTE = (
    "If you do not pick one, the app will look for a 960 x 960 screen "
    "that is not your main screen."
)

PICTURES_BODY = (
    "Pick where your pictures come from, then press Test. Testing here "
    "means you find out now, rather than looking at an empty View later."
)
#: This step is gated: "Save disabled on zero." The same rule applies
#: to Next — advancing past an unusable source produces a View that shows
#: nothing and a user with no idea why.
PICTURES_BLOCKED_NOTE = "Press Test and get a result before you continue."

CONFIRM_BODY = (
    "The app already knows the size and position of the circle on the "
    "View. Nothing here needs changing for most people."
)

#: Verbatim copy.
CONFIRM_NOTE = (
    "These numbers came from one owner's device. Yours may differ "
    "slightly. You can adjust this any time from the menu."
)

#: The gap note, on this step rather than only in the calibration window. The
#: two-ring explanation lives in a window most users will now never open,
#: and the rule is explicit about what happens when the gap is unexplained:
#: the user reads a picture inside a ring of dead black panel as
#: miscalibration and cranks the radius up until the picture meets the
#: bezel, spending the entire safety margin correcting an error that was
#: never there. So the explanation has to appear where the ring does.
DARK_RING_NOTE = (
    "Pictures sit slightly inside the edge of the glass so nothing gets "
    "clipped. That thin dark ring is deliberate."
)
DARK_RING_FOLLOWUP = (
    "If pictures ever look cut off at the edge, use Adjust the circle "
    "from the menu."
)

#: Not "skip". With identical hardware and defaults measured off a real
#: unit, confirming is the expected path, and a button labelled "skip"
#: tells the user they are declining something they ought to have done.
CONFIRM_PRIMARY = "Looks good"
#: Plainly styled, deliberately not competing with the primary.
CONFIRM_SECONDARY = "Adjust the circle"

BACK_BUTTON = "Back"
NEXT_BUTTON = "Next"


def body_for(step: Step) -> str:
    """The explanatory paragraph under a step's title."""
    return {
        Step.DISPLAY: DISPLAY_BODY,
        Step.PICTURES: PICTURES_BODY,
        Step.CONFIRM: CONFIRM_BODY,
    }[step]


def confirm_notes() -> tuple[str, ...]:
    """Every line the confirm step shows under its body, in order.

    Grouped into one function so a test can assert that explanation is actually present on the step where the ring is
    visible, rather than merely defined in this module.
    """
    return (CONFIRM_NOTE, DARK_RING_NOTE, DARK_RING_FOLLOWUP)


# -- how the flow ends -------------------------------------------------


class Finish(enum.Enum):
    """What the last click asked for.

    `ADJUST` still finishes the flow and still writes everything
    `CONFIRMED` writes — it only additionally asks the caller to open the
    calibration window. Treating it as "cancel" would throw away a
    completed source choice because the user wanted to nudge a circle.
    """

    CONFIRMED = "confirmed"
    ADJUST = "adjust"


# -- the flow ----------------------------------------------------------


@dataclasses.dataclass
class FirstRunFlow:
    """Where the user is, and what they have given us so far.

    Mutable, like `SourceForm` and for the same reason: it *is* the
    session buffer. The durable objects it produces (`SourceSettings`,
    the settings document) are built fresh on finish.
    """

    form: settings_state.SourceForm = dataclasses.field(
        default_factory=settings_state.SourceForm
    )
    step: Step = Step.DISPLAY
    #: The picker's answer: `display_target.stable_display_id`'s
    #: EDID-derived `vendor-model-serial` **string**, not a number.
    #: Empty means "never picked" -> fall back to the resolution
    #: heuristic, which is what a non-blocking picker should do.
    chosen_display_id: str = ""
    #: The most recent Test, or None if the source has been edited since.
    test_result: settings_state.TestResult | None = None
    finished: Finish | None = None

    # -- gating --------------------------------------------------------

    @property
    def can_advance(self) -> bool:
        """Whether Next is enabled on the current step.

        Only the pictures step gates, and it gates on a real result
        rather than on "the fields look filled in".3 puts validation
        at pick time precisely because a plausible-looking URL that
        returns nothing is the failure this flow exists to catch.
        """
        if self.step is Step.PICTURES:
            return self.test_result is not None and self.test_result.save_enabled
        return True

    @property
    def blocked_note(self) -> str:
        """Why Next is disabled, or "" when it is not.

        A disabled button with no adjacent reason is the standard way to
        strand someone, and this flow is the one screen where the user
        has no prior model of what the app expects.
        """
        if self.can_advance:
            return ""
        return PICTURES_BLOCKED_NOTE

    @property
    def can_go_back(self) -> bool:
        return self.step is not STEP_ORDER[0]

    @property
    def is_last_step(self) -> bool:
        return self.step is STEP_ORDER[-1]

    def reachable(self, step: Step) -> bool:
        """Whether `step` could be shown given what has been supplied.

        Steps at or before the current one are always reachable (going
        back is free). A later step is reachable only if every gate
        between here and there is satisfied — which today means the
        pictures gate, but is written as a walk so that a second gate
        does not need this to be rewritten.
        """
        target = STEP_ORDER.index(step)
        here = STEP_ORDER.index(self.step)
        if target <= here:
            return True
        probe = dataclasses.replace(self)
        for _ in range(target - here):
            if not probe.can_advance:
                return False
            probe.step = STEP_ORDER[STEP_ORDER.index(probe.step) + 1]
        return True

    # -- moving --------------------------------------------------------

    def advance(self) -> bool:
        """Move to the next step. Returns whether it moved.

        Refuses rather than raises on a blocked or final step: this is
        called from a button whose enabled state is derived from
        `can_advance`, and a race between the two should be a no-op, not
        a traceback inside an AppKit callback.
        """
        if self.is_last_step or not self.can_advance:
            return False
        self.step = STEP_ORDER[STEP_ORDER.index(self.step) + 1]
        return True

    def back(self) -> bool:
        """Move to the previous step. Returns whether it moved.

        Never gated. Going back must always work, including from a step
        whose own gate is unsatisfied — otherwise a user who mistyped a
        URL on step 2 is trapped on it.
        """
        if not self.can_go_back:
            return False
        self.step = STEP_ORDER[STEP_ORDER.index(self.step) - 1]
        return True

    # -- what the steps collect ----------------------------------------

    def choose_display(self, display_id: str | None) -> None:
        self.chosen_display_id = str(display_id or "")

    def record_test(self, result: settings_state.TestResult | None) -> None:
        self.test_result = result

    def source_edited(self) -> None:
        """Called when any source field changes.

        Drops the previous result, because it belonged to the previous
        value. Without this, a user who tests a working URL, edits it to
        a broken one, and presses Next walks past the gate on a pass that
        was never about the source being saved.
        """
        self.test_result = None

    # -- finishing -----------------------------------------------------

    @property
    def is_complete(self) -> bool:
        """Whether the flow has everything it needs to write.

        Note what this does *not* require: a display choice, or a visit
        to the confirm step's buttons. The display picker never blocks and calibration has shipped defaults, so the source
        is the only genuinely required answer.
        """
        return self.test_result is not None and self.test_result.save_enabled

    def finish(self, how: Finish) -> bool:
        """Record how the flow ended. Returns whether it was allowed."""
        if not self.is_complete:
            return False
        self.finished = how
        return True

    @property
    def wants_calibration(self) -> bool:
        """Whether the caller should open the calibration window after."""
        return self.finished is Finish.ADJUST

    def to_source(self) -> SourceSettings | None:
        """The chosen source, validated. None if it is not usable.

        Goes through `SourceForm.to_settings`, which validates with the
        same function the display uses when reading the file — so this
        flow cannot write a document the display would then reject.
        """
        return self.form.to_settings()


# -- the trigger -------------------------------------------------------


def should_start(settings_data: object) -> bool:
    """Whether to run the flow on launch.

    Delegates to `menubar_state.setup_needed` rather than asking its own
    question, so that the menu's `Setup needed` title and the flow that
    resolves it can never disagree about whether setup is needed — a
    menu that says `Setup needed` while the flow declines to open is a
    dead end with no way out.
    """
    return menubar_state.setup_needed(settings_data)


# -- what gets written on finish ---------------------------------------


def _existing_interval(previous: Mapping[str, Any] | None) -> float:
    """Defensive read; never raises."""
    if isinstance(previous, Mapping):
        value = previous.get("rotation_interval_s")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if value > 0:
                return float(value)
    return display_settings.FALLBACK_ROTATION_INTERVAL_S


def _existing_shuffle(previous: Mapping[str, Any] | None) -> bool:
    if isinstance(previous, Mapping):
        value = previous.get("shuffle")
        if isinstance(value, bool):
            return value
    return display_settings.FALLBACK_SHUFFLE


def settings_document(
    previous: Mapping[str, Any] | None, source: SourceSettings
) -> dict[str, Any]:
    """The `settings.json` to write when the flow finishes.

    Built through `settings_state.settings_document`, so the merge
    behaviour the resolution order requires — additive-only, unknown keys preserved —
    has exactly one implementation.

    The flow does not ask about timing, order or the nightly schedule, so
    those are carried through from whatever is already on disk rather
    than being set to this module's idea of a default. A first run on a
    machine where someone hand-edited `rotation_interval_s` should not
    have that quietly reverted by a flow that never mentioned it.

    Writing the `source` block is also what *clears the trigger*:
    `setup_needed` asks whether the document carries a source the user
    put there, so no separate "first run done" marker is needed. One
    fact, one place — a marker file could disagree with the settings and
    would eventually have to be reconciled with it.
    """
    return settings_state.settings_document(
        previous,
        source=source,
        rotation_interval_s=_existing_interval(previous),
        shuffle=_existing_shuffle(previous),
        schedule=blank_schedule.parse_schedule(
            previous.get("blank_schedule") if isinstance(previous, Mapping) else None
        ),
    )


def calibration_document(
    previous: Mapping[str, Any] | None, display_id: str | None
) -> dict[str, Any] | None:
    """`calibration.json` with the display choice merged in, or None.

    None means "do not write" — and it is returned when there is no
    existing calibration document to merge into, matching what the
    settings window already does. Creating one from nothing here would
    write a `target_screen` with no circle numbers beside it, and the
    display would fall back to the bundled defaults anyway.

    `target_screen` belongs in this file rather than in settings.json for
    reason: `get_view_screen` derives the target from here, and
    other tools read the same file. Storing the picker's answer
    somewhere private would let different tools target different monitors
    with no way to notice.
    """
    if not isinstance(previous, Mapping) or not previous:
        return None
    document: dict[str, Any] = dict(previous)
    if display_id:
        document["target_screen"] = {
            "resolve_strategy": EXPLICIT_RESOLVE_STRATEGY,
            # A string, matching `Calibration.target_display_id` and what
            # `screen_for_display_id` compares against. Coercing this to
            # an int would produce a document the display reads, fails to
            # match, and silently falls back from.
            "display_id": str(display_id),
        }
    else:
        document["target_screen"] = {
            "resolve_strategy": FALLBACK_RESOLVE_STRATEGY,
        }
    return document


#: `display_target.EXPLICIT_STRATEGY` and the fallback strategy string,
#: restated here rather than imported: `display_target` imports AppKit at
#: module level, and this module's whole premise is that it is testable
#: without it. Restating a constant is a drift risk, so
#: `test_first_run_state.py` imports `display_target` and asserts these
#: two are equal to it — the check lives in the test, where AppKit is
#: allowed to be a soft dependency, rather than in the import graph.
EXPLICIT_RESOLVE_STRATEGY = "explicit"
FALLBACK_RESOLVE_STRATEGY = "match_by_resolution_excluding_main"


__all__ = [
    "BACK_BUTTON",
    "CONFIRM_BODY",
    "CONFIRM_NOTE",
    "CONFIRM_PRIMARY",
    "CONFIRM_SECONDARY",
    "DARK_RING_FOLLOWUP",
    "DARK_RING_NOTE",
    "DISPLAY_BODY",
    "DISPLAY_CHECK_AGAIN",
    "DISPLAY_IDENTIFY",
    "DISPLAY_SKIP_NOTE",
    "EXPLICIT_RESOLVE_STRATEGY",
    "FALLBACK_RESOLVE_STRATEGY",
    "MENU_ITEM",
    "NEXT_BUTTON",
    "PICTURES_BLOCKED_NOTE",
    "PICTURES_BODY",
    "STEP_ORDER",
    "STEP_TITLES",
    "WINDOW_TITLE",
    "Finish",
    "FirstRunFlow",
    "Step",
    "body_for",
    "calibration_document",
    "confirm_notes",
    "progress_label",
    "settings_document",
    "should_start",
    "step_count",
    "step_number",
]
