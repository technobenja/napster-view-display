"""First-run flow, as an AppKit window.

The shell only. Every decision it makes — which step is next, whether
Next is enabled, what gets written on finish, and all of the copy — lives
in `first_run_state.py`, which is testable without a window server. Same
split as `settings_window.py` / `settings_state.py`.

Everything the flow *shows* is Step 4's, reused rather than
reimplemented: `settings_state.probe` is the Test button,
`settings_state.display_options` is the picker, and `SourceForm` is the
edit buffer. A second implementation of any of them would drift from the
settings window, and the two screens would then disagree about whether
the same source was usable.

**The window rebuilds its content view on each step change** rather than
hiding and showing three overlapping sets of widgets. A wizard step is a
whole screen, not a variation on one, and the alternative — thirty
widgets whose visibility is a function of the current step — is the kind
of layout that develops a step where one stale label never got hidden.
Rebuilding costs nothing at this size and cannot leave residue.
"""

from __future__ import annotations

import sys
import threading
import traceback

import AppKit
import objc
from Foundation import NSMakeRect

from display import display_target, paths, source_settings
from display.atomic_io import atomic_write_json
from display.config_store import read_json_object
from ui import first_run_state as fr
from ui import identify
from ui import settings_state as ss

WINDOW_WIDTH = 560.0
WINDOW_HEIGHT = 460.0

LEFT = 24.0
INDENT = 44.0
FIELD_WIDTH = WINDOW_WIDTH - INDENT - LEFT - 8.0
BUTTON_BAR = 60.0

KEY_CODE_ESCAPE = 53


class FirstRunController(AppKit.NSObject):
    """Owns the window and the flow. `alloc().init()` then `start()`,
    matching every other controller in this UI."""

    def init(self):
        self = objc.super(FirstRunController, self).init()
        if self is None:
            return None
        self._window = None
        self._on_close = None
        self._on_calibrate = None
        self._closing = False
        self._flow = fr.FirstRunFlow()
        self._displays = []
        self._controls = {}
        self._content = None
        self._monitor = None
        self._testing = False
        self._test_token = 0
        self._saved = False
        return self

    # -- setup ---------------------------------------------------------

    @objc.python_method
    def start(self, on_close=None, on_calibrate=None) -> bool:
        """Open the flow. `on_calibrate` is called after the window
        closes if the user asked to adjust the circle."""
        self._on_close = on_close
        self._on_calibrate = on_calibrate
        try:
            # Read the raw user document, **not** `load_settings_resolved`.
            # resolution order seeds `~/.viewlab/settings.json`
            # from the bundled default as a side effect of reading it,
            # and the bundled default carries the legacy flat
            # `image_studio_base_url` key — which `setup_needed` counts as
            # "the user has a source". Opening this window would
            # therefore clear the very condition that opens it: close the
            # flow at step 1 and the menu no longer offers
            # `Finish setup…`, leaving a new user stranded on a config
            # they never chose. This read never writes.
            data = read_json_object(paths.settings_path(), "settings") or {}
            self._flow.form = ss.SourceForm.from_settings(
                source_settings.source_from_settings_data(data)
            )
        except Exception:  # noqa: BLE001 - an empty form still works
            print(
                f"first_run: could not load settings:\n{traceback.format_exc()}",
                file=sys.stderr,
            )
            self._flow.form = ss.SourceForm()

        self._refresh_displays()
        self._build_window()
        self._install_key_monitor()
        self._show_step()
        self._window.makeKeyAndOrderFront_(None)
        AppKit.NSApp().activateIgnoringOtherApps_(True)
        return True

    @objc.python_method
    def _refresh_displays(self) -> None:
        """`Check again`. Never filters."""
        try:
            self._displays = ss.display_options(display_target.screen_records())
        except Exception:  # noqa: BLE001 - an empty picker beats a crash
            print(
                f"first_run: could not enumerate displays:\n"
                f"{traceback.format_exc()}",
                file=sys.stderr,
            )
            self._displays = []

    @objc.python_method
    def _build_window(self) -> None:
        window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WINDOW_WIDTH, WINDOW_HEIGHT),
            AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable,
            AppKit.NSBackingStoreBuffered,
            False,
        )
        window.setTitle_(fr.WINDOW_TITLE)
        window.setReleasedWhenClosed_(False)
        window.setDelegate_(self)
        window.center()
        self._window = window

    # -- building one step ---------------------------------------------

    @objc.python_method
    def _show_step(self) -> None:
        """Tear down the content view and build the current step."""
        step = self._flow.step
        self._controls = {}

        content = AppKit.NSView.alloc().initWithFrame_(
            NSMakeRect(0, 0, WINDOW_WIDTH, WINDOW_HEIGHT)
        )
        top = 22.0

        progress = self._label(fr.progress_label(step), size=11.0, secondary=True)
        top = self._place(content, progress, top, 16.0) + 4.0

        title = self._label(fr.STEP_TITLES[step], size=19.0)
        title.setFont_(AppKit.NSFont.boldSystemFontOfSize_(19.0))
        top = self._place(content, title, top, 26.0) + 6.0

        body = self._label(fr.body_for(step), size=12.0, secondary=True)
        top = self._place(content, body, top, 48.0) + 12.0

        if step is fr.Step.DISPLAY:
            top = self._build_display_step(content, top)
        elif step is fr.Step.PICTURES:
            top = self._build_pictures_step(content, top)
        else:
            top = self._build_confirm_step(content, top)

        self._build_buttons(content)
        self._window.setContentView_(content)
        self._content = content
        self._sync()

    @objc.python_method
    def _place(self, content, view, top: float, height: float, left: float = LEFT):
        """Place `view` `top` points from the top of the window.

        An NSView is unflipped — y counts up from the bottom — and a
        wizard reads downwards, so every caller would otherwise be doing
        this subtraction by hand and one of them would get it wrong.
        Returns the new top cursor.
        """
        width = WINDOW_WIDTH - left - LEFT
        view.setFrame_(NSMakeRect(left, WINDOW_HEIGHT - top - height, width, height))
        content.addSubview_(view)
        return top + height

    @objc.python_method
    def _build_display_step(self, content, top: float) -> float:
        picker = self._popup([], "displayChanged:")
        top = self._place(content, picker, top, 26.0, left=INDENT) + 8.0
        self._controls["display"] = picker

        check = self._button(fr.DISPLAY_CHECK_AGAIN, "checkDisplaysAgain:")
        check.setFrame_(NSMakeRect(INDENT, WINDOW_HEIGHT - top - 28.0, 110.0, 28.0))
        content.addSubview_(check)
        identify = self._button(fr.DISPLAY_IDENTIFY, "identifyDisplay:")
        identify.setFrame_(
            NSMakeRect(INDENT + 118.0, WINDOW_HEIGHT - top - 28.0, 100.0, 28.0)
        )
        content.addSubview_(identify)
        top += 28.0 + 12.0

        note = self._label("", size=11.0, secondary=True)
        top = self._place(content, note, top, 32.0, left=INDENT) + 4.0
        self._controls["display_note"] = note

        skip = self._label(fr.DISPLAY_SKIP_NOTE, size=11.0, secondary=True)
        return self._place(content, skip, top, 32.0, left=INDENT)

    @objc.python_method
    def _build_pictures_step(self, content, top: float) -> float:
        """Three rows, always visible, unselected ones disabled.

        Hiding the unselected rows would make the window resize under the
        user and would hide the fact that there are three choices at all
        — which on first run is the one thing this step has to teach.
        """
        for kind, title in ss.SOURCE_ROWS:
            radio = AppKit.NSButton.alloc().init()
            radio.setButtonType_(AppKit.NSButtonTypeRadio)
            radio.setTitle_(title)
            radio.setTarget_(self)
            radio.setAction_("sourceRowChanged:")
            radio.setTag_([k for k, _ in ss.SOURCE_ROWS].index(kind))
            top = self._place(content, radio, top, 20.0) + 2.0
            self._controls[f"radio_{kind}"] = radio

            if kind == source_settings.KIND_FOLDER:
                field = self._field("folderChanged:")
                field.setFrame_(
                    NSMakeRect(
                        INDENT, WINDOW_HEIGHT - top - 24.0, FIELD_WIDTH - 104.0, 24.0
                    )
                )
                content.addSubview_(field)
                self._controls["folder"] = field
                choose = self._button("Choose…", "chooseFolder:")
                choose.setFrame_(
                    NSMakeRect(
                        WINDOW_WIDTH - LEFT - 96.0,
                        WINDOW_HEIGHT - top - 26.0,
                        96.0,
                        28.0,
                    )
                )
                content.addSubview_(choose)
                self._controls["choose_folder"] = choose
                top += 24.0 + 10.0
            elif kind == source_settings.KIND_JSON_URL:
                field = self._field("listUrlChanged:")
                field.setPlaceholderString_("https://example.com/pictures.json")
                top = self._place(content, field, top, 24.0, left=INDENT) + 10.0
                self._controls["list_url"] = field
            else:
                field = self._field("baseUrlChanged:")
                field.setPlaceholderString_("http://your-server:8883")
                top = self._place(content, field, top, 24.0, left=INDENT) + 6.0
                self._controls["base_url"] = field
                pool = self._popup(
                    [label for label, _ in ss.POOL_CHOICES], "poolChanged:"
                )
                pool.setFrame_(
                    NSMakeRect(INDENT + 60.0, WINDOW_HEIGHT - top - 26.0, 150.0, 26.0)
                )
                content.addSubview_(pool)
                self._controls["pool"] = pool
                pool_label = self._label("Show:", size=12.0)
                pool_label.setFrame_(
                    NSMakeRect(INDENT, WINDOW_HEIGHT - top - 22.0, 56.0, 18.0)
                )
                content.addSubview_(pool_label)
                self._controls["pool_label"] = pool_label
                top += 26.0 + 10.0

        test = self._button("Test", "testSource:")
        test.setFrame_(NSMakeRect(LEFT, WINDOW_HEIGHT - top - 28.0, 90.0, 28.0))
        content.addSubview_(test)
        self._controls["test"] = test

        result = self._label("", size=12.0)
        result.setFrame_(
            NSMakeRect(
                LEFT + 100.0,
                WINDOW_HEIGHT - top - 26.0,
                WINDOW_WIDTH - LEFT - LEFT - 100.0,
                24.0,
            )
        )
        content.addSubview_(result)
        self._controls["test_result"] = result
        return top + 28.0

    @objc.python_method
    def _build_confirm_step(self, content, top: float) -> float:
        """Note plus two-ring explanation.

        explanation normally lives in the calibration window. Most
        users will now never open that window, and the dark ring is
        visible from the moment pictures start — so the explanation has
        to be here, where the ring is, or the user reads the gap as a
        fault and spends the safety margin correcting it.
        """
        for text in fr.confirm_notes():
            label = self._label(text, size=12.0, secondary=True)
            top = self._place(content, label, top, 44.0, left=INDENT) + 6.0
        return top

    @objc.python_method
    def _build_buttons(self, content) -> None:
        """The bar is rebuilt with the step, because the last step's
        buttons are not the others'."""
        y = 16.0
        if self._flow.can_go_back:
            back = self._button(fr.BACK_BUTTON, "goBack:")
            back.setFrame_(NSMakeRect(LEFT, y, 90.0, 32.0))
            content.addSubview_(back)

        if self._flow.is_last_step:
            # Primary right, secondary to its left and plainly styled:
            # expected path is confirming, so `Adjust the circle`
            # must not compete with `Looks good` for the eye.
            primary = self._button(fr.CONFIRM_PRIMARY, "confirm:")
            primary.setKeyEquivalent_("\r")
            primary.setFrame_(NSMakeRect(WINDOW_WIDTH - LEFT - 120.0, y, 120.0, 32.0))
            content.addSubview_(primary)
            adjust = self._button(fr.CONFIRM_SECONDARY, "adjust:")
            adjust.setFrame_(NSMakeRect(WINDOW_WIDTH - LEFT - 280.0, y, 152.0, 32.0))
            content.addSubview_(adjust)
            self._controls["primary"] = primary
        else:
            nxt = self._button(fr.NEXT_BUTTON, "goNext:")
            nxt.setKeyEquivalent_("\r")
            nxt.setFrame_(NSMakeRect(WINDOW_WIDTH - LEFT - 100.0, y, 100.0, 32.0))
            content.addSubview_(nxt)
            self._controls["primary"] = nxt

            blocked = self._label("", size=11.0, secondary=True)
            blocked.setFrame_(NSMakeRect(LEFT + 100.0, y + 7.0, 320.0, 18.0))
            content.addSubview_(blocked)
            self._controls["blocked_note"] = blocked

    # -- widget helpers -------------------------------------------------

    @objc.python_method
    def _label(self, text: str, size: float, secondary: bool = False):
        field = AppKit.NSTextField.alloc().init()
        field.setStringValue_(text)
        field.setBezeled_(False)
        field.setDrawsBackground_(False)
        field.setEditable_(False)
        field.setSelectable_(False)
        field.setFont_(AppKit.NSFont.systemFontOfSize_(size))
        if secondary:
            field.setTextColor_(AppKit.NSColor.secondaryLabelColor())
        field.cell().setWraps_(True)
        return field

    @objc.python_method
    def _button(self, title: str, action: str):
        button = AppKit.NSButton.alloc().init()
        button.setTitle_(title)
        button.setBezelStyle_(AppKit.NSBezelStyleRounded)
        button.setTarget_(self)
        button.setAction_(action)
        return button

    @objc.python_method
    def _field(self, action: str):
        field = AppKit.NSTextField.alloc().init()
        field.setTarget_(self)
        field.setAction_(action)
        field.setContinuous_(True)
        field.setDelegate_(self)
        return field

    @objc.python_method
    def _popup(self, titles: list[str], action: str):
        popup = AppKit.NSPopUpButton.alloc().init()
        popup.setTarget_(self)
        popup.setAction_(action)
        for title in titles:
            popup.addItemWithTitle_(title)
        return popup

    # -- syncing --------------------------------------------------------

    @objc.python_method
    def _sync(self) -> None:
        """Model -> widgets, one direction, like every other window here."""
        controls = self._controls
        step = self._flow.step

        if step is fr.Step.DISPLAY:
            self._sync_display_picker()
        elif step is fr.Step.PICTURES:
            self._sync_pictures()

        primary = controls.get("primary")
        if primary is not None:
            primary.setEnabled_(
                self._flow.is_last_step or self._flow.can_advance
            )
        note = controls.get("blocked_note")
        if note is not None:
            note.setStringValue_(self._flow.blocked_note)

    @objc.python_method
    def _sync_display_picker(self) -> None:
        picker = self._controls.get("display")
        if picker is None:
            return
        picker.removeAllItems()
        picker.addItemWithTitle_("Find it automatically")
        for option in self._displays:
            picker.addItemWithTitle_(option.title)

        index = 0
        for position, option in enumerate(self._displays):
            record = self._record_for(option)
            if record and record.get("display_id") == self._flow.chosen_display_id:
                if self._flow.chosen_display_id:
                    index = position + 1
                    break
        picker.selectItemAtIndex_(index)

        matched = any(option.probably_view for option in self._displays)
        self._controls["display_note"].setStringValue_(
            "" if matched else ss.nothing_matched_note()
        )

    @objc.python_method
    def _record_for(self, option):
        try:
            for record in display_target.screen_records():
                if (
                    record.get("name") == option.name
                    and record.get("width") == option.width
                    and record.get("height") == option.height
                ):
                    return record
        except Exception:  # noqa: BLE001
            print(f"first_run: _record_for:\n{traceback.format_exc()}", file=sys.stderr)
        return None

    @objc.python_method
    def _sync_pictures(self) -> None:
        controls = self._controls
        form = self._flow.form
        for kind, _ in ss.SOURCE_ROWS:
            radio = controls.get(f"radio_{kind}")
            if radio is not None:
                radio.setState_(
                    AppKit.NSControlStateValueOn
                    if form.kind == kind
                    else AppKit.NSControlStateValueOff
                )
        self._set_text(controls.get("folder"), form.folder)
        self._set_text(controls.get("list_url"), form.list_url)
        self._set_text(controls.get("base_url"), form.base_url)
        pool = controls.get("pool")
        if pool is not None:
            pool.selectItemAtIndex_(ss.pool_index(form.pool))

        # Unselected rows are disabled, never hidden.
        is_folder = form.kind == source_settings.KIND_FOLDER
        is_url = form.kind == source_settings.KIND_JSON_URL
        is_studio = form.kind == source_settings.KIND_IMAGE_SERVER
        for key, enabled in (
            ("folder", is_folder),
            ("choose_folder", is_folder),
            ("list_url", is_url),
            ("base_url", is_studio),
            ("pool", is_studio),
        ):
            widget = controls.get(key)
            if widget is not None:
                widget.setEnabled_(enabled)

        test = controls.get("test")
        if test is not None:
            test.setEnabled_(not self._testing)
        self._sync_test_result()

    @objc.python_method
    def _sync_test_result(self) -> None:
        label = self._controls.get("test_result")
        if label is None:
            return
        if self._testing:
            label.setStringValue_("Checking…")
            label.setTextColor_(AppKit.NSColor.secondaryLabelColor())
            return
        result = self._flow.test_result
        if result is None:
            label.setStringValue_("")
            return
        label.setStringValue_(result.message)
        label.setTextColor_(
            AppKit.NSColor.labelColor() if result.ok else AppKit.NSColor.systemRedColor()
        )

    @objc.python_method
    def _set_text(self, field, value: str) -> None:
        """Only write when it differs. Assigning the same string resets
        the insertion point to the end, which is visible and maddening
        while typing in the middle of a URL."""
        if field is None:
            return
        if str(field.stringValue()) != value:
            field.setStringValue_(value)

    @objc.python_method
    def _invalidate_test(self) -> None:
        """Any source edit drops the previous result and supersedes any
        probe still in flight."""
        self._flow.source_edited()
        self._testing = False
        self._test_token += 1

    # -- actions: the source rows ---------------------------------------

    def sourceRowChanged_(self, sender) -> None:
        try:
            index = int(sender.tag())
            self._flow.form.kind = ss.SOURCE_ROWS[index][0]
            self._invalidate_test()
            self._sync()
        except Exception:
            print(f"sourceRowChanged_:\n{traceback.format_exc()}", file=sys.stderr)

    def folderChanged_(self, sender) -> None:
        try:
            self._flow.form.folder = str(sender.stringValue())
            self._invalidate_test()
            self._sync()
        except Exception:
            print(f"folderChanged_:\n{traceback.format_exc()}", file=sys.stderr)

    def listUrlChanged_(self, sender) -> None:
        try:
            self._flow.form.list_url = str(sender.stringValue())
            self._invalidate_test()
            self._sync()
        except Exception:
            print(f"listUrlChanged_:\n{traceback.format_exc()}", file=sys.stderr)

    def baseUrlChanged_(self, sender) -> None:
        try:
            self._flow.form.base_url = str(sender.stringValue())
            self._invalidate_test()
            self._sync()
        except Exception:
            print(f"baseUrlChanged_:\n{traceback.format_exc()}", file=sys.stderr)

    def poolChanged_(self, sender) -> None:
        try:
            index = int(sender.indexOfSelectedItem())
            self._flow.form.pool = ss.POOL_CHOICES[index][1]
            self._invalidate_test()
            self._sync()
        except Exception:
            print(f"poolChanged_:\n{traceback.format_exc()}", file=sys.stderr)

    def controlTextDidChange_(self, notification) -> None:
        """Continuous fields also report through the delegate. Routed to
        the same handlers so a keystroke and a commit are one path."""
        try:
            field = notification.object()
            controls = self._controls
            if field is controls.get("folder"):
                self.folderChanged_(field)
            elif field is controls.get("list_url"):
                self.listUrlChanged_(field)
            elif field is controls.get("base_url"):
                self.baseUrlChanged_(field)
        except Exception:
            print(f"controlTextDidChange_:\n{traceback.format_exc()}", file=sys.stderr)

    def chooseFolder_(self, sender) -> None:
        try:
            panel = AppKit.NSOpenPanel.openPanel()
            panel.setCanChooseFiles_(False)
            panel.setCanChooseDirectories_(True)
            panel.setAllowsMultipleSelection_(False)
            panel.setPrompt_("Choose")
            if panel.runModal() != AppKit.NSModalResponseOK:
                return
            url = panel.URL()
            if url is None:
                return
            self._flow.form.folder = str(url.path())
            self._invalidate_test()
            self._sync()
            # The panel is also what grants folder access, so this
            # is both the moment the folder is readable and the moment
            # the answer is most useful.
            self.testSource_(None)
        except Exception:
            print(f"chooseFolder_:\n{traceback.format_exc()}", file=sys.stderr)

    # -- actions: Test ---------------------------------------------------

    def testSource_(self, sender) -> None:
        """Test button, on a background thread for the reason the
        settings window documents: a folder read or an HTTP probe can
        block for seconds, and freezing this window during the one
        interaction whose purpose is to feel responsive is worse than the
        thread."""
        try:
            self._testing = True
            self._flow.record_test(None)
            self._test_token += 1
            token = self._test_token
            form = ss.SourceForm(**vars(self._flow.form))
            self._sync()
            threading.Thread(
                target=self._run_test, args=(form, token), daemon=True
            ).start()
        except Exception:
            self._testing = False
            print(f"testSource_:\n{traceback.format_exc()}", file=sys.stderr)

    @objc.python_method
    def _run_test(self, form, token: int) -> None:
        """Background half. Never raises — a bare thread, and an
        exception here would leave `Checking…` on screen forever."""
        try:
            result = ss.probe(form)
        except Exception:  # noqa: BLE001 - see docstring
            print(f"first_run: probe failed:\n{traceback.format_exc()}", file=sys.stderr)
            result = ss.TestResult(ss.Outcome.UNREACHABLE, "Could not reach that address")
        AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(
            lambda: self._test_finished(result, token)
        )

    @objc.python_method
    def _test_finished(self, result, token: int) -> None:
        try:
            if token != self._test_token:
                # Superseded by a later edit or Test. A slow probe must
                # not answer a question the user has already changed.
                return
            self._testing = False
            self._flow.record_test(result)
            self._sync()
        except Exception:
            print(f"_test_finished:\n{traceback.format_exc()}", file=sys.stderr)

    # -- actions: the display picker -------------------------------------

    def displayChanged_(self, sender) -> None:
        try:
            index = int(sender.indexOfSelectedItem())
            if index <= 0:
                self._flow.choose_display("")
            elif index - 1 < len(self._displays):
                record = self._record_for(self._displays[index - 1])
                self._flow.choose_display((record or {}).get("display_id", ""))
        except Exception:
            print(f"displayChanged_:\n{traceback.format_exc()}", file=sys.stderr)

    def checkDisplaysAgain_(self, sender) -> None:
        try:
            self._refresh_displays()
            self._sync_display_picker()
        except Exception:
            print(f"checkDisplaysAgain_:\n{traceback.format_exc()}", file=sys.stderr)

    def identifyDisplay_(self, sender) -> None:
        """Identify. Every display, not only the selected one —
        the question being answered is "which of these is the little
        round one", and lighting only the current guess cannot answer
        it."""
        try:
            identify.flash_screens()
        except Exception:
            print(f"identifyDisplay_:\n{traceback.format_exc()}", file=sys.stderr)

    # -- actions: moving through the flow --------------------------------

    def goNext_(self, sender) -> None:
        try:
            was = self._flow.step
            if not self._flow.advance():
                return
            if was is fr.Step.PICTURES:
                # Persist on entering the confirm step, not on finishing.
                # Pictures come before confirm so that the confirm
                # step has real content behind it — which is only true if
                # the display has actually been told about the source by
                # the time that step is on screen. It also means closing
                # the window at the last step keeps a source the user
                # already chose and validated, rather than dropping them
                # back into `Setup needed` with nothing saved.
                if not self._persist():
                    self._flow.back()
                    return
            self._show_step()
        except Exception:
            print(f"goNext_:\n{traceback.format_exc()}", file=sys.stderr)

    def goBack_(self, sender) -> None:
        try:
            if self._flow.back():
                self._show_step()
        except Exception:
            print(f"goBack_:\n{traceback.format_exc()}", file=sys.stderr)

    def confirm_(self, sender) -> None:
        try:
            self._flow.finish(fr.Finish.CONFIRMED)
            self._finish()
        except Exception:
            print(f"confirm_:\n{traceback.format_exc()}", file=sys.stderr)

    def adjust_(self, sender) -> None:
        """`Adjust the circle` finishes too — it is not a cancel. The
        source is already written; this only additionally asks the menu
        bar to open the calibration window."""
        try:
            self._flow.finish(fr.Finish.ADJUST)
            self._finish()
        except Exception:
            print(f"adjust_:\n{traceback.format_exc()}", file=sys.stderr)

    # -- writing ---------------------------------------------------------

    @objc.python_method
    def _persist(self) -> bool:
        """The two durable writes, both atomic merges, same as Save. Returns whether it succeeded."""
        source = self._flow.to_source()
        if source is None:
            self._alert(
                "That source isn't complete.",
                "Fill in the option under the row you picked, then press Test.",
            )
            return False

        path = paths.settings_path()
        try:
            previous = read_json_object(path, "settings") or {}
            document = fr.settings_document(previous, source)
            paths.ensure_dir(path.parent)
            atomic_write_json(path, document)
        except OSError:
            print(f"first_run: save failed:\n{traceback.format_exc()}", file=sys.stderr)
            self._alert(
                "Couldn't save your settings.",
                f"Nothing was changed. Could not write {path}.",
            )
            return False

        self._save_display_choice()
        self._saved = True
        print(f"first_run: saved {path}.", file=sys.stderr)
        return True

    @objc.python_method
    def _save_display_choice(self) -> None:
        """Write `target_screen` into calibration.json (puts it
        there, not in settings.json, so this app and any other reader cannot
        target different monitors).

        A failure here is logged but does not block the flow: the source
        is already saved, the heuristic still finds the View on this
        hardware, and stopping a first run over it would be worse than
        the fallback.
        """
        path = paths.calibration_path()
        try:
            previous = read_json_object(path, "calibration") or {}
            if not previous:
                # First run is exactly when `~/.viewlab/calibration.json`
                # may not exist yet — nothing has necessarily read
                # calibration on this machine, so seeding has not
                # happened. Merging into the bundled document rather than
                # declining to write is what makes step 1's answer stick;
                # otherwise the user picks their View by name, sees it
                # accepted, and the choice is silently dropped. The
                # bundled file supplies the circle numbers, so the result
                # is a complete document rather than a `target_screen`
                # floating on its own.
                previous = (
                    read_json_object(paths.bundled_calibration_path(), "calibration")
                    or {}
                )
            document = fr.calibration_document(previous, self._flow.chosen_display_id)
            if document is None:
                return
            paths.ensure_dir(path.parent)
            atomic_write_json(path, document)
        except OSError:
            print(
                f"first_run: could not write the screen choice:\n"
                f"{traceback.format_exc()}",
                file=sys.stderr,
            )

    # -- teardown ---------------------------------------------------------

    def windowShouldClose_(self, sender) -> bool:
        try:
            self._finish()
        except Exception:
            print(f"windowShouldClose_:\n{traceback.format_exc()}", file=sys.stderr)
        return False

    @objc.python_method
    def _finish(self) -> None:
        """Idempotent, matching the other controllers: Esc plus a click
        would otherwise remove the key monitor twice."""
        if self._closing:
            return
        self._closing = True
        if self._monitor is not None:
            AppKit.NSEvent.removeMonitor_(self._monitor)
            self._monitor = None
        if self._window is not None:
            self._window.orderOut_(None)
        self._window = None
        wants_calibration = self._flow.wants_calibration
        if self._on_close is not None:
            callback, self._on_close = self._on_close, None
            callback()
        if wants_calibration and self._on_calibrate is not None:
            callback, self._on_calibrate = self._on_calibrate, None
            callback()

    @objc.python_method
    def _install_key_monitor(self) -> None:
        def handler(event):
            try:
                if (
                    self._window is not None
                    and self._window.isKeyWindow()
                    and event.keyCode() == KEY_CODE_ESCAPE
                ):
                    self._finish()
                    return None
            except Exception:  # noqa: BLE001 - an AppKit callback
                print(
                    f"first_run: key handler:\n{traceback.format_exc()}",
                    file=sys.stderr,
                )
            return event

        self._monitor = AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            AppKit.NSEventMaskKeyDown, handler
        )

    @objc.python_method
    def _alert(self, message: str, informative: str) -> None:
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_(message)
        alert.setInformativeText_(informative)
        alert.addButtonWithTitle_("OK")
        alert.runModal()


__all__ = ["FirstRunController"]
