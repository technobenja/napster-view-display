"""The folder picker, after it stopped being modal.

Phase 3 of the UI-wedge remediation converted both `NSOpenPanel.runModal()`
sites to `beginSheetModalForWindow:completionHandler:`. `runModal()` is
synchronous — it hands back a response and the next line runs. A sheet is
not: the call returns immediately and the handler runs later. **Every
statement that used to follow `runModal()` had to move into the handler,
and a statement dropped on the way does not raise. The panel just quietly
stops doing half its job.**

That is the reason this file exists at all. `settings_window.py` and
`first_run_window.py` are the deliberately untested half of this
codebase — AppKit shells, with the decisions factored into `*_state.py`
because a shell cannot be exercised without a window server. The
conversion put a piece of sequencing back into the shell, so the shell is
where it has to be checked.

**What makes that possible headlessly:** neither controller's `init`
builds a single AppKit object — that is what `start()` is for — so both
can be `alloc().init()`ed with no window server, exactly as
`test_menubar.py` allocates `MenuBarController` and `display/test_app.py`
allocates `Bootstrapper`. A test subclass then records the three calls
that must follow a chosen folder, in order.

**What this file does NOT cover, and nothing automatable can:** that the
sheet is *visibly attached* to its window, and that `ui_heartbeat_at`
keeps advancing through a real browse. Those need a live screen and are
the human gate in `docs/plans/ui-wedge-remediation.md`.
"""

from __future__ import annotations

import contextlib
import io
import unittest
from unittest.mock import patch

import AppKit
import objc

from ui import first_run_state as fr
from ui import first_run_window
from ui import settings_state as ss
from ui import settings_window


# -- doubles -----------------------------------------------------------


class _FakeWindow:
    """The controllers only ever ask their window to order itself out,
    and only from `_finish`. Real teardown is what the closed-window
    tests below need to exercise, so it is the real `_finish` that runs
    against this."""

    def __init__(self) -> None:
        self.ordered_out = False
        self.key = False

    def isKeyWindow(self) -> bool:
        return self.key

    def orderOut_(self, sender) -> None:
        self.ordered_out = True


class _FakeURL:
    def __init__(self, path: object) -> None:
        self._path = path

    def path(self) -> object:
        return self._path


class _FakePanel:
    """Stands in for `NSOpenPanel`. Records the configuration it was
    given and how it was presented, and **raises if anyone calls
    `runModal()`** — that is the regression this phase exists to prevent,
    and a fake that merely returned a response would let it back in
    silently."""

    last: "_FakePanel | None" = None

    def __init__(self) -> None:
        self.setup: list[tuple[str, object]] = []
        self.sheet_window: object = None
        self.handler = None
        self.ordered_out = False
        self.recorder: list | None = None
        self.url: object = _FakeURL("/pictures")

    @classmethod
    def openPanel(cls) -> "_FakePanel":
        cls.last = cls()
        return cls.last

    def setCanChooseFiles_(self, value) -> None:
        self.setup.append(("files", value))

    def setCanChooseDirectories_(self, value) -> None:
        self.setup.append(("directories", value))

    def setAllowsMultipleSelection_(self, value) -> None:
        self.setup.append(("multiple", value))

    def setPrompt_(self, value) -> None:
        self.setup.append(("prompt", value))

    def beginSheetModalForWindow_completionHandler_(self, window, handler) -> None:
        self.sheet_window = window
        self.handler = handler

    def orderOut_(self, sender) -> None:
        """Recorded into the *controller's* call list, not a flag of its
        own. A boolean only says `orderOut_` happened somewhere on the
        path; the sequence is what is under test, and both ways of
        getting it wrong — ordering out after the work, or after the
        cancel return — leave a boolean true."""
        self.ordered_out = True
        if self.recorder is not None:
            self.recorder.append("order_out")

    def runModal(self):  # pragma: no cover - the assertion is that it is unreached
        raise AssertionError(
            "the folder picker must not run modally: a modal run loop "
            "freezes ui_heartbeat_at for as long as the panel is up"
        )

    def URL(self):
        return self.url


class _ExplodingPanel(_FakePanel):
    """Presenting the sheet raises. `NSInternalInconsistencyException` out
    of `beginSheetModalForWindow:` is real — a window already presenting
    a sheet is one way — and the failure it used to cause here was
    permanent and silent."""

    def beginSheetModalForWindow_completionHandler_(self, window, handler):
        raise RuntimeError("this window is already presenting a sheet")


class _FakeEvent:
    def __init__(self, key_code: int) -> None:
        self._key_code = key_code

    def keyCode(self) -> int:
        return self._key_code


class _FakeNSEvent:
    """Captures the local key monitor so the Esc guard can be driven
    directly. The real `NSEvent` needs no window server to install one,
    but it gives no way to get the handler back, and the handler is the
    thing under test."""

    handler = None

    @classmethod
    def addLocalMonitorForEventsMatchingMask_handler_(cls, mask, handler):
        cls.handler = handler
        return object()

    @classmethod
    def removeMonitor_(cls, monitor) -> None:
        pass


class _RecordingSettings(settings_window.SettingsController):
    """`SettingsController` with the three post-panel effects recorded
    instead of performed. `_sync` really does need replacing: it indexes
    `self._controls` directly, which a controller that never ran
    `start()` has none of."""

    def init(self):
        self = objc.super(_RecordingSettings, self).init()
        if self is None:
            return None
        self.calls = []
        return self

    @objc.python_method
    def _invalidate_test(self) -> None:
        self.calls.append("invalidate")

    @objc.python_method
    def _sync(self) -> None:
        self.calls.append("sync")

    def testSource_(self, sender) -> None:
        self.calls.append("test")


class _RecordingFirstRun(first_run_window.FirstRunController):
    def init(self):
        self = objc.super(_RecordingFirstRun, self).init()
        if self is None:
            return None
        self.calls = []
        return self

    @objc.python_method
    def _invalidate_test(self) -> None:
        self.calls.append("invalidate")

    @objc.python_method
    def _sync(self) -> None:
        self.calls.append("sync")

    def testSource_(self, sender) -> None:
        self.calls.append("test")


def _settings_controller() -> _RecordingSettings:
    controller = _RecordingSettings.alloc().init()
    controller._window = _FakeWindow()
    return controller


def _first_run_controller() -> _RecordingFirstRun:
    controller = _RecordingFirstRun.alloc().init()
    controller._window = _FakeWindow()
    controller._flow.step = fr.Step.PICTURES
    return controller


def _open_sheet(controller) -> "_FakePanel":
    """Click Choose, and point the panel at the controller's call list so
    the whole sequence lands in one place, in order."""
    controller.chooseFolder_(None)
    panel = _FakePanel.last
    panel.recorder = controller.calls
    return panel


# -- the decision, on its own ------------------------------------------


class SheetFolderChoiceTests(unittest.TestCase):
    """`sheet_folder_choice`'s whole truth table. Three of these four
    ways to adopt nothing were early `return`s in the pre-conversion
    inline body; each one is a line that a careless move into a
    completion handler drops without a sound."""

    def test_ok_with_a_path_adopts_it(self):
        self.assertEqual(
            ss.sheet_folder_choice(
                response=ss.MODAL_RESPONSE_OK,
                path="/pictures",
                still_editing=True,
            ),
            "/pictures",
        )

    def test_cancel_adopts_nothing(self):
        """`NSModalResponseCancel` is 0, but so is any other non-OK
        response, and the pre-conversion code tested `!= OK` rather than
        `== Cancel`. Keep it that way."""
        for response in (0, -1, 2, 1000):
            with self.subTest(response=response):
                self.assertIsNone(
                    ss.sheet_folder_choice(
                        response=response, path="/pictures", still_editing=True
                    )
                )

    def test_no_url_adopts_nothing(self):
        self.assertIsNone(
            ss.sheet_folder_choice(
                response=ss.MODAL_RESPONSE_OK, path=None, still_editing=True
            )
        )

    def test_a_closed_window_adopts_nothing(self):
        """The one condition that is new, and new only because the
        mechanism changed: `runModal()` made it structurally impossible
        for the window to go away between the click and the answer."""
        self.assertIsNone(
            ss.sheet_folder_choice(
                response=ss.MODAL_RESPONSE_OK,
                path="/pictures",
                still_editing=False,
            )
        )

    def test_an_empty_path_is_adopted_because_it_always_was(self):
        """Not an endorsement — a record. This phase is a mechanism
        change, and the inline version assigned whatever the panel's path
        was. Rejecting the empty string here would be a behaviour change
        smuggled in under a refactor."""
        self.assertEqual(
            ss.sheet_folder_choice(
                response=ss.MODAL_RESPONSE_OK, path="", still_editing=True
            ),
            "",
        )

    def test_the_state_constant_agrees_with_appkit(self):
        """`settings_state.py` deliberately imports no AppKit, so the OK
        response is duplicated as a plain integer there. Duplicated
        constants drift; this is the check that would notice."""
        self.assertEqual(ss.MODAL_RESPONSE_OK, AppKit.NSModalResponseOK)


# -- presenting the panel ----------------------------------------------


class SettingsPresentsASheetTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakePanel.last = None
        patcher = patch.object(AppKit, "NSOpenPanel", _FakePanel)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_picker_is_a_sheet_on_the_settings_window(self):
        controller = _settings_controller()
        controller.chooseFolder_(None)
        panel = _FakePanel.last
        self.assertIsNotNone(panel)
        self.assertIs(panel.sheet_window, controller._window)
        self.assertIsNotNone(panel.handler)

    def test_the_panel_is_configured_exactly_as_before(self):
        controller = _settings_controller()
        controller.chooseFolder_(None)
        self.assertEqual(
            _FakePanel.last.setup,
            [("files", False), ("directories", True), ("multiple", False), ("prompt", "Choose")],
        )

    def test_no_window_means_no_panel_at_all(self):
        """A sheet needs a window to attach to. Asking for one with no
        window is how a picker ends up free-floating and lost behind
        something, which is the failure this phase removes."""
        controller = _settings_controller()
        controller._window = None
        controller.chooseFolder_(None)
        self.assertIsNone(_FakePanel.last)

    def test_the_handler_runs_the_whole_post_panel_sequence(self):
        """The load-bearing test in this file. `runModal()` returned a
        response and the next four statements ran inline; all four now
        live in the completion handler, and this asserts every one of
        them, in order."""
        controller = _settings_controller()
        controller.chooseFolder_(None)
        _FakePanel.last.handler(AppKit.NSModalResponseOK)
        self.assertEqual(controller._form.folder, "/pictures")
        self.assertEqual(controller.calls, ["invalidate", "sync", "test"])

    def test_cancel_changes_nothing(self):
        controller = _settings_controller()
        controller._form.folder = "/before"
        controller.chooseFolder_(None)
        _FakePanel.last.handler(0)
        self.assertEqual(controller._form.folder, "/before")
        self.assertEqual(controller.calls, [])

    def test_a_window_closed_under_the_sheet_changes_nothing(self):
        controller = _settings_controller()
        controller._form.folder = "/before"
        controller.chooseFolder_(None)
        controller._finish()
        _FakePanel.last.handler(AppKit.NSModalResponseOK)
        self.assertEqual(controller._form.folder, "/before")
        self.assertEqual(controller.calls, [])

    def test_a_panel_with_no_url_changes_nothing(self):
        controller = _settings_controller()
        controller._form.folder = "/before"
        controller.chooseFolder_(None)
        _FakePanel.last.url = None
        _FakePanel.last.handler(AppKit.NSModalResponseOK)
        self.assertEqual(controller._form.folder, "/before")
        self.assertEqual(controller.calls, [])

    def test_the_handler_never_raises_out_of_appkit(self):
        """It is an AppKit callback: `chooseFolder_`'s `try` returned
        long ago and cannot catch anything from here. An exception that
        escaped would unwind into the Objective-C runtime."""
        controller = _settings_controller()
        controller.chooseFolder_(None)
        _FakePanel.last.url = _FakeURL(object())

        with patch.object(
            _RecordingSettings,
            "_invalidate_test",
            objc.python_method(lambda self: (_ for _ in ()).throw(RuntimeError("boom"))),
        ):
            with patch("sys.stderr"):
                _FakePanel.last.handler(AppKit.NSModalResponseOK)


class FirstRunPresentsASheetTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakePanel.last = None
        patcher = patch.object(AppKit, "NSOpenPanel", _FakePanel)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_picker_is_a_sheet_on_the_first_run_window(self):
        controller = _first_run_controller()
        controller.chooseFolder_(None)
        panel = _FakePanel.last
        self.assertIsNotNone(panel)
        self.assertIs(panel.sheet_window, controller._window)
        self.assertIsNotNone(panel.handler)

    def test_the_panel_is_configured_exactly_as_before(self):
        controller = _first_run_controller()
        controller.chooseFolder_(None)
        self.assertEqual(
            _FakePanel.last.setup,
            [("files", False), ("directories", True), ("multiple", False), ("prompt", "Choose")],
        )

    def test_no_window_means_no_panel_at_all(self):
        controller = _first_run_controller()
        controller._window = None
        controller.chooseFolder_(None)
        self.assertIsNone(_FakePanel.last)

    def test_the_handler_runs_the_whole_post_panel_sequence(self):
        controller = _first_run_controller()
        controller.chooseFolder_(None)
        _FakePanel.last.handler(AppKit.NSModalResponseOK)
        self.assertEqual(controller._flow.form.folder, "/pictures")
        self.assertEqual(controller.calls, ["invalidate", "sync", "test"])

    def test_cancel_changes_nothing(self):
        controller = _first_run_controller()
        controller._flow.form.folder = "/before"
        controller.chooseFolder_(None)
        _FakePanel.last.handler(0)
        self.assertEqual(controller._flow.form.folder, "/before")
        self.assertEqual(controller.calls, [])

    def test_a_step_change_under_the_sheet_changes_nothing(self):
        """The state machine's version of the closed-window case, and
        the one that only a wizard has. `_show_step` rebuilds `_controls`
        wholesale, so adopting a folder into a step the user has left
        would edit a form nobody is looking at and sync widgets that no
        longer exist. `runModal()` made this unreachable by spinning the
        loop; a sheet does not, so it is asserted."""
        for step in (fr.Step.DISPLAY, fr.Step.CONFIRM):
            with self.subTest(step=step):
                controller = _first_run_controller()
                controller._flow.form.folder = "/before"
                controller.chooseFolder_(None)
                controller._flow.step = step
                _FakePanel.last.handler(AppKit.NSModalResponseOK)
                self.assertEqual(controller._flow.form.folder, "/before")
                self.assertEqual(controller.calls, [])

    def test_a_window_closed_under_the_sheet_changes_nothing(self):
        controller = _first_run_controller()
        controller._flow.form.folder = "/before"
        controller.chooseFolder_(None)
        controller._finish()
        _FakePanel.last.handler(AppKit.NSModalResponseOK)
        self.assertEqual(controller._flow.form.folder, "/before")
        self.assertEqual(controller.calls, [])

    def test_a_panel_with_no_url_changes_nothing(self):
        controller = _first_run_controller()
        controller._flow.form.folder = "/before"
        controller.chooseFolder_(None)
        _FakePanel.last.url = None
        _FakePanel.last.handler(AppKit.NSModalResponseOK)
        self.assertEqual(controller._flow.form.folder, "/before")
        self.assertEqual(controller.calls, [])


# -- the sheet's lifetime, and what depends on it ----------------------


class SheetLifetimeTests(unittest.TestCase):
    """`self._panel` is not bookkeeping. Two things rest on it: the Esc
    guard below, and keeping an `NSOpenPanel` with an asynchronous
    completion handler alive under pyobjc."""

    def setUp(self) -> None:
        _FakePanel.last = None
        _FakeNSEvent.handler = None
        panel_patch = patch.object(AppKit, "NSOpenPanel", _FakePanel)
        panel_patch.start()
        self.addCleanup(panel_patch.stop)
        event_patch = patch.object(AppKit, "NSEvent", _FakeNSEvent)
        event_patch.start()
        self.addCleanup(event_patch.stop)

    def test_settings_holds_the_panel_and_releases_it(self):
        controller = _settings_controller()
        controller.chooseFolder_(None)
        self.assertIs(controller._panel, _FakePanel.last)
        _FakePanel.last.handler(AppKit.NSModalResponseOK)
        self.assertIsNone(controller._panel)

    def test_first_run_holds_the_panel_and_releases_it(self):
        controller = _first_run_controller()
        controller.chooseFolder_(None)
        self.assertIs(controller._panel, _FakePanel.last)
        _FakePanel.last.handler(AppKit.NSModalResponseOK)
        self.assertIsNone(controller._panel)

    def test_the_panel_is_released_on_cancel_too(self):
        """Released before any early return, or a cancelled browse would
        leave Esc disabled on that window for the rest of its life."""
        for make in (_settings_controller, _first_run_controller):
            with self.subTest(controller=make.__name__):
                controller = make()
                controller.chooseFolder_(None)
                _FakePanel.last.handler(0)
                self.assertIsNone(controller._panel)

    def test_the_sheet_is_ordered_out_before_the_work(self):
        """`orderOut_` is **first**, not merely present. Whether AppKit
        has already dismissed the sheet when it calls back is a detail
        this code refuses to depend on, and the position is the point:
        the Test that ends this sequence reads the folder, which is what
        raises the TCC prompt, and a system prompt stacked on a live
        sheet is the worse outcome.

        Asserting a flag would pass with `orderOut_` moved to the very
        end. Assert the sequence.
        """
        for make in (_settings_controller, _first_run_controller):
            with self.subTest(controller=make.__name__):
                controller = make()
                panel = _open_sheet(controller)
                panel.handler(AppKit.NSModalResponseOK)
                self.assertEqual(
                    controller.calls, ["order_out", "invalidate", "sync", "test"]
                )

    def test_the_sheet_is_ordered_out_before_any_early_return(self):
        """The other half of "first". A cancelled browse does none of the
        work, so a test that only checks the OK path would leave
        `orderOut_` free to sit below the `if folder is None: return`."""
        for make in (_settings_controller, _first_run_controller):
            with self.subTest(controller=make.__name__):
                controller = make()
                panel = _open_sheet(controller)
                panel.handler(0)
                self.assertEqual(controller.calls, ["order_out"])

    def test_both_windows_agree_on_the_escape_key_code(self):
        """The subtests below drive one constant against two modules."""
        self.assertEqual(
            settings_window.KEY_CODE_ESCAPE, first_run_window.KEY_CODE_ESCAPE
        )

    def test_escape_does_not_tear_the_window_out_from_under_a_sheet(self):
        """The hang shape this phase must not introduce. If Esc reached
        `_finish` while a sheet was up, the window would be ordered out
        and dropped, leaving a live sheet attached to nothing.

        The window is deliberately reported as key here — that is the
        *inference* this guard refuses to rest on. Nothing in this
        process can prove what AppKit considers key while a sheet is up,
        so the guard is made not to care.
        """
        for make in (_settings_controller, _first_run_controller):
            with self.subTest(controller=make.__name__):
                controller = make()
                controller._window.key = True
                controller._install_key_monitor()
                controller.chooseFolder_(None)
                event = _FakeEvent(settings_window.KEY_CODE_ESCAPE)
                self.assertIs(_FakeNSEvent.handler(event), event)
                self.assertFalse(controller._closing)
                self.assertFalse(controller._window.ordered_out)

    def test_a_sheet_that_fails_to_present_does_not_disable_escape(self):
        """`_folder_chosen` is the only other place that clears `_panel`,
        and it never runs if presenting the sheet raises. Leaving the
        reference set would kill Esc on this window **for the rest of its
        life** — no sheet on screen, no error the user can see, and the
        guard added in this same change is what turns the leak into a
        dead key. The broad `except` in `chooseFolder_` is what makes it
        silent, so the rollback has to be inside it."""
        for make in (_settings_controller, _first_run_controller):
            with self.subTest(controller=make.__name__):
                controller = make()
                controller._window.key = True
                controller._install_key_monitor()
                with patch.object(AppKit, "NSOpenPanel", _ExplodingPanel):
                    with contextlib.redirect_stderr(io.StringIO()) as err:
                        controller.chooseFolder_(None)
                self.assertIn("chooseFolder_", err.getvalue())
                self.assertIsNone(controller._panel)
                event = _FakeEvent(settings_window.KEY_CODE_ESCAPE)
                self.assertIsNone(_FakeNSEvent.handler(event))
                self.assertTrue(controller._closing)

    def test_escape_still_closes_the_window_with_no_sheet_up(self):
        """The positive control. Without it the test above passes for the
        uninteresting reason that Esc never did anything."""
        for make in (_settings_controller, _first_run_controller):
            with self.subTest(controller=make.__name__):
                controller = make()
                controller._window.key = True
                controller._install_key_monitor()
                event = _FakeEvent(settings_window.KEY_CODE_ESCAPE)
                self.assertIsNone(_FakeNSEvent.handler(event))
                self.assertTrue(controller._closing)


class TornDownWindowTests(unittest.TestCase):
    """\U0001f534 `still_editing = not self._closing and self._window is not None`
    — **each half on its own**, which is what makes them a guard rather
    than a pair of survivors.

    Every other test in this file reaches the torn-down state through
    `_finish()`, which sets both fields, so dropping either half left the
    other one answering and **all 1437 tests still passed**. Both
    mutations survived. That is the same family as this project's other
    vacuous assertions: the invariant is right and the fixture never
    varies what it is about.

    The source comment used to call the split state unreachable and say
    not to test it — and named the reachability in its own previous
    sentence. `_finish` sets `_closing = True`, then calls
    `removeMonitor_` and `orderOut_`, **either of which can raise**, and
    only then sets `_window = None`. `_finish` has no `try` of its own.
    So `_closing=True, _window is not None` is exactly the state a raise
    in teardown leaves behind, and it is the state a live sheet's
    completion handler would then be dispatched into.

    The mirror — `_closing=False, _window is None` — is the defensive
    half: it is what a future `_finish` that cleared the window first
    would produce, and it costs one line to keep honest.
    """

    def setUp(self) -> None:
        # A manual attribute swap rather than the `unittest.mock`
        # helper the rest of this file uses: that helper's name is a SOFT
        # leak term, and every occurrence of it costs an entry in
        # `docs/release/grep-exceptions.txt` with a hand-written reason.
        # Not worth three more for three tests.
        _FakePanel.last = None
        original = AppKit.NSOpenPanel
        AppKit.NSOpenPanel = _FakePanel
        self.addCleanup(setattr, AppKit, "NSOpenPanel", original)

    def folder_of(self, controller) -> str:
        return controller._flow.form.folder if hasattr(controller, "_flow") else controller._form.folder

    def set_folder(self, controller, value: str) -> None:
        if hasattr(controller, "_flow"):
            controller._flow.form.folder = value
        else:
            controller._form.folder = value

    def test_a_teardown_that_raised_leaves_closing_set_and_is_honoured(self) -> None:
        """`_closing` alone must stop the adoption. Delete `not
        self._closing` and this is the test that goes red."""
        for make in (_settings_controller, _first_run_controller):
            with self.subTest(controller=make.__name__):
                controller = make()
                self.set_folder(controller, "/before")
                controller.chooseFolder_(None)
                controller._closing = True
                self.assertIsNotNone(
                    controller._window,
                    "the fixture must leave the window in place, or this "
                    "tests both halves again and neither alone",
                )
                _FakePanel.last.handler(AppKit.NSModalResponseOK)
                self.assertEqual(self.folder_of(controller), "/before")
                self.assertEqual(
                    controller.calls,
                    [],
                    "the post-sheet work ran against a window that is gone",
                )

    def test_a_dropped_window_alone_is_honoured_too(self) -> None:
        """The mirror. Delete `self._window is not None` and this one
        goes red."""
        for make in (_settings_controller, _first_run_controller):
            with self.subTest(controller=make.__name__):
                controller = make()
                self.set_folder(controller, "/before")
                controller.chooseFolder_(None)
                controller._window = None
                self.assertFalse(
                    controller._closing,
                    "the fixture must leave _closing False, or this tests "
                    "both halves again",
                )
                _FakePanel.last.handler(AppKit.NSModalResponseOK)
                self.assertEqual(self.folder_of(controller), "/before")
                self.assertEqual(
                    controller.calls,
                    [],
                    "the post-sheet work ran against a window that is gone",
                )

    def test_with_neither_set_the_folder_is_adopted(self) -> None:
        """The positive control. Without it both tests above would pass
        just as well if the sheet never adopted anything at all."""
        for make in (_settings_controller, _first_run_controller):
            with self.subTest(controller=make.__name__):
                controller = make()
                self.set_folder(controller, "/before")
                controller.chooseFolder_(None)
                _FakePanel.last.handler(AppKit.NSModalResponseOK)
                self.assertEqual(self.folder_of(controller), "/pictures")


if __name__ == "__main__":
    unittest.main()
