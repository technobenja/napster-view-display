"""The `NSPanel` half of the contended-launch notice.

`ui/test_notice_state.py` covers the geometry, headlessly. This file
covers the window, and it is honest about what it does **not** cover:

* **Anything that needs a window server is skipped loudly outside an Aqua
  session.** A green CI badge does not cover a single assertion in the
  `PanelTests` class below, and `README.md` says so.
* **`present()` and the run loop are not covered by anything.** Ordering
  a panel front puts a real window on the developer's screen, installs a
  global key monitor and schedules a timer; running `app.run()` then
  blocks until a human dismisses it, which no `unittest` run can do.
  Everything that path *decides* — whether to auto-dismiss, and after how
  long — is a pure function and is tested in the other file. What is
  untested is that AppKit does what it is asked. That gap is the same one
  every `*_window.py` in this package carries by design, and this note
  exists so it is a stated limit rather than a discovered one.
* **The clipboard write is not exercised**, deliberately: a test suite
  that clobbers the owner's pasteboard to prove a button works has taken
  more than it gave. `notice_state.details_text` — the payload, which is
  the part that can be wrong — is tested.

The source-level assertions are not a substitute for behaviour. The claim
they make is *about the source*: the **absence of a call** is the
property, so reading the syntax tree measures the thing rather than
standing in for it. They run everywhere, including CI, which is what
keeps the three forbidden calls out of this file on a machine with no
window server to prove it on.
"""

from __future__ import annotations

import ast
import subprocess
import unittest
from pathlib import Path

import AppKit

from ui import contention_state as cs
from ui import notice_state as ns
from ui import notice_window as nw

SOURCE = Path(nw.__file__).read_text()
TREE = ast.parse(SOURCE)

#: Every attribute name the module actually *uses*. Docstrings and
#: comments are not `Attribute` nodes, so a file that explains why it
#: never calls `runModal()` does not thereby look like it calls it — the
#: exact false positive a `grep` would produce here, in the one file
#: whose whole job is to talk about those calls.
ATTRIBUTES = {node.attr for node in ast.walk(TREE) if isinstance(node, ast.Attribute)}


def attributes_of(method: str) -> set[str]:
    """The same, narrowed to one method of `NoticeController`.

    A file-level scan cannot say *which* method makes a call, and Phase 5
    proved that matters: adding a second `orderFrontRegardless()` call
    elsewhere in this file turned a file-level presence check into a
    guard that could not fail.
    """
    for node in ast.walk(TREE):
        if isinstance(node, ast.FunctionDef) and node.name == method:
            return {
                inner.attr
                for inner in ast.walk(node)
                if isinstance(inner, ast.Attribute)
            }
    raise AssertionError(f"notice_window has no method named {method}")


def aqua_session() -> bool:
    """Whether this process is in a graphical login session. Same
    discriminator `ui/test_contention.py` and the release gate use."""
    try:
        done = subprocess.run(
            ["launchctl", "managername"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.stdout.strip() == "Aqua"


AQUA = aqua_session()
NOT_AQUA = (
    "needs a window server: `launchctl managername` is not Aqua, so an "
    "NSPanel cannot be built. A green run here has NOT covered the notice "
    "window's style mask, its level, its hidesOnDeactivate setting, its "
    "text fields or its placement."
)


def notice(verdict=cs.Verdict.UNRESPONSIVE, **overrides) -> cs.Notice:
    base = dict(
        verdict=verdict,
        headline="ImageView is not responding",
        body=(
            "ImageView (pid 4321) last reported working 37s ago and is not "
            "responding. Its menu bar icon is still there, but clicking it will "
            "do nothing. If you want it gone, force quit pid 4321 specifically."
        ),
        holder_pid=4321,
        display_pid=4300,
        heartbeat_age_s=37.0,
        window_layers=(25,),
        state_writable=True,
        stacks_path="/tmp/viewlab/ui.stacks.log",
    )
    base.update(overrides)
    return cs.Notice(**base)


class ForbiddenCallTests(unittest.TestCase):
    """🔴 The process that arrives to diagnose a wedge must never make the
    call it is diagnosing."""

    def test_it_never_runs_a_modal(self) -> None:
        """`runModal()` spins the run loop in `NSModalPanelRunLoopMode`.
        A Quit Apple Event is then never *fetched*, default-mode timers
        stop firing, and an alert that opened behind everything is
        invisible, unanswerable and permanent. That is the mechanism
        suspected in the wedge this window exists to explain."""
        self.assertNotIn("runModal", ATTRIBUTES)

    def test_it_never_asks_to_be_activated(self) -> None:
        """Apple reports app activation failing intermittently on
        macOS 26 (DevForums 807805 / FB21087054), which is the OS the app
        this notice is about runs on. `orderFrontRegardless()` is the
        documented way for an accessory app to show a window without
        asking."""
        self.assertNotIn("activateIgnoringOtherApps_", ATTRIBUTES)

    def test_it_never_creates_a_status_item(self) -> None:
        """`statusItemWithLength_` is the named blocking call behind the
        "it started but never reached the menu bar" hypothesis."""
        self.assertNotIn("statusItemWithLength_", ATTRIBUTES)

    def test_it_never_makes_a_window_key(self) -> None:
        """🔴 A fourth forbidden call, added in Phase 5 because that
        phase made the third one's guard vacuous.

        `makeKeyAndOrderFront_` takes the keyboard from whatever the
        reader was doing. This window sets `becomesKeyOnlyIfNeeded` for
        exactly that reason, and a panel that grabs focus is worse than
        the modal it replaced — `orderFrontRegardless()` is the whole
        point, and it is the documented way for an accessory app to show
        a window without asking to be activated.

        **Why this test exists rather than the presence check below
        carrying the weight.** The 4b mutation "orderFrontRegardless
        becomes makeKeyAndOrderFront" used to be killed by
        `assertIn("orderFrontRegardless", ATTRIBUTES)` — mutate the only
        call and the name vanished from the module. Phase 5 added a
        second one in `bring_forward`, so mutating `present()`'s call
        left the name behind and the mutation **SURVIVED**, on a
        regression run of 4b's own matrix. The presence check was never
        a rule about `present()`; it was a scan self-test that happened
        to catch this. Now the rule is stated directly.
        """
        self.assertNotIn("makeKeyAndOrderFront_", ATTRIBUTES)

    def test_present_orders_the_panel_front_rather_than_activating(self) -> None:
        """The positive half, scoped to the one method that shows a
        window for the first time. A file-level `assertIn` cannot tell
        which method makes the call, which is exactly how the mutation
        above slipped through."""
        self.assertIn("orderFrontRegardless", attributes_of("present"))

    def test_the_scan_can_see_the_calls_it_looks_for(self) -> None:
        """A scan that finds nothing because it is looking in the wrong
        place passes for the wrong reason. These two calls are present,
        so a scan that reports all five answers has actually parsed the
        module."""
        self.assertIn("orderFrontRegardless", ATTRIBUTES)
        self.assertIn("setHidesOnDeactivate_", ATTRIBUTES)

    def test_the_level_is_never_raised(self) -> None:
        """An `NSPanel`'s default is `NSFloatingWindowLevel` (measured:
        3), which is above ordinary windows and below everything that
        matters. 1001 belongs to the calibration overlay; a diagnostic
        has no business over a full-screen picture."""
        self.assertNotIn("setLevel_", ATTRIBUTES)


class HidesOnDeactivateSourceTests(unittest.TestCase):
    """🔴 **The guard most likely to be tidied away by someone who does
    not know why it is there**, so it is asserted twice: here, from the
    syntax tree, everywhere including CI — and in `PanelTests` from the
    built window, where an Aqua session can prove the default really is
    the other way.

    MEASURED on this machine, 2026-09-10: an `NSPanel`'s
    `hidesOnDeactivate` defaults to **True**. A default panel would
    therefore vanish the instant the user clicked another application —
    and this window's worst case says *"force quit pid 4321, and leave
    the other ImageView alone"*. The user clicks Activity Monitor, the
    window disappears with the pid in it, and they are back exactly where
    2026-09-08 left them.
    """

    def call(self):
        for node in ast.walk(TREE):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "setHidesOnDeactivate_"
            ):
                return node
        return None

    def test_the_call_is_there_at_all(self) -> None:
        self.assertIsNotNone(self.call(), "the notice panel no longer pins hidesOnDeactivate")

    def test_it_is_called_with_a_literal_false(self) -> None:
        """Not merely "the setter is called": a call passing True, or a
        name that happens to be falsy today, is the same bug."""
        node = self.call()
        self.assertIsNotNone(node)
        self.assertEqual(len(node.args), 1)
        self.assertIsInstance(node.args[0], ast.Constant)
        self.assertIs(node.args[0].value, False)


class _Point:
    def __init__(self, x, y):
        self.x = x
        self.y = y


class _Size:
    def __init__(self, width, height):
        self.width = width
        self.height = height


class _Frame:
    """An `NSRect` the way PyObjC hands one back: `.origin.x/.y` and
    `.size.width/.height`. A stand-in rather than a real one, so these
    run with no window server — and so the ground truth is written down
    here instead of being computed by the code under test."""

    def __init__(self, x, y, width, height):
        self.origin = _Point(x, y)
        self.size = _Size(width, height)


class ConversionTests(unittest.TestCase):
    """🔴 The AppKit boundary, against hand-written literals.

    These exist because three mutations **survived**: transposing `x` and
    `y` in `_rect`, and both branches of `zero_screen_height`. They
    survived for one reason worth remembering — every other test that
    touches these functions builds its synthetic icon *out of them* and
    then asserts a property computed back *through* them. Both sides move
    together, so a systematic conversion error is invisible: the test
    agrees with the bug.

    🔴 And `zero_screen_height`'s zero-origin test mutated to `if True`
    passed **because on this machine screen 0 happens to be the
    zero-origin screen**. A hardware-dependent pass, on the next user's
    display arrangement it diverges — and that function's own docstring
    says getting it wrong "flips every off-main-screen window into the
    wrong place, silently".

    No Aqua gate: nothing here needs a window server, which is the point.
    """

    def test_rect_keeps_x_and_y_apart(self) -> None:
        got = nw._rect(_Frame(11.0, 22.0, 33.0, 44.0))
        self.assertEqual((got.x, got.y, got.width, got.height), (11.0, 22.0, 33.0, 44.0))

    def test_rect_keeps_width_and_height_apart(self) -> None:
        got = nw._rect(_Frame(0.0, 0.0, 1600.0, 1000.0))
        self.assertEqual(got.width, 1600.0)
        self.assertEqual(got.height, 1000.0)

    def test_the_zero_origin_screen_is_chosen_not_the_first(self) -> None:
        """🔴 The one that passes on this hardware for the wrong reason.
        Here the zero-origin screen is listed **second**, so a function
        that returns `screens[0]` answers 900 and this fails."""
        screens = (
            ns.Screen(
                frame=ns.Rect(-1440.0, 200.0, 1440.0, 900.0),
                visible_frame=ns.Rect(-1440.0, 200.0, 1440.0, 875.0),
            ),
            ns.Screen(
                frame=ns.Rect(0.0, 0.0, 1600.0, 1000.0),
                visible_frame=ns.Rect(0.0, 0.0, 1600.0, 975.0),
            ),
        )
        self.assertEqual(nw.zero_screen_height(screens), 1000.0)

    def test_with_no_zero_origin_screen_it_falls_back_to_the_first(self) -> None:
        """Not to 0.0: a height of zero makes every conversion nonsense
        rather than merely approximate, and the first screen's height is
        the closest thing to an answer available."""
        screens = (
            ns.Screen(
                frame=ns.Rect(100.0, 100.0, 800.0, 777.0),
                visible_frame=ns.Rect(100.0, 100.0, 800.0, 752.0),
            ),
        )
        self.assertEqual(nw.zero_screen_height(screens), 777.0)

    def test_with_no_screens_it_answers_zero(self) -> None:
        self.assertEqual(nw.zero_screen_height(()), 0.0)

    def test_a_full_conversion_against_a_written_down_answer(self) -> None:
        """End to end with nothing derived: a status item at the top-left
        of a 1000pt-tall zero-origin screen, Quartz `y=0`, is Cocoa
        `y=976` — because Quartz measures down from the top and Cocoa up
        from the bottom, and 1000 - 0 - 24 is 976."""
        icon = cs.WindowFact(layer=25, on_screen=True, x=1169.0, y=0.0, width=34.0, height=24.0)
        got = ns.rect_of(icon, 1000.0)
        self.assertEqual((got.x, got.y, got.width, got.height), (1169.0, 976.0, 34.0, 24.0))


@unittest.skipUnless(AQUA, NOT_AQUA)
class PanelTests(unittest.TestCase):
    """The built window, asked about itself."""

    @classmethod
    def setUpClass(cls) -> None:
        # Accessory, so nothing here can put a Dock icon on screen. This
        # is policy, not activation: it asks the window server for
        # nothing.
        app = AppKit.NSApplication.sharedApplication()
        app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

    def build(self, note=None):
        controller = nw.NoticeController.alloc().init()
        panel = controller.build(ns.message_for(note or notice()))
        self.assertIsNotNone(panel, "the notice panel could not be built")
        return controller, panel

    def test_hides_on_deactivate_is_off(self) -> None:
        """🔴 The behavioural half. It fails if the line is deleted,
        because the measured default is the opposite."""
        _, panel = self.build()
        self.assertFalse(panel.hidesOnDeactivate())

    def test_the_measured_default_really_is_the_other_way(self) -> None:
        """Otherwise the test above passes for the wrong reason on some
        future macOS, and nobody would notice that the guard had stopped
        guarding anything."""
        bare = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            AppKit.NSMakeRect(0.0, 0.0, 360.0, 100.0),
            AppKit.NSWindowStyleMaskTitled
            | AppKit.NSWindowStyleMaskClosable
            | AppKit.NSWindowStyleMaskUtilityWindow,
            AppKit.NSBackingStoreBuffered,
            False,
        )
        self.assertTrue(
            bare.hidesOnDeactivate(),
            "an NSPanel no longer hides on deactivate by default — the guard in "
            "notice_window.py is now a no-op and its comment is wrong",
        )

    def test_the_style_mask(self) -> None:
        _, panel = self.build()
        mask = panel.styleMask()
        self.assertTrue(mask & AppKit.NSWindowStyleMaskTitled)
        self.assertTrue(mask & AppKit.NSWindowStyleMaskClosable)
        self.assertTrue(mask & AppKit.NSWindowStyleMaskUtilityWindow)

    def test_it_becomes_key_only_if_needed(self) -> None:
        """Reading a notice must not take focus from whatever the user
        was doing. The Close button works on a window that never becomes
        key, which is why it is the guaranteed dismissal path."""
        _, panel = self.build()
        self.assertTrue(panel.becomesKeyOnlyIfNeeded())

    def test_the_level_is_the_floating_default(self) -> None:
        _, panel = self.build()
        self.assertEqual(panel.level(), AppKit.NSFloatingWindowLevel)

    def test_the_width_is_fixed_and_the_height_fits_the_content(self) -> None:
        short, tall = (
            self.build(notice(body="Short."))[1],
            self.build(
                notice(
                    body=(
                        "ImageView is already running (pid 4321), and it has never "
                        "reported whether it is working — which is simply what "
                        "versions before 1.1.5 do, so this is most likely an older "
                        "copy that is perfectly fine. Click its menu bar icon: if "
                        "the menu opens, nothing is wrong and updating ImageView "
                        "will make this message go away."
                    )
                )
            )[1],
        )
        self.assertEqual(short.frame().size.width, ns.PANEL_WIDTH)
        self.assertEqual(tall.frame().size.width, ns.PANEL_WIDTH)
        self.assertGreater(
            tall.frame().size.height,
            short.frame().size.height,
            "the panel height does not follow the body it has to show",
        )

    def test_the_text_is_selectable_and_not_editable(self) -> None:
        """The pid and the paths are the whole point of the wedge
        messages. A reader who cannot select them has to retype a path
        off a screen."""
        _, panel = self.build()
        fields = [
            view
            for view in panel.contentView().subviews()
            if isinstance(view, AppKit.NSTextField)
        ]
        self.assertEqual(len(fields), 2, "expected a headline and a body")
        for field in fields:
            self.assertTrue(field.isSelectable())
            self.assertFalse(field.isEditable())

    def test_the_words_are_4a_s_words_verbatim(self) -> None:
        """🔴 The 4a/4b seam. The copy is decided and tested in
        `contention_state`; this layer renders it and must not edit it,
        or the tested wording and the shown wording drift apart."""
        note = notice()
        _, panel = self.build(note)
        shown = {
            str(view.stringValue())
            for view in panel.contentView().subviews()
            if isinstance(view, AppKit.NSTextField)
        }
        self.assertIn(note.headline, shown)
        self.assertIn(note.body, shown)

    def test_the_pointer_is_a_field_of_its_own(self) -> None:
        """🔴 Not appended to `body`. 4a's words reach the screen
        byte-identical to the words 4a tests, so the tested copy and the
        shown copy cannot drift — and a sentence this layer wrote does not
        get to look like one the classifier wrote."""
        note = notice()
        controller = nw.NoticeController.alloc().init()
        panel = controller.build(ns.message_for(note), ns.POINTER_SENTENCE)
        shown = [
            str(view.stringValue())
            for view in panel.contentView().subviews()
            if isinstance(view, AppKit.NSTextField)
        ]
        self.assertEqual(len(shown), 3)
        self.assertIn(note.headline, shown)
        self.assertIn(note.body, shown)
        self.assertIn(ns.POINTER_SENTENCE, shown)

    def test_without_a_pointer_there_is_no_third_field(self) -> None:
        _, panel = self.build()
        fields = [
            view
            for view in panel.contentView().subviews()
            if isinstance(view, AppKit.NSTextField)
        ]
        self.assertEqual(len(fields), 2)

    def test_the_pointer_makes_the_panel_taller(self) -> None:
        """A line that is not accounted for in the height is a line that
        overlaps the body or falls off the bottom."""
        note = notice()
        without = self.build(note)[1].frame().size.height
        with_pointer = (
            nw.NoticeController.alloc().init().build(
                ns.message_for(note), ns.POINTER_SENTENCE
            )
        ).frame().size.height
        self.assertGreater(with_pointer, without)

    def test_the_buttons_match_the_verdict(self) -> None:
        note = notice()
        _, panel = self.build(note)
        titles = [
            str(view.title())
            for view in panel.contentView().subviews()
            if isinstance(view, AppKit.NSButton)
        ]
        self.assertEqual(sorted(titles), sorted(ns.buttons_for(note)))

    def test_a_notice_with_no_evidence_offers_only_close(self) -> None:
        bare = cs.Notice(
            verdict=cs.Verdict.UNIDENTIFIED,
            headline="Something else holds ImageView's lock",
            body="No usable pid was recorded.",
        )
        _, panel = self.build(bare)
        titles = [
            str(view.title())
            for view in panel.contentView().subviews()
            if isinstance(view, AppKit.NSButton)
        ]
        self.assertEqual(titles, [ns.CLOSE_BUTTON])

    def test_every_verdict_builds(self) -> None:
        """Eight verdicts, seven messages, and one of them is empty-ish.
        A layout that divides by zero on the shortest body would only
        ever be found by a user in the worst possible mood."""
        for verdict in cs.Verdict:
            with self.subTest(verdict.value):
                _, panel = self.build(notice(verdict))
                self.assertGreater(panel.frame().size.height, 0.0)


@unittest.skipUnless(AQUA, NOT_AQUA)
class PlacementTests(unittest.TestCase):
    """Placement against this machine's real screens."""

    @classmethod
    def setUpClass(cls) -> None:
        app = AppKit.NSApplication.sharedApplication()
        app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
        cls.screens = nw.screen_records()

    def build(self):
        controller = nw.NoticeController.alloc().init()
        controller.build(ns.message_for(notice()))
        return controller

    def icon_on(self, screen: ns.Screen) -> cs.WindowFact:
        """A 34x24 status item in the middle of `screen`'s menu bar,
        expressed the way the window server would express it.

        The middle, not an edge: an edge icon is clamped, and a clamped
        placement is a different property — pinned headlessly in
        `ui/test_notice_state.py` against known rectangles rather than
        against whatever this machine's Dock happens to be doing.
        """
        height = nw.zero_screen_height(self.screens)
        return cs.WindowFact(
            layer=25,
            on_screen=True,
            x=screen.visible_frame.center_x - 17.0,
            y=height - screen.frame.max_y,
            width=34.0,
            height=24.0,
        )

    def test_this_machine_really_has_a_zero_origin_screen(self) -> None:
        """Named for what it checks. It used to be called
        "the zero origin screen is found" and only asserted that the
        height was positive — the name claimed selection, the body checked
        arithmetic. Selection is pinned headlessly in `ConversionTests`;
        what an Aqua session can add is that the premise holds on real
        hardware."""
        self.assertTrue(self.screens, "an Aqua session has at least one screen")
        self.assertTrue(
            any(s.frame.x == 0.0 and s.frame.y == 0.0 for s in self.screens),
            "no attached screen is at the Cocoa origin — the conversion has no anchor",
        )

    def test_the_panel_lands_under_the_icon(self) -> None:
        controller = self.build()
        icon = self.icon_on(self.screens[0])
        placement = controller.place([icon], [])
        self.assertIsNotNone(placement)
        self.assertTrue(placement.anchored)
        panel = controller._panel
        centre = panel.frame().origin.x + panel.frame().size.width / 2.0
        rect = ns.rect_of(icon, nw.zero_screen_height(self.screens))
        # A point of tolerance: AppKit rounds a window origin to whole
        # points, so a half-point anchor lands a half-point off. The
        # exact arithmetic is pinned headlessly in
        # `ui/test_notice_state.py`; what this asserts is that the panel
        # AppKit actually built ended up under the icon.
        self.assertAlmostEqual(centre, rect.center_x, delta=1.0)

    def test_it_is_never_placed_on_the_picture_display(self) -> None:
        """🔴 The failure mode this whole placement rule exists for: a
        notice on a 2.1-inch round panel, under the slideshow, with half
        of it outside the circular mask.

        The picture window here is **synthetic** — a rectangle covering a
        real second screen — rather than read off the live display agent,
        so that the assertion is about the rule and not about whatever
        happens to be running.
        """
        if len(self.screens) < 2:
            self.skipTest(
                "needs a second attached screen to stand in for the picture "
                "display: this run did NOT cover the exclusion rule"
            )
        target = self.screens[1]
        height = nw.zero_screen_height(self.screens)
        picture = cs.WindowFact(
            layer=1000,
            on_screen=True,
            x=target.frame.x,
            y=height - target.frame.max_y,
            width=target.frame.width,
            height=target.frame.height,
        )
        controller = self.build()
        placement = controller.place([self.icon_on(target)], [picture])
        self.assertIsNotNone(placement)
        self.assertNotEqual(placement.screen_index, 1)
        panel = controller._panel
        self.assertFalse(
            target.frame.contains_point(
                panel.frame().origin.x + panel.frame().size.width / 2.0,
                panel.frame().origin.y + panel.frame().size.height / 2.0,
            ),
            "the notice was placed on the picture display",
        )

    def test_no_icon_still_places_the_panel(self) -> None:
        controller = self.build()
        placement = controller.place([], [])
        self.assertIsNotNone(placement)
        self.assertFalse(placement.anchored)


@unittest.skipUnless(AQUA, NOT_AQUA)
class PointerWiringTests(unittest.TestCase):
    """`prepare()` — build, place, and point, against real screens.

    🔴 **This class exists because a mutation run found the wiring
    unprotected.** `pointer_sentence` was pinned branch by branch in
    `ui/test_notice_state.py`, and the code that *calls* it lived entirely
    inside `show()` — which blocks on a run loop and so can never be
    reached by a test. Deleting the call, or asking for the pointer and
    then not rendering it, left the whole suite green. Splitting
    `prepare()` out is what made those two mutations killable.
    """

    @classmethod
    def setUpClass(cls) -> None:
        app = AppKit.NSApplication.sharedApplication()
        app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
        cls.screens = nw.screen_records()

    def fields(self, controller) -> list[str]:
        return [
            str(view.stringValue())
            for view in controller._panel.contentView().subviews()
            if isinstance(view, AppKit.NSTextField)
        ]

    def icon_at(self, x: float) -> cs.WindowFact:
        return cs.WindowFact(
            layer=25,
            on_screen=True,
            x=x,
            y=nw.zero_screen_height(self.screens) - self.screens[0].frame.max_y,
            width=34.0,
            height=24.0,
        )

    def test_an_anchored_panel_carries_the_pointer(self) -> None:
        icon = self.icon_at(self.screens[0].visible_frame.center_x - 17.0)
        controller, placement, pointer = nw.prepare(notice(), [icon], [])
        self.assertIsNotNone(controller)
        self.assertTrue(placement.anchored)
        self.assertFalse(placement.clamped)
        self.assertEqual(pointer, ns.POINTER_SENTENCE)
        self.assertIn(ns.POINTER_SENTENCE, self.fields(controller))

    def test_a_clamped_panel_does_not(self) -> None:
        """🔴 The failure to avoid: text asserting a spatial fact the code
        did not establish. A panel pulled back from the screen edge is no
        longer centred under the icon."""
        icon = self.icon_at(self.screens[0].visible_frame.max_x - 34.0)
        controller, placement, pointer = nw.prepare(notice(), [icon], [])
        self.assertTrue(placement.clamped)
        self.assertIsNone(pointer)
        self.assertNotIn(ns.POINTER_SENTENCE, self.fields(controller))

    def test_no_icon_means_no_pointer(self) -> None:
        controller, placement, pointer = nw.prepare(notice(), [], [])
        self.assertFalse(placement.anchored)
        self.assertIsNone(pointer)
        self.assertEqual(len(self.fields(controller)), 2)

    def test_an_icon_on_the_picture_display_means_no_pointer(self) -> None:
        """The panel is on the desk monitor; the icon is buried under a
        slideshow on a 2.1-inch round panel."""
        if len(self.screens) < 2:
            self.skipTest(
                "needs a second attached screen to stand in for the picture "
                "display: this run did NOT cover the pointer's exclusion rule"
            )
        target = self.screens[1]
        height = nw.zero_screen_height(self.screens)
        picture = cs.WindowFact(
            layer=1000,
            on_screen=True,
            x=target.frame.x,
            y=height - target.frame.max_y,
            width=target.frame.width,
            height=target.frame.height,
        )
        icon = cs.WindowFact(
            layer=25,
            on_screen=True,
            x=target.visible_frame.center_x - 17.0,
            y=height - target.frame.max_y,
            width=34.0,
            height=24.0,
        )
        controller, placement, pointer = nw.prepare(notice(), [icon], [picture])
        self.assertNotEqual(placement.screen_index, 1)
        self.assertFalse(placement.anchored)
        self.assertIsNone(pointer)
        self.assertNotIn(ns.POINTER_SENTENCE, self.fields(controller))

    def test_the_returned_pointer_always_matches_the_returned_placement(self) -> None:
        """The invariant, on the ordinary path where nothing moves."""
        icon = self.icon_at(self.screens[0].visible_frame.center_x - 17.0)
        _, placement, pointer = nw.prepare(notice(), [icon], [])
        self.assertEqual(ns.pointer_sentence(placement), pointer)

    def test_the_screens_moving_between_the_passes_withdraws_the_claim(self) -> None:
        """🔴 The test this replaced asserted the right invariant and
        **varied nothing between the passes**, so it could not fail —
        subtler than an assertion that is simply wrong, and it looks more
        convincing in review. Deleting the second `place()` call outright
        survived the whole suite.

        So vary the world. The screens are listed once per `place()`, and
        here the second listing comes back empty — a display asleep, the
        View's USB-C nudged, a lid closed. `place()` then centres the
        window itself and returns `None`, and the pointer decided from
        pass 1 no longer describes where the panel is. `prepare()` must
        retreat to the panel that claims nothing, and the claim must not
        be on screen."""
        icon = self.icon_at(self.screens[0].visible_frame.center_x - 17.0)
        real = nw.screen_records
        listings = []

        def vanishing():
            listings.append(1)
            return real() if len(listings) == 1 else ()

        nw.screen_records = vanishing
        try:
            controller, placement, pointer = nw.prepare(notice(), [icon], [])
        finally:
            nw.screen_records = real

        self.assertGreater(len(listings), 1, "the second pass never re-placed")
        self.assertIsNone(pointer, "a claim survived the screens moving")
        self.assertEqual(ns.pointer_sentence(placement), pointer)
        self.assertNotIn(ns.POINTER_SENTENCE, self.fields(controller))

    def test_4a_s_words_survive_the_second_pass_byte_identical(self) -> None:
        note = notice()
        icon = self.icon_at(self.screens[0].visible_frame.center_x - 17.0)
        controller, _, _ = nw.prepare(note, [icon], [])
        shown = self.fields(controller)
        self.assertIn(note.headline, shown)
        self.assertIn(note.body, shown)


@unittest.skipUnless(AQUA, NOT_AQUA)
class DismissalTests(unittest.TestCase):
    """Every path that ends the notice routes through one teardown.

    `present()` is deliberately not called — see the module docstring —
    so there is no timer, no monitor and no visible window here. What is
    asserted is that each entry point reaches `_finish` and that `_finish`
    latches, because a second teardown removing an already-removed event
    monitor is the kind of thing that raises inside an AppKit callback.
    """

    @classmethod
    def setUpClass(cls) -> None:
        app = AppKit.NSApplication.sharedApplication()
        app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

    def controller(self):
        controller = nw.NoticeController.alloc().init()
        controller.build(ns.message_for(notice()))
        return controller

    def test_the_close_button_finishes(self) -> None:
        controller = self.controller()
        controller.closeNotice_(None)
        self.assertTrue(controller._finished)

    def test_the_title_bar_close_finishes(self) -> None:
        controller = self.controller()
        controller.windowWillClose_(None)
        self.assertTrue(controller._finished)

    def test_the_auto_dismiss_timer_finishes(self) -> None:
        controller = self.controller()
        controller.autoDismiss_(None)
        self.assertTrue(controller._finished)

    def test_finishing_twice_is_harmless(self) -> None:
        controller = self.controller()
        controller.closeNotice_(None)
        controller.windowWillClose_(None)
        self.assertTrue(controller._finished)


if __name__ == "__main__":
    unittest.main()
