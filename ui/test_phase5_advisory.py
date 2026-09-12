"""Phase 5 — the modals that had no window to open in front of.

Four `NSAlert.runModal()` sites are gone: the two in `menubar.py`
(`about_`, and both `startDisplay_` failures, all through one helper),
the two in `first_run_window.py`, and `calibrate_window.start()`'s. What
replaced them is **not one mechanism**, and the difference is the whole
content of this file:

* **No window of ours on screen → a non-modal `NSPanel`**, the one Phase
  4b built. `menubar._advise` and `calibrate_window._advise`.
* **A window of ours already on screen → a sheet on it.**
  `first_run_window._advise`, the same conversion Phase 3 made to that
  file's folder picker, for the same reasons plus one this file records:
  a sheet is window-modal, so the flow is unchanged, and a 360pt panel
  centred on screen would land on top of the centred first-run window and
  cover the row its first message names.

🔴 **THE ONE THAT COULD HAVE SHIPPED AND QUIT THE APP.** `NoticeController._finish`
stopped the application's run loop unconditionally, which was right while
its only caller was a process whose run loop existed solely to hold that
one window up. In the live menu bar `app.run()` **is** the application, so
an unconditional stop turns "close the About box" into "quit ImageView".
`stops_app` is the fix and `RunLoopOwnershipTests` is what holds it: both
branches, driven from both entry points, with the stop replaced by a
recorder — because a test that let the real call through would stop the
run loop of the process running the test.

**What is not covered here.** `present()` orders a real window front and
installs a global key monitor; nothing below calls it, exactly as
`test_notice_window.py` does not. The window-server assertions skip
loudly outside an Aqua session, and a green CI badge has not seen one of
them.
"""

from __future__ import annotations

import ast
import contextlib
import io
import unittest
from pathlib import Path

import AppKit
import objc

from ui import calibrate_window
from ui import contention_state as cs
from ui import first_run_state as fr
from ui import first_run_window
from ui import menubar
from ui import notice_state as ns
from ui import notice_window as nw
from ui.test_notice_window import AQUA, NOT_AQUA


def attributes(module) -> set[str]:
    """Every attribute name a module's source actually *uses*.

    Docstrings and comments are not `Attribute` nodes, so a file that
    explains at length why it no longer calls `runModal()` does not
    thereby look like it calls it — the exact false positive a `grep`
    produces here, and these four files are now full of that prose.

    ⚠️ **KNOWN BLIND SPOT, and it is the same "passes because of a file
    property" family these tests exist to escape.** Only *attribute*
    access is seen. `from AppKit import NSAlert` followed by a bare
    `NSAlert.alloc()` is an `ast.Name` plus one attribute, so
    `NSAlert.alloc().init().runModal()` would be caught by the `runModal`
    check but a bare re-import would slip the `NSAlert` one. No file in
    this package imports AppKit symbols that way today — every one does
    `import AppKit` — which is why this is a note rather than a second
    scanner. If that convention ever changes, this stops being a rule.
    """
    tree = ast.parse(Path(module.__file__).read_text())
    return {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}


def attributes_of_method(module, class_name: str, method: str) -> set[str]:
    """The same, narrowed to one method.

    `calibrate_window` deliberately still runs three modals, so a
    file-level assertion there would say nothing. The claim Phase 5 makes
    about that file is about **`start()`** specifically.
    """
    tree = ast.parse(Path(module.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for child in node.body:
                if isinstance(child, ast.FunctionDef) and child.name == method:
                    return {
                        inner.attr
                        for inner in ast.walk(child)
                        if isinstance(inner, ast.Attribute)
                    }
    raise AssertionError(f"{class_name}.{method} is no longer in {module.__name__}")


MENUBAR = attributes(menubar)
FIRST_RUN = attributes(first_run_window)
CALIBRATE = attributes(calibrate_window)
CALIBRATE_START = attributes_of_method(calibrate_window, "CalibrateController", "start")


class ConvertedSiteTests(unittest.TestCase):
    """🔴 The sites that had nothing on screen to own a modal.

    The claim is *about the source*: the **absence of a call** is the
    property, so reading the syntax tree measures the thing rather than
    standing in for it. These run everywhere, including CI.
    """

    def test_the_menu_bar_runs_no_modal_at_all(self) -> None:
        """An accessory app has no window of its own. If
        `activateIgnoringOtherApps_` fails — Apple reports it
        intermittently does on macOS 26 — a modal opened from here has
        nothing to open in front of, so it opens **behind everything**:
        invisible, unanswerable, and holding the run loop forever."""
        self.assertNotIn("runModal", MENUBAR)

    def test_the_menu_bar_never_asks_to_be_activated(self) -> None:
        self.assertNotIn("activateIgnoringOtherApps_", MENUBAR)

    def test_the_menu_bar_builds_no_alert(self) -> None:
        """Not merely "does not run one". `NSAlert.alloc()` here at all
        would mean a modal was reintroduced and merely not run yet."""
        self.assertNotIn("NSAlert", MENUBAR)

    def test_first_run_runs_no_modal(self) -> None:
        """Its alert had **no** preceding activation at all, so it could
        open behind another window even on a build where activation
        works — in the app's first two minutes."""
        self.assertNotIn("runModal", FIRST_RUN)

    def test_first_run_presents_its_alert_as_a_sheet(self) -> None:
        """A sheet cannot be lost behind a window, spins no nested run
        loop, and is window-modal, so the flow is what it was."""
        self.assertIn("beginSheetModalForWindow_completionHandler_", FIRST_RUN)

    def test_calibrate_start_runs_no_modal(self) -> None:
        """`start()`'s "the View isn't connected" alert fires **before**
        `_overlay` and `_window` are built and then returns False, at
        which point `menubar.calibrate_` drops the controller. Nothing is
        on screen — the same hazard class as the menu bar's, and the plan
        puts this file wholly in the left-alone bucket.

        🔴 **Asserted on the helper `start()` calls, not on `runModal`,
        and the mutation matrix is what forced that.** `runModal` lives
        inside `_alert`'s body, not inside `start()`, so
        `assertNotIn("runModal", CALIBRATE_START)` — which is what this
        test said first — **survived** switching the call back to
        `self._alert(`. The property is which helper this path reaches.
        """
        self.assertNotIn("runModal", CALIBRATE_START)
        self.assertNotIn("_alert", CALIBRATE_START)
        self.assertIn("_advise", CALIBRATE_START)

    def test_calibrate_keeps_the_modals_that_have_a_window(self) -> None:
        """🔴 The positive control, and the record of where the line was
        drawn. Three alerts in this file stay modal on purpose: two owned
        by a window already on screen, and `_confirm_close`, whose
        **return value drives behaviour** — Save, Discard or Cancel — so
        it has to block until it is answered.

        Without this, the test above passes for the uninteresting reason
        that the scan found nothing anywhere."""
        self.assertIn("runModal", CALIBRATE)

    def test_the_scan_can_see_calls_that_are_still_there(self) -> None:
        """A scan looking in the wrong place reports a clean
        absence. Each of these is present in the file it names, so a run
        that answers all three has actually parsed all three modules."""
        self.assertIn("statusItemWithLength_", MENUBAR)
        self.assertIn("activateIgnoringOtherApps_", FIRST_RUN)
        self.assertIn("activateIgnoringOtherApps_", CALIBRATE)


class AdvisoryMessageTests(unittest.TestCase):
    """`notice_state.advisory`, headlessly. Nothing here needs AppKit."""

    def test_the_words_reach_the_message_unedited(self) -> None:
        message = ns.advisory("Couldn't start showing pictures.", "Login Items.")
        self.assertEqual(message.headline, "Couldn't start showing pictures.")
        self.assertEqual(message.body, "Login Items.")

    def test_an_advisory_never_dismisses_itself(self) -> None:
        """🔴 An advisory is always the app saying something it was asked
        to say. A window that deletes that after eight seconds leaves the
        reader knowing something was wrong and unable to read what."""
        self.assertIsNone(ns.advisory("Head", "Body").dismiss_after_s)

    def test_the_one_notice_that_does_dismiss_itself_still_does(self) -> None:
        """🔴 The other half, and the reason the test above is not a
        tautology: the same field is 8.0 for the one verdict entitled to
        it, so "always None" is a statement about advisories rather than
        about the field. Written as a literal — the duplication is the
        point, because a test that compares the constant to itself
        proves the constant equals itself."""
        serving = ns.message_for(_notice(cs.Verdict.SERVING_VISIBLE))
        self.assertEqual(serving.dismiss_after_s, 8.0)
        self.assertIsNone(ns.message_for(_notice(cs.Verdict.UNRESPONSIVE)).dismiss_after_s)

    def test_an_advisory_offers_only_close(self) -> None:
        """No `Copy details`: an advisory's evidence *is* the two strings
        on screen and they are selectable, so a button offering to copy
        what is already copyable would be furniture."""
        self.assertEqual(ns.advisory("Head", "Body").buttons, ("Close",))
        self.assertEqual(ns.advisory("Head", "Body").details, "")

    def test_a_wedge_notice_still_offers_copy_details(self) -> None:
        """The contrast that makes the test above mean something. This
        one carries a pid, a heartbeat age and a stacks path that the
        reader cannot retype off a screen."""
        self.assertEqual(
            ns.message_for(_notice(cs.Verdict.UNRESPONSIVE)).buttons,
            ("Copy details", "Close"),
        )

    def test_the_log_line_is_one_line_whatever_the_body_is(self) -> None:
        """🔴 `about_text` joins its paragraphs with blank lines, so a
        body written through verbatim would put a six-line entry in a log
        whose every other entry is one timestamped line — and everything
        that reads it, starting with a human scrolling, treats a line as
        an event."""
        message = ns.advisory("ImageView 1.1.6", "First para.\n\nSecond para.")
        self.assertEqual(
            ns.advisory_log_line(message),
            "notice: advisory on screen — ImageView 1.1.6 First para. Second para.",
        )

    def test_the_log_line_carries_the_words_not_just_the_headline(self) -> None:
        """The useful half of a `startDisplay_` failure is the body — it
        is what names Login Items."""
        line = ns.advisory_log_line(ns.advisory("Couldn't start.", "Login Items is blocking this."))
        self.assertIn("Login Items is blocking this.", line)

    def test_message_for_keeps_the_headline_and_the_body_apart(self) -> None:
        """🔴 Field by field, with two values that cannot be confused.

        `test_notice_window.test_the_words_are_4a_s_words_verbatim`
        gathers the panel's strings into a **set** and asserts both are
        in it, so a `message_for` that transposed headline and body would
        pass it — both words are still on screen. Assert the pairing, not
        the presence.
        """
        note = _notice()
        message = ns.message_for(note)
        self.assertEqual(message.headline, "ImageView is already running")
        self.assertEqual(message.body, "Its menu bar icon is on screen — click it.")

    def test_message_for_carries_the_clipboard_payload(self) -> None:
        """Without this the `Copy details` button is offered and copies
        an empty string — a button that works and does nothing."""
        details = ns.message_for(_notice()).details
        self.assertTrue(details.startswith("ImageView — a second copy did not start"))
        self.assertIn("holder pid: 4321", details)

    def test_an_advisory_carries_no_clipboard_payload(self) -> None:
        """The contrast: the same field, empty, because the words on
        screen are the whole of an advisory's evidence."""
        self.assertEqual(ns.advisory("Head", "Body").details, "")

    def test_the_about_body_really_does_contain_blank_lines(self) -> None:
        """Otherwise the collapse above is protecting against a shape
        this app never produces, and would survive being deleted."""
        _, body = __import__("ui.menubar_state", fromlist=["x"]).about_text("1.1.6")
        self.assertIn("\n\n", body)


def _notice(verdict=cs.Verdict.UNRESPONSIVE) -> cs.Notice:
    return cs.Notice(
        verdict=verdict,
        headline="ImageView is already running",
        body="Its menu bar icon is on screen — click it.",
        holder_pid=4321,
        display_pid=4300,
        heartbeat_age_s=1.0,
        window_layers=(25,),
        state_writable=True,
        stacks_path="/tmp/viewlab/ui.stacks.log",
    )


class _StopRecorder:
    """Stands in for `_stop_run_loop`, which a test must never let run:
    it stops the run loop of the process running the test."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


@unittest.skipUnless(AQUA, NOT_AQUA)
class RunLoopOwnershipTests(unittest.TestCase):
    """🔴 Whether dismissing a panel stops the application.

    Both entry points, both branches. The invariant is not "advisories do
    not stop the app" on its own — that would hold just as well if
    nothing ever stopped it, and the contended-launch process would then
    sit there forever after its window closed.
    """

    @classmethod
    def setUpClass(cls) -> None:
        app = AppKit.NSApplication.sharedApplication()
        app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

    def tearDown(self) -> None:
        for held in nw.on_screen():
            held.dismiss()

    def test_an_advisory_does_not_own_the_run_loop(self) -> None:
        controller = nw.prepare_advisory("Head", "Body")
        self.assertIsNotNone(controller)
        self.assertFalse(controller.stops_app())

    def test_a_contended_launch_notice_does_own_it(self) -> None:
        controller, _, _ = nw.prepare(_notice())
        self.assertIsNotNone(controller)
        self.assertTrue(controller.stops_app())

    def test_the_second_panel_prepare_builds_owns_it_too(self) -> None:
        """🔴 `prepare()` builds **two** controllers — a plain one, and a
        second carrying the spatial pointer when the placement earned it
        — and returns whichever the geometry supports. The test above
        only ever reaches the plain one, because a notice with no icons
        cannot anchor. This one supplies an icon so the *pointed*
        controller is the one returned; without it, half of the flag's
        wiring is untested and a `prepare()` that set it on one
        controller and not the other would look correct."""
        screens = nw.screen_records()
        self.assertTrue(screens, "an Aqua session has at least one screen")
        screen = screens[0]
        icon = cs.WindowFact(
            layer=25,
            on_screen=True,
            x=screen.visible_frame.center_x - 17.0,
            y=nw.zero_screen_height(screens) - screen.frame.max_y,
            width=34.0,
            height=24.0,
        )
        controller, placement, pointer = nw.prepare(_notice(), [icon], [])
        self.assertIsNotNone(placement)
        self.assertTrue(placement.anchored, "the icon did not anchor; no pointed panel was built")
        self.assertEqual(pointer, ns.POINTER_SENTENCE)
        self.assertTrue(controller.stops_app())

    def test_dismissing_an_advisory_never_reaches_the_stop(self) -> None:
        """🔴 The one that would have quit ImageView when the user closed
        the About box."""
        controller = nw.prepare_advisory("Head", "Body")
        recorder = _StopRecorder()
        controller._stop_run_loop = recorder
        controller.dismiss()
        self.assertEqual(recorder.calls, 0)

    def test_dismissing_a_contended_launch_notice_does_reach_it(self) -> None:
        """The half that makes the other half a statement about the flag
        rather than about `_finish` having no stop in it at all."""
        controller, _, _ = nw.prepare(_notice())
        recorder = _StopRecorder()
        controller._stop_run_loop = recorder
        controller.dismiss()
        self.assertEqual(recorder.calls, 1)

    def test_the_recorder_really_does_replace_the_call(self) -> None:
        """The two tests above rest on an instance attribute shadowing a
        `python_method`. If it did not, the first would pass for the
        wrong reason and the second would stop this test run's run loop.
        """
        controller = nw.prepare_advisory("Head", "Body")
        recorder = _StopRecorder()
        controller._stop_run_loop = recorder
        controller._stop_run_loop()
        self.assertEqual(recorder.calls, 1)
        controller.dismiss()


@unittest.skipUnless(AQUA, NOT_AQUA)
class AdvisoryLifetimeTests(unittest.TestCase):
    """🔴 Nothing else retains a controller.

    `menubar.py` records this defect class for `CalibrateController`: one
    collected out from under its own key monitor takes its window with
    it. `calibrate_window.start()` structurally cannot hold an ivar — it
    shows its advisory and returns False, and its caller drops it — so
    the hold lives here.
    """

    @classmethod
    def setUpClass(cls) -> None:
        AppKit.NSApplication.sharedApplication()

    def tearDown(self) -> None:
        for held in nw.on_screen():
            held.dismiss()

    def test_an_advisory_is_held_until_it_is_dismissed(self) -> None:
        controller = nw.prepare_advisory("Head", "Body")
        self.assertIn(controller, nw.on_screen())
        controller.dismiss()
        self.assertNotIn(controller, nw.on_screen())

    def test_two_advisories_are_held_separately(self) -> None:
        """Dismissing one must not release the other: the menu bar has
        two slots and either may be up while the other is."""
        first = nw.prepare_advisory("First", "Body")
        second = nw.prepare_advisory("Second", "Body")
        first.dismiss()
        self.assertNotIn(first, nw.on_screen())
        self.assertIn(second, nw.on_screen())

    def test_dismissing_twice_is_harmless(self) -> None:
        controller = nw.prepare_advisory("Head", "Body")
        controller.dismiss()
        controller.dismiss()
        self.assertEqual(len(nw.on_screen()), 0)

    def test_the_contended_launch_notice_is_not_held_here(self) -> None:
        """`show()` holds its controller in a local for the whole life of
        the run loop it starts, so the hold would be a second owner with
        nothing to own. Asserting it keeps `prepare()` from quietly
        acquiring one and leaking a controller per contended launch."""
        controller, _, _ = nw.prepare(_notice())
        self.assertNotIn(controller, nw.on_screen())

    def test_on_dismiss_runs_once_and_then_never_again(self) -> None:
        calls = []
        controller = nw.prepare_advisory("Head", "Body", lambda: calls.append(1))
        self.assertEqual(calls, [])
        controller.dismiss()
        controller.dismiss()
        self.assertEqual(calls, [1])


@unittest.skipUnless(AQUA, NOT_AQUA)
class AdvisoryPanelTests(unittest.TestCase):
    """The window an advisory actually builds."""

    @classmethod
    def setUpClass(cls) -> None:
        app = AppKit.NSApplication.sharedApplication()
        app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

    def tearDown(self) -> None:
        for held in nw.on_screen():
            held.dismiss()

    def panel(self, headline="Couldn't start showing pictures.", body="Login Items."):
        controller = nw.prepare_advisory(headline, body)
        self.assertIsNotNone(controller, "the advisory panel could not be built")
        return controller, controller._panel

    def test_it_does_not_hide_when_the_user_clicks_another_app(self) -> None:
        """The measured default is True, and the message most likely to
        be on screen here tells the reader to go and look at System
        Settings — the click that would take it away."""
        _, panel = self.panel()
        self.assertFalse(panel.hidesOnDeactivate())

    def test_it_does_not_take_focus(self) -> None:
        _, panel = self.panel()
        self.assertTrue(panel.becomesKeyOnlyIfNeeded())

    def test_it_stays_at_the_floating_default(self) -> None:
        _, panel = self.panel()
        self.assertEqual(panel.level(), AppKit.NSFloatingWindowLevel)

    def test_it_shows_the_two_strings_and_nothing_else(self) -> None:
        """🔴 No third field. `notice_state.pointer_sentence` is never
        consulted on this path — an advisory is shown by the very process
        it is about, so there is no other app's icon to point at, and the
        spatial claim is structurally unavailable rather than
        conditionally withheld."""
        _, panel = self.panel("A headline.", "A body.")
        shown = [
            str(view.stringValue())
            for view in panel.contentView().subviews()
            if isinstance(view, AppKit.NSTextField)
        ]
        self.assertEqual(sorted(shown), ["A body.", "A headline."])
        self.assertNotIn(ns.POINTER_SENTENCE, shown)

    def test_it_offers_one_button_and_it_says_close(self) -> None:
        _, panel = self.panel()
        titles = [
            str(view.title())
            for view in panel.contentView().subviews()
            if isinstance(view, AppKit.NSButton)
        ]
        self.assertEqual(titles, ["Close"])

    def test_the_text_is_selectable(self) -> None:
        """The About box's whole content is a version number someone is
        about to paste into a bug report."""
        _, panel = self.panel()
        for view in panel.contentView().subviews():
            if isinstance(view, AppKit.NSTextField):
                self.assertTrue(view.isSelectable())
                self.assertFalse(view.isEditable())

    def test_says_compares_the_words_and_not_merely_the_slot(self) -> None:
        """🔴 `startDisplay_` has two distinct failures. A user who hits
        "move it to Applications", moves it, and tries again must not be
        re-shown last time's message."""
        controller, _ = self.panel("Head", "Body")
        self.assertTrue(controller.says("Head", "Body"))
        self.assertFalse(controller.says("Head", "Other body"))
        self.assertFalse(controller.says("Other head", "Body"))

    def test_it_is_centred_the_way_appkit_centres_a_window(self) -> None:
        """🔴 `center()` is the whole of an advisory's placement, and
        deleting it looks like deleting a cosmetic line. A panel built
        and never positioned sits at the bottom-left of the zero-origin
        screen with its title bar down by the Dock.

        🔴 **This test was vacuous in its first form and the mutation
        matrix is what said so.** It asserted the origin was not exactly
        `(0, 0)` and that the panel's centre landed on some attached
        screen — and MEASURED on this machine, an *uncentred* panel
        satisfies both: AppKit cascades it to `(63, 0)`, whose centre is
        comfortably inside the main screen. Deleting `center()` passed.

        The oracle here is a second panel built the same way and centred
        explicitly. It comes from **AppKit**, not from the code under
        test — all `prepare_advisory` contributes is whether it made the
        call — so the two origins agree only if it did.
        """
        _, panel = self.panel("Head", "Body")
        reference = nw.NoticeController.alloc().init()
        reference.build(ns.advisory("Head", "Body"))
        reference.center()
        self.assertEqual(
            (float(panel.frame().origin.x), float(panel.frame().origin.y)),
            (
                float(reference._panel.frame().origin.x),
                float(reference._panel.frame().origin.y),
            ),
        )

    def test_a_panel_that_is_never_centred_lands_somewhere_else(self) -> None:
        """The control for the test above: it would hold just as well if
        `center()` did nothing at all on this hardware."""
        uncentred = nw.NoticeController.alloc().init()
        uncentred.build(ns.advisory("Head", "Body"))
        before = (
            float(uncentred._panel.frame().origin.x),
            float(uncentred._panel.frame().origin.y),
        )
        uncentred.center()
        after = (
            float(uncentred._panel.frame().origin.x),
            float(uncentred._panel.frame().origin.y),
        )
        self.assertNotEqual(before, after)

    def test_the_advisory_is_logged(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()) as err:
            nw.prepare_advisory("A headline.", "A body.")
        self.assertIn("notice: advisory on screen — A headline. A body.", err.getvalue())


class _FakeAdvisory:
    """A controller stand-in with the three calls `reuse_slot` makes
    recorded instead of performed. Registered straight into `_SLOTS`, so
    nothing here builds an `NSPanel` and none of it needs Aqua."""

    def __init__(self, headline: str, body: str) -> None:
        self.headline = headline
        self.body = body
        self.fronted = 0
        self.dismissed = 0

    def says(self, headline: str, body: str) -> bool:
        return self.headline == headline and self.body == body

    def bring_forward(self) -> None:
        self.fronted += 1

    def dismiss(self) -> None:
        self.dismissed += 1
        nw._SLOTS.pop(nw.SLOT_UNDER_TEST, None)


class ReuseSlotTests(unittest.TestCase):
    """\U0001f534 A second click must not stack a second identical panel.

    It could not happen while these were `runModal()` — a modal blocked
    the menu — and it is the one thing the conversion gives away. This is
    the **only** implementation of the rule: `menubar._advise` and
    `calibrate_window._advise` both reach it, which is the point, because
    two implementations of one question is how this app once answered
    the same question two different ways and believed the wrong one.
    """

    def setUp(self) -> None:
        self.saved = dict(nw._SLOTS)
        nw._SLOTS.clear()
        self.addCleanup(lambda: (nw._SLOTS.clear(), nw._SLOTS.update(self.saved)))

    def register(self, headline="Head", body="Body") -> "_FakeAdvisory":
        made = _FakeAdvisory(headline, body)
        nw._SLOTS[nw.SLOT_UNDER_TEST] = made
        return made

    def test_an_empty_slot_asks_for_a_new_panel(self) -> None:
        self.assertIsNone(nw.reuse_slot(nw.SLOT_UNDER_TEST, "Head", "Body"))

    def test_no_slot_at_all_always_asks_for_a_new_panel(self) -> None:
        """A caller that passes no slot opts out of the rule entirely and
        must not pick up another site's panel — `calibrate_window.main()`
        and any future one-shot caller.

        ⚠️ **This pins the contract, not the early return that implements
        it.** Two guards produce it — this one and `prepare_advisory`'s
        registration guard — and either alone is sufficient, so removing
        `if slot is None` leaves this test green. That mutation is
        labelled `[EQUIVALENT]` in `tools/mutate_phase5.py` with the
        argument; `test_an_unslotted_advisory_is_not` below is what keeps
        the premise honest by pinning the other guard.
        """
        self.register()
        self.assertIsNone(nw.reuse_slot(None, "Head", "Body"))

    def test_the_same_words_re_show_the_panel_already_up(self) -> None:
        held = self.register()
        for _ in range(3):
            self.assertIs(nw.reuse_slot(nw.SLOT_UNDER_TEST, "Head", "Body"), held)
        self.assertEqual(held.fronted, 3)
        self.assertEqual(held.dismissed, 0)

    def test_different_words_dismiss_it_and_ask_for_a_new_one(self) -> None:
        """\U0001f534 The half that matters for `startDisplay_`, whose two
        failures say different things. Re-showing the old panel would put
        a stale diagnosis on screen."""
        held = self.register("Couldn't start showing pictures.", "Move it.")
        answer = nw.reuse_slot(
            nw.SLOT_UNDER_TEST, "Couldn't start showing pictures.", "Login Items."
        )
        self.assertIsNone(answer)
        self.assertEqual(held.dismissed, 1)
        self.assertEqual(held.fronted, 0)

    def test_a_different_headline_alone_is_enough_to_replace_it(self) -> None:
        held = self.register("Head", "Body")
        self.assertIsNone(nw.reuse_slot(nw.SLOT_UNDER_TEST, "Other head", "Body"))
        self.assertEqual(held.dismissed, 1)

    def test_another_slot_is_not_this_one(self) -> None:
        """The two menu bar slots may be on screen together, and
        `calibrate`'s is a third."""
        held = self.register()
        self.assertIsNone(nw.reuse_slot("some.other.slot", "Head", "Body"))
        self.assertEqual(held.dismissed, 0)
        self.assertEqual(held.fronted, 0)


@unittest.skipUnless(AQUA, NOT_AQUA)
class SlotRegistrationTests(unittest.TestCase):
    """The table entry itself, against real controllers."""

    @classmethod
    def setUpClass(cls) -> None:
        AppKit.NSApplication.sharedApplication()

    def tearDown(self) -> None:
        for held in nw.on_screen():
            held.dismiss()

    def test_a_slotted_advisory_is_in_the_table(self) -> None:
        controller = nw.prepare_advisory("Head", "Body", None, nw.SLOT_UNDER_TEST)
        self.assertIs(nw.slots().get(nw.SLOT_UNDER_TEST), controller)
        self.assertEqual(controller.slot(), nw.SLOT_UNDER_TEST)

    def test_an_unslotted_advisory_is_not(self) -> None:
        """`calibrate_window.main()` and any future one-shot caller. The
        table must not grow a `None` key."""
        controller = nw.prepare_advisory("Head", "Body")
        self.assertNotIn(None, nw.slots())
        self.assertIsNone(controller.slot())

    def test_dismissal_frees_the_slot(self) -> None:
        """Without this the table points at a window that is gone, and
        every later click re-fronts nothing."""
        controller = nw.prepare_advisory("Head", "Body", None, nw.SLOT_UNDER_TEST)
        controller.dismiss()
        self.assertNotIn(nw.SLOT_UNDER_TEST, nw.slots())

    def test_a_replaced_panel_does_not_free_its_successor_s_slot(self) -> None:
        """\U0001f534 `_finish` clears the slot only when the table still
        points at *this* controller. Dismissing the old panel after the
        new one has registered would otherwise empty the slot the new one
        just took, and the click after that would stack."""
        first = nw.prepare_advisory("Head", "Body", None, nw.SLOT_UNDER_TEST)
        second = nw.prepare_advisory("Other", "Body", None, nw.SLOT_UNDER_TEST)
        self.assertIs(nw.slots().get(nw.SLOT_UNDER_TEST), second)
        first.dismiss()
        self.assertIs(nw.slots().get(nw.SLOT_UNDER_TEST), second)


@unittest.skipUnless(AQUA, NOT_AQUA)
class AdvisoryHoldTests(unittest.TestCase):
    """\U0001f534 The hold must survive a failure to write the log.

    PROVEN by execution rather than reasoned: `prepare_advisory` appends
    to `_ON_SCREEN` and then logs. With the log write raising,
    `advise()` honoured its never-raises contract and returned None —
    and `controller` never bound, so its recovery had nothing to dismiss
    and the hold was permanent. A closed fd, a broken pipe or a full log
    volume is all it takes.
    """

    @classmethod
    def setUpClass(cls) -> None:
        AppKit.NSApplication.sharedApplication()

    def tearDown(self) -> None:
        for held in nw.on_screen():
            held.dismiss()

    def test_a_log_write_that_raises_does_not_leak_the_hold(self) -> None:
        original = ns.advisory_log_line

        def explode(message):
            raise RuntimeError("the log volume is full")

        ns.advisory_log_line = explode
        self.addCleanup(setattr, ns, "advisory_log_line", original)
        before = len(nw.on_screen())
        controller = nw.prepare_advisory("Head", "Body")
        self.assertIsNotNone(controller, "the panel must still be built and returned")
        self.assertEqual(len(nw.on_screen()), before + 1)
        controller.dismiss()
        self.assertEqual(len(nw.on_screen()), before)

    def test_the_log_really_is_written_when_it_can_be(self) -> None:
        """The control. Without it the test above passes just as well if
        the log line were deleted outright."""
        with contextlib.redirect_stderr(io.StringIO()) as err:
            nw.prepare_advisory("A headline.", "A body.")
        self.assertIn("notice: advisory on screen — A headline. A body.", err.getvalue())


class MenuBarDelegationTests(unittest.TestCase):
    """`menubar._advise` is now a single call into the rule above. What
    is left to check is that it passes the right slot and the right
    words — the part a refactor can silently drop."""

    def setUp(self) -> None:
        self.calls = []
        original = nw.advise
        nw.advise = lambda headline, body, on_dismiss=None, slot=None: (
            self.calls.append((slot, headline, body)) or None
        )
        self.addCleanup(setattr, nw, "advise", original)
        self.controller = menubar.MenuBarController.alloc().init()

    def test_the_about_box_names_the_about_slot(self) -> None:
        self.controller.about_(None)
        self.assertEqual(len(self.calls), 1)
        slot, headline, _ = self.calls[0]
        self.assertEqual(slot, menubar.ADVISORY_ABOUT)
        self.assertIn("ImageView", headline)

    def test_the_two_menu_bar_slots_are_different(self) -> None:
        """One slot for both would make either message evict the other,
        and they can be on screen together."""
        self.assertNotEqual(menubar.ADVISORY_ABOUT, menubar.ADVISORY_START_FAILED)

    def test_every_slot_name_in_the_app_is_distinct(self) -> None:
        """\U0001f534 The table is module-level and shared. A collision
        would make two unrelated windows evict each other, and would look
        exactly like the stacking bug this rule exists to fix."""
        names = [
            menubar.ADVISORY_ABOUT,
            menubar.ADVISORY_START_FAILED,
            calibrate_window.ADVISORY_NO_VIEW,
        ]
        self.assertEqual(len(names), len(set(names)))

    def test_the_words_reach_the_rule_unedited(self) -> None:
        self.controller._advise("some.slot", "A headline.", "A body.")
        self.assertEqual(self.calls, [("some.slot", "A headline.", "A body.")])


class CalibrateAdvisoryTests(unittest.TestCase):
    """`calibrate_window._advise` — the site with no window and no owner."""

    def setUp(self) -> None:
        self.calls = []
        original = notice_window_in_calibrate().advise
        notice_window_in_calibrate().advise = (
            lambda headline, body, on_dismiss=None, slot=None: (
                self.calls.append((slot, headline, body)) or None
            )
        )
        self.addCleanup(
            setattr, notice_window_in_calibrate(), "advise", original
        )

    def test_it_goes_through_the_non_modal_panel_with_a_slot(self) -> None:
        """\U0001f534 MEASURED before the slot: four clicks on *Adjust the
        circle* with the View unplugged gave four panels. This controller
        is dropped by its caller the moment `start()` returns False, so
        it cannot remember anything itself."""
        controller = calibrate_window.CalibrateController.alloc().init()
        controller._advise("The View isn't connected.", "Plug it back in.")
        self.assertEqual(
            self.calls,
            [(calibrate_window.ADVISORY_NO_VIEW, "The View isn't connected.", "Plug it back in.")],
        )

    def test_it_holds_nothing_because_its_caller_is_about_to_be_dropped(self) -> None:
        """`start()` returns False straight after, and
        `menubar.calibrate_` then drops this controller with every ivar
        on it. `notice_window`'s module-level table is what remembers."""
        controller = calibrate_window.CalibrateController.alloc().init()
        before = set(vars(controller))
        controller._advise("Head", "Body")
        self.assertEqual(set(vars(controller)), before)


def notice_window_in_calibrate():
    return calibrate_window.notice_window


class _FakeWindow:
    def __init__(self) -> None:
        self.key = False
        self.ordered_out = False

    def isKeyWindow(self) -> bool:
        return self.key

    def orderOut_(self, sender) -> None:
        self.ordered_out = True

    def makeKeyAndOrderFront_(self, sender) -> None:
        pass


class _FakeAlert:
    """`NSAlert` with the sheet presentation captured. `runModal` raises
    rather than returning, so a reversion is a failure and not a pass."""

    last = None

    @classmethod
    def alloc(cls):
        return cls

    @classmethod
    def init(cls):
        made = object.__new__(cls)
        made.message = ""
        made.informative = ""
        made.buttons = []
        made.sheet_window = None
        made.handler = None
        cls.last = made
        return made

    def setMessageText_(self, text) -> None:
        self.message = text

    def setInformativeText_(self, text) -> None:
        self.informative = text

    def addButtonWithTitle_(self, title) -> None:
        self.buttons.append(title)

    def beginSheetModalForWindow_completionHandler_(self, window, handler) -> None:
        self.sheet_window = window
        self.handler = handler

    def runModal(self):  # pragma: no cover - the assertion is that it is unreached
        raise AssertionError(
            "first run must not run an alert modally: it has no preceding "
            "activation, so the alert can open behind another window"
        )


class _ExplodingAlert(_FakeAlert):
    def beginSheetModalForWindow_completionHandler_(self, window, handler):
        raise RuntimeError("this window is already presenting a sheet")


class _FakeNSEvent:
    handler = None

    @classmethod
    def addLocalMonitorForEventsMatchingMask_handler_(cls, mask, handler):
        cls.handler = handler
        return object()

    @classmethod
    def removeMonitor_(cls, monitor) -> None:
        pass


class _FakeEvent:
    def __init__(self, key_code: int) -> None:
        self._key_code = key_code

    def keyCode(self) -> int:
        return self._key_code


class _Controller(first_run_window.FirstRunController):
    """A `FirstRunController` that never ran `start()`, so it has a fake
    window and no widgets."""


def _first_run() -> _Controller:
    controller = _Controller.alloc().init()
    controller._window = _FakeWindow()
    controller._flow.step = fr.Step.PICTURES
    return controller


class FirstRunSheetTests(unittest.TestCase):
    """The sheet, and the Esc guard a second sheet type needs."""

    def setUp(self) -> None:
        _FakeAlert.last = None
        _FakeNSEvent.handler = None
        alert, event = AppKit.NSAlert, AppKit.NSEvent
        AppKit.NSAlert = _FakeAlert
        AppKit.NSEvent = _FakeNSEvent
        self.addCleanup(setattr, AppKit, "NSAlert", alert)
        self.addCleanup(setattr, AppKit, "NSEvent", event)

    def test_it_presents_as_a_sheet_on_this_window(self) -> None:
        controller = _first_run()
        controller._advise("Head", "Body")
        self.assertIs(_FakeAlert.last.sheet_window, controller._window)

    def test_the_words_reach_the_alert(self) -> None:
        controller = _first_run()
        controller._advise("Couldn't save your settings.", "Nothing was changed.")
        self.assertEqual(_FakeAlert.last.message, "Couldn't save your settings.")
        self.assertEqual(_FakeAlert.last.informative, "Nothing was changed.")
        self.assertEqual(_FakeAlert.last.buttons, ["OK"])

    def test_the_sheet_is_held_and_then_released(self) -> None:
        controller = _first_run()
        controller._advise("Head", "Body")
        self.assertIs(controller._alert_sheet, _FakeAlert.last)
        _FakeAlert.last.handler(AppKit.NSModalResponseOK)
        self.assertIsNone(controller._alert_sheet)

    def test_escape_does_not_tear_the_window_out_from_under_it(self) -> None:
        """🔴 The hang shape Phase 3 removed and a second sheet type can
        reintroduce. The window is deliberately reported as key — that is
        the inference the guard refuses to rest on."""
        controller = _first_run()
        controller._window.key = True
        controller._install_key_monitor()
        controller._advise("Head", "Body")
        event = _FakeEvent(first_run_window.KEY_CODE_ESCAPE)
        self.assertIs(_FakeNSEvent.handler(event), event)
        self.assertFalse(controller._closing)
        self.assertFalse(controller._window.ordered_out)

    def test_escape_closes_the_window_once_the_sheet_is_gone(self) -> None:
        """The positive control. Without it the test above passes for the
        uninteresting reason that Esc never closed anything."""
        controller = _first_run()
        controller._window.key = True
        controller._install_key_monitor()
        controller._advise("Head", "Body")
        _FakeAlert.last.handler(AppKit.NSModalResponseOK)
        event = _FakeEvent(first_run_window.KEY_CODE_ESCAPE)
        self.assertIsNone(_FakeNSEvent.handler(event))
        self.assertTrue(controller._closing)

    def test_a_sheet_that_fails_to_present_does_not_disable_escape(self) -> None:
        """`_advice_dismissed` is the only other place that clears
        `_alert_sheet`, and it never runs if presenting raises. Leaving
        the reference set would kill Esc on this window for the rest of
        its life, with nothing on screen to explain it."""
        controller = _first_run()
        controller._window.key = True
        controller._install_key_monitor()
        AppKit.NSAlert = _ExplodingAlert
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(RuntimeError):
                controller._advise("Head", "Body")
        self.assertIsNone(controller._alert_sheet)
        event = _FakeEvent(first_run_window.KEY_CODE_ESCAPE)
        self.assertIsNone(_FakeNSEvent.handler(event))
        self.assertTrue(controller._closing)

    def test_the_words_go_to_the_log_as_one_line(self) -> None:
        """This app writes to stderr and nowhere else, and
        the alert this replaced left no trace at all."""
        controller = _first_run()
        with contextlib.redirect_stderr(io.StringIO()) as err:
            controller._advise("Couldn't save.", "Could not write\n/tmp/x.")
        self.assertEqual(
            err.getvalue().strip(), "first_run: Couldn't save. Could not write /tmp/x."
        )

    def test_it_is_logged_even_with_no_window_to_attach_a_sheet_to(self) -> None:
        """A message that cannot be shown must still be recorded. The
        sheet is skipped; the line is not."""
        controller = _first_run()
        controller._window = None
        with contextlib.redirect_stderr(io.StringIO()) as err:
            controller._advise("Couldn't save.", "Nothing was changed.")
        self.assertIn("first_run: Couldn't save.", err.getvalue())
        self.assertIsNone(_FakeAlert.last)


if __name__ == "__main__":
    unittest.main()
