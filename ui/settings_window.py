"""The settings window.

Run standalone: `display/.venv/bin/python3 ui/settings_window.py`
(normally opened from the menu bar's `Settings…`.)

**Deliberately small.** The design cut two things and named the reasons: the
cache-size ceiling (nobody can evaluate it) and the crossfade duration
(2.0s was confirmed on the physical device in Phase 2, and a slider
invites unconfirming it). Both stay in `settings.json` for the one
person who cares, and `settings_state.settings_document` preserves them
across every Save precisely so this window cannot quietly reset them.

**The three sources are laid out statically, not swapped.** Three
hand-written panels rendered as three always-visible radio rows rather
than a shape-shifting panel — the UX and product reviews converged on
this independently. Unselected rows' options are *disabled, never
hidden*: a control that vanishes takes its label with it, so the user
cannot see what they would be choosing, and the window's height changes
under the cursor.

**Validation happens at pick time, and this is the one place this
project's "stay silent about problems" philosophy deliberately does not
apply** — the user is present and can fix it. That applies to later
source changes too, not just first-run: fat-fingering a URL is more
likely after setup than during it.

The decisions all live in `settings_state.py`, which has no AppKit in
it. What is left here is an `NSWindow`, some rows of controls, and the
code that turns a click into a call — the same split `menubar.py` and
`calibrate_window.py` already make.

**Conventions this file follows**, each already paid for elsewhere in
this project: `@objc.python_method` on every private helper of an
`NSObject` subclass; a broad `try/except` around the body of every
selector AppKit can call; atomic write-then-rename for every write;
reads that never raise. No emoji and no icons in any label.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

import AppKit
import objc
from Foundation import NSMakeRect

from display import blank_schedule, control, display_target, paths, source_settings
from display.atomic_io import atomic_write_json
from display.blank_schedule import BlankSchedule
from display.calibration import load_calibration_resolved
from display.config_store import read_json_object
from display.settings import load_settings_resolved
from ui import identify
from ui import menubar_state as ms
from ui import settings_state as ss
from ui import ui_agent

WINDOW_WIDTH = 560.0

#: Tall content in a window that must fit a laptop screen. The content
#: scrolls; Save and Cancel do not, because a button you have to scroll
#: to find is a button that looks missing.
MAX_CONTENT_HEIGHT = 620.0

LEFT = 20.0
INDENT = 40.0
FIELD_WIDTH = WINDOW_WIDTH - INDENT - LEFT - 8.0

#: The live display agent's label. Deliberately the *existing* one, for
#: the reason `menubar.py` documents: Step 4's job is to drive the
#: display that is actually running. shipped label
#: (`dev.viewlab.imageview.display`) lands with the installer.
DISPLAY_AGENT_LABEL = paths.DISPLAY_AGENT_LABEL

KEY_CODE_ESCAPE = 53


def _settings_document() -> dict:
    """The current `~/.viewlab/settings.json` as a plain dict, or {}.

    Read raw rather than through `load_settings_resolved()` because the resolution order
    makes v1 additive-only, so Save has to preserve keys this version
    does not understand — and a validated `Settings` has already dropped
    them. Same reasoning as `calibrate_window._calibration_document`.
    """
    return read_json_object(paths.settings_path(), "settings") or {}


class SettingsController(AppKit.NSObject):
    """Owns the window, the edit buffer, and the two durable writes.

    `alloc().init()` then `start()`, matching `MenuBarController` and
    `CalibrateController`: a failure to read config is a logged error
    and an alert, not an exception thrown out of `init`.
    """

    def init(self):
        self = objc.super(SettingsController, self).init()
        if self is None:
            return None
        self._window = None
        self._on_close = None
        self._closing = False
        self._form = ss.SourceForm()
        self._schedule = BlankSchedule()
        self._interval_index = 1
        self._shuffle = True
        self._displays = []
        self._chosen_display_id = ""
        self._controls = {}
        self._status_rows = []
        self._monitor = None
        self._test_result = None
        self._testing = False
        self._test_token = 0
        self._dirty = False
        return self

    # -- setup ----------------------------------------------------------

    @objc.python_method
    def start(self, on_close=None) -> bool:
        self._on_close = on_close
        try:
            settings = load_settings_resolved().value
        except Exception:  # noqa: BLE001 - fall back rather than fail to open
            print(f"settings: could not load settings:\n{traceback.format_exc()}", file=sys.stderr)
            return False

        self._form = ss.SourceForm.from_settings(settings.source)
        self._schedule = settings.blank_schedule
        self._interval_index = ss.interval_index(settings.rotation_interval_s)
        self._shuffle = settings.shuffle
        try:
            calibration = load_calibration_resolved().value
            self._chosen_display_id = calibration.target_display_id
        except Exception:  # noqa: BLE001 - the picker degrades to "no choice"
            self._chosen_display_id = ""
        self._refresh_displays()

        self._build_window()
        self._install_key_monitor()
        self._sync()
        self._window.makeKeyAndOrderFront_(None)
        AppKit.NSApp().activateIgnoringOtherApps_(True)
        return True

    @objc.python_method
    def _refresh_displays(self) -> None:
        """`Check again`. Re-enumerates and re-sorts; **never
        filters** — the heuristic sorts the list, it does not gate it."""
        self._displays = ss.display_options(display_target.screen_records())

    # -- building the window --------------------------------------------

    @objc.python_method
    def _build_window(self) -> None:
        """Manual frame layout, bottom-up in AppKit's coordinate space —
        the same approach `calibrate_window` takes, and for the same
        reason: this is a column of static rows, and a constraint graph
        would be more code to read and more to get wrong than the
        arithmetic it replaces.
        """
        content = AppKit.NSView.alloc().initWithFrame_(
            NSMakeRect(0, 0, WINDOW_WIDTH, 10.0)
        )
        y = 16.0

        y = self._build_status(content, y)
        y = self._build_blanking(content, y)
        y = self._build_display_picker(content, y)
        y = self._build_timing(content, y)
        y = self._build_sources(content, y)

        content_height = y
        content.setFrame_(NSMakeRect(0, 0, WINDOW_WIDTH, content_height))

        visible = min(content_height, MAX_CONTENT_HEIGHT)
        button_bar = 56.0
        window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WINDOW_WIDTH, visible + button_bar),
            AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable,
            AppKit.NSBackingStoreBuffered,
            False,
        )
        window.setTitle_("Settings")
        window.setReleasedWhenClosed_(False)
        window.setDelegate_(self)

        scroll = AppKit.NSScrollView.alloc().initWithFrame_(
            NSMakeRect(0, button_bar, WINDOW_WIDTH, visible)
        )
        scroll.setHasVerticalScroller_(True)
        scroll.setDrawsBackground_(False)
        scroll.setAutohidesScrollers_(True)
        scroll.setDocumentView_(content)
        window.contentView().addSubview_(scroll)

        # Save and Cancel live outside the scroll view, pinned to the
        # window. A primary action that scrolls out of sight is one users
        # report as missing.
        save = self._button("Save", "save:")
        save.setKeyEquivalent_("\r")
        save.setFrame_(NSMakeRect(WINDOW_WIDTH - 106.0, 14.0, 90.0, 32.0))
        cancel = self._button("Cancel", "cancel:")
        cancel.setFrame_(NSMakeRect(WINDOW_WIDTH - 200.0, 14.0, 90.0, 32.0))
        window.contentView().addSubview_(save)
        window.contentView().addSubview_(cancel)
        self._controls["save"] = save

        # An NSView is unflipped, so the stack built above runs bottom-up
        # and the scroll view opens showing its *end*. Scroll to the top
        # so the first thing read is the first thing written.
        content.scrollPoint_(AppKit.NSMakePoint(0.0, content_height))

        window.center()
        self._window = window

    # -- three source rows ---------------------------------------

    @objc.python_method
    def _build_sources(self, content, y: float) -> float:
        """Three always-visible radio rows, built bottom-up so they read
        top-down in the plan's order — folder first, and preselected."""
        # The Test button and its result line sit below the three rows.
        result = self._label("", size=11.0, secondary=True)
        result.setFrame_(NSMakeRect(INDENT + 96.0, y + 6.0, FIELD_WIDTH - 96.0, 32.0))
        content.addSubview_(result)
        self._controls["test_result"] = result

        test = self._button("Test", "testSource:")
        test.setFrame_(NSMakeRect(INDENT, y, 88.0, 28.0))
        content.addSubview_(test)
        self._controls["test"] = test
        y += 34.0 + 10.0

        for kind, title in reversed(ss.SOURCE_ROWS):
            y = self._build_source_row(content, y, kind, title)

        y = self._section_header(content, y, "Where pictures come from")
        return y

    @objc.python_method
    def _build_source_row(self, content, y: float, kind: str, title: str) -> float:
        if kind == source_settings.KIND_FOLDER:
            y = self._build_folder_options(content, y)
        elif kind == source_settings.KIND_JSON_URL:
            y = self._build_json_url_options(content, y)
        else:
            y = self._build_image_server_options(content, y)

        sublabel = self._label(ss.SOURCE_SUBLABELS[kind], size=11.0, secondary=True)
        sublabel.setFrame_(NSMakeRect(INDENT, y, FIELD_WIDTH, 30.0))
        content.addSubview_(sublabel)
        y += 30.0 + 2.0

        radio = AppKit.NSButton.alloc().init()
        radio.setButtonType_(AppKit.NSButtonTypeRadio)
        radio.setTitle_(title)
        radio.setTarget_(self)
        radio.setAction_("sourceRowChanged:")
        # The tag carries the kind's index, which is how the action knows
        # which row was clicked without comparing titles.
        radio.setTag_([k for k, _ in ss.SOURCE_ROWS].index(kind))
        radio.setFrame_(NSMakeRect(LEFT, y, FIELD_WIDTH, 20.0))
        content.addSubview_(radio)
        self._controls[f"radio_{kind}"] = radio
        return y + 20.0 + 8.0

    @objc.python_method
    def _build_folder_options(self, content, y: float) -> float:
        sort_popup = self._popup([label for label, _ in ss.SORT_ORDER_CHOICES], "sortOrderChanged:")
        sort_popup.setFrame_(NSMakeRect(INDENT + 74.0, y, 160.0, 26.0))
        content.addSubview_(sort_popup)
        self._controls["sort_order"] = sort_popup
        sort_label = self._label("Order:", size=12.0)
        sort_label.setFrame_(NSMakeRect(INDENT, y + 4.0, 70.0, 18.0))
        content.addSubview_(sort_label)
        self._controls["sort_order_label"] = sort_label
        y += 26.0 + 6.0

        subfolders = self._checkbox("Include subfolders", "includeSubfoldersChanged:")
        subfolders.setFrame_(NSMakeRect(INDENT, y, FIELD_WIDTH, 20.0))
        content.addSubview_(subfolders)
        self._controls["include_subfolders"] = subfolders
        y += 20.0 + 6.0

        choose = self._button("Choose…", "chooseFolder:")
        choose.setFrame_(NSMakeRect(WINDOW_WIDTH - LEFT - 92.0, y - 2.0, 92.0, 28.0))
        content.addSubview_(choose)
        self._controls["choose_folder"] = choose

        field = self._field("folderChanged:")
        field.setFrame_(NSMakeRect(INDENT, y, FIELD_WIDTH - 100.0, 24.0))
        content.addSubview_(field)
        self._controls["folder"] = field
        return y + 24.0 + 8.0

    @objc.python_method
    def _build_json_url_options(self, content, y: float) -> float:
        field = self._field("listUrlChanged:")
        field.setFrame_(NSMakeRect(INDENT, y, FIELD_WIDTH, 24.0))
        field.setPlaceholderString_("https://example.com/pictures.json")
        content.addSubview_(field)
        self._controls["list_url"] = field
        return y + 24.0 + 8.0

    @objc.python_method
    def _build_image_server_options(self, content, y: float) -> float:
        pool = self._popup([label for label, _ in ss.POOL_CHOICES], "poolChanged:")
        pool.setFrame_(NSMakeRect(INDENT + 74.0, y, 140.0, 26.0))
        content.addSubview_(pool)
        self._controls["pool"] = pool
        pool_label = self._label("Show:", size=12.0)
        pool_label.setFrame_(NSMakeRect(INDENT, y + 4.0, 70.0, 18.0))
        content.addSubview_(pool_label)
        self._controls["pool_label"] = pool_label
        y += 26.0 + 6.0

        field = self._field("baseUrlChanged:")
        field.setFrame_(NSMakeRect(INDENT, y, FIELD_WIDTH, 24.0))
        field.setPlaceholderString_("http://your-server:8883")
        content.addSubview_(field)
        self._controls["base_url"] = field
        return y + 24.0 + 8.0

    # -- timing --------------------------------------------------

    @objc.python_method
    def _build_timing(self, content, y: float) -> float:
        order = self._popup([label for label, _ in ss.ORDER_CHOICES], "orderChanged:")
        order.setFrame_(NSMakeRect(INDENT + 168.0, y, 160.0, 26.0))
        content.addSubview_(order)
        self._controls["order"] = order
        label = self._label("Order:", size=12.0)
        label.setFrame_(NSMakeRect(INDENT, y + 4.0, 160.0, 18.0))
        content.addSubview_(label)
        y += 26.0 + 8.0

        interval = self._popup([label for label, _ in ss.INTERVAL_CHOICES], "intervalChanged:")
        interval.setFrame_(NSMakeRect(INDENT + 168.0, y, 160.0, 26.0))
        content.addSubview_(interval)
        self._controls["interval"] = interval
        label = self._label("Show each picture for:", size=12.0)
        label.setFrame_(NSMakeRect(INDENT, y + 4.0, 160.0, 18.0))
        content.addSubview_(label)
        y += 26.0 + 8.0

        return self._section_header(content, y, "How pictures change")

    # -- display picker ------------------------------------------

    @objc.python_method
    def _build_display_picker(self, content, y: float) -> float:
        note = self._label("", size=11.0, secondary=True)
        note.setFrame_(NSMakeRect(INDENT, y, FIELD_WIDTH, 30.0))
        content.addSubview_(note)
        self._controls["display_note"] = note
        y += 30.0 + 4.0

        identify = self._button("Identify", "identifyDisplay:")
        identify.setFrame_(NSMakeRect(INDENT + 108.0, y, 100.0, 28.0))
        content.addSubview_(identify)
        check = self._button("Check again", "checkDisplaysAgain:")
        check.setFrame_(NSMakeRect(INDENT, y, 104.0, 28.0))
        content.addSubview_(check)
        y += 28.0 + 8.0

        picker = self._popup([], "displayChanged:")
        picker.setFrame_(NSMakeRect(INDENT, y, FIELD_WIDTH, 26.0))
        content.addSubview_(picker)
        self._controls["display"] = picker
        y += 26.0 + 8.0

        return self._section_header(content, y, "Which screen is the View")

    # -- blank schedule ------------------------------------------

    @objc.python_method
    def _build_blanking(self, content, y: float) -> float:
        backlight = self._label(ss.BACKLIGHT_NOTE, size=11.0, secondary=True)
        backlight.setFrame_(NSMakeRect(INDENT, y, FIELD_WIDTH, 30.0))
        content.addSubview_(backlight)
        y += 30.0 + 6.0

        state = self._label("", size=11.0, secondary=True)
        state.setFrame_(NSMakeRect(INDENT, y, FIELD_WIDTH, 16.0))
        content.addSubview_(state)
        self._controls["blank_state"] = state
        y += 16.0 + 8.0

        end = self._field("scheduleChanged:")
        end.setFrame_(NSMakeRect(INDENT + 250.0, y, 90.0, 24.0))
        content.addSubview_(end)
        self._controls["schedule_end"] = end
        and_label = self._label("and", size=12.0)
        and_label.setFrame_(NSMakeRect(INDENT + 214.0, y + 4.0, 34.0, 18.0))
        content.addSubview_(and_label)
        self._controls["schedule_and"] = and_label
        start = self._field("scheduleChanged:")
        start.setFrame_(NSMakeRect(INDENT + 120.0, y, 90.0, 24.0))
        content.addSubview_(start)
        self._controls["schedule_start"] = start
        between = self._label("Between", size=12.0)
        between.setFrame_(NSMakeRect(INDENT, y + 4.0, 116.0, 18.0))
        content.addSubview_(between)
        self._controls["schedule_between"] = between
        y += 24.0 + 8.0

        enabled = self._checkbox(
            "Blank the View automatically", "scheduleToggled:"
        )
        enabled.setFrame_(NSMakeRect(LEFT, y, FIELD_WIDTH, 20.0))
        content.addSubview_(enabled)
        self._controls["schedule_enabled"] = enabled
        y += 20.0 + 8.0

        return self._section_header(content, y, "Blanking")

    # -- status block and login checkboxes ------------------------

    @objc.python_method
    def _build_status(self, content, y: float) -> float:
        for key in ("status_4", "status_3", "status_2", "status_1", "status_0"):
            row = self._label("", size=11.0)
            row.setFrame_(NSMakeRect(INDENT, y, FIELD_WIDTH, 16.0))
            content.addSubview_(row)
            self._status_rows.insert(0, row)
            y += 16.0 + 2.0
        y += 8.0
        y = self._section_header(content, y, "Right now")

        # The action names carry **no underscores**: PyObjC maps a
        # Python method's internal underscores to colons, so a
        # `login_uiToggled_` method would be exported as the two-argument
        # selector `login:uiToggled:` and never fire. Costly to rediscover
        # and invisible until the click does nothing.
        for key, title, action in (
            (
                "login_ui",
                "Show these controls in the menu bar at login",
                "loginUiToggled:",
            ),
            (
                "login_display",
                "Show pictures on the View at login",
                "loginDisplayToggled:",
            ),
        ):
            note = self._label("", size=11.0, secondary=True)
            note.setFrame_(NSMakeRect(INDENT, y, FIELD_WIDTH, 16.0))
            content.addSubview_(note)
            self._controls[f"{key}_note"] = note
            y += 16.0 + 2.0

            box = self._checkbox(title, action)
            box.setFrame_(NSMakeRect(LEFT, y, FIELD_WIDTH, 20.0))
            content.addSubview_(box)
            self._controls[key] = box
            y += 20.0 + 6.0

        return self._section_header(content, y, "At login")

    # -- widget helpers --------------------------------------------------

    @objc.python_method
    def _section_header(self, content, y: float, title: str) -> float:
        label = self._label(title, size=13.0)
        label.setFont_(AppKit.NSFont.boldSystemFontOfSize_(13.0))
        label.setFrame_(NSMakeRect(LEFT, y, FIELD_WIDTH + 20.0, 18.0))
        content.addSubview_(label)
        return y + 18.0 + 14.0

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
    def _checkbox(self, title: str, action: str):
        box = AppKit.NSButton.alloc().init()
        box.setButtonType_(AppKit.NSButtonTypeSwitch)
        box.setTitle_(title)
        box.setTarget_(self)
        box.setAction_(action)
        return box

    @objc.python_method
    def _field(self, action: str):
        field = AppKit.NSTextField.alloc().init()
        field.setTarget_(self)
        field.setAction_(action)
        # Fires the action as the user types, not only on Return. The
        # Test button reads the form, and a URL typed but not committed
        # would otherwise be tested as an empty string.
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

    # -- syncing the UI to the model ------------------------------------

    @objc.python_method
    def _sync(self) -> None:
        """One direction only: model -> widgets. Every mutation goes
        through the model and then calls this.

        Widgets are never read back as state, for the reason
        `calibrate_window._sync_from_session` gives: a text field
        mid-edit holds a string, not a value.
        """
        controls = self._controls

        for kind, _ in ss.SOURCE_ROWS:
            controls[f"radio_{kind}"].setState_(
                AppKit.NSControlStateValueOn
                if kind == self._form.kind
                else AppKit.NSControlStateValueOff
            )

        self._set_text(controls["folder"], self._form.folder)
        self._set_text(controls["list_url"], self._form.list_url)
        self._set_text(controls["base_url"], self._form.base_url)
        controls["include_subfolders"].setState_(
            AppKit.NSControlStateValueOn
            if self._form.include_subfolders
            else AppKit.NSControlStateValueOff
        )
        controls["sort_order"].selectItemAtIndex_(
            ss.sort_order_index(self._form.sort_order)
        )
        controls["pool"].selectItemAtIndex_(ss.pool_index(self._form.pool))

        # Unselected rows' options are *disabled*, never hidden.
        folder_on = self._form.kind == source_settings.KIND_FOLDER
        url_on = self._form.kind == source_settings.KIND_JSON_URL
        studio_on = self._form.kind == source_settings.KIND_IMAGE_SERVER
        for key in ("folder", "choose_folder", "include_subfolders", "sort_order", "sort_order_label"):
            controls[key].setEnabled_(folder_on)
        controls["list_url"].setEnabled_(url_on)
        for key in ("base_url", "pool", "pool_label"):
            controls[key].setEnabled_(studio_on)

        controls["interval"].selectItemAtIndex_(self._interval_index)
        controls["order"].selectItemAtIndex_(ss.order_index(self._shuffle))

        self._sync_display_picker()
        self._sync_schedule()
        self._sync_status()
        self._sync_login_boxes()
        self._sync_test_result()

    @objc.python_method
    def _set_text(self, field, value: str) -> None:
        """Assign only when different, so the insertion point does not
        jump to the end of the field on every keystroke."""
        if field.stringValue() != value:
            field.setStringValue_(value)

    @objc.python_method
    def _sync_display_picker(self) -> None:
        picker = self._controls["display"]
        picker.removeAllItems()
        # The list is never gated. The first entry is the "let the
        # app decide" option, which is the shipped default and the right
        # answer for almost everyone.
        picker.addItemWithTitle_("Find it automatically")
        for option in self._displays:
            picker.addItemWithTitle_(option.title)

        index = 0
        for position, option in enumerate(self._displays):
            record = self._record_for(option)
            if record and record.get("display_id") == self._chosen_display_id:
                index = position + 1
                break
        if self._chosen_display_id and index == 0:
            # A previously-chosen display that is not attached right now.
            # Said out loud rather than silently reverting to automatic.
            picker.addItemWithTitle_("Your chosen screen (not connected)")
            index = picker.numberOfItems() - 1
        picker.selectItemAtIndex_(index)

        matched = any(option.probably_view for option in self._displays)
        self._controls["display_note"].setStringValue_(
            "" if matched else ss.nothing_matched_note()
        )

    @objc.python_method
    def _record_for(self, option: ss.DisplayOption) -> dict | None:
        for record in display_target.screen_records():
            if (
                record.get("name") == option.name
                and record.get("width") == option.width
                and record.get("height") == option.height
            ):
                return record
        return None

    @objc.python_method
    def _sync_schedule(self) -> None:
        controls = self._controls
        controls["schedule_enabled"].setState_(
            AppKit.NSControlStateValueOn
            if self._schedule.enabled
            else AppKit.NSControlStateValueOff
        )
        self._set_text(
            controls["schedule_start"],
            blank_schedule.format_minute(self._schedule.start_minute),
        )
        self._set_text(
            controls["schedule_end"],
            blank_schedule.format_minute(self._schedule.end_minute),
        )
        for key in ("schedule_start", "schedule_end", "schedule_between", "schedule_and"):
            controls[key].setEnabled_(self._schedule.enabled)
        controls["blank_state"].setStringValue_(self._effective_blank_state())

    @objc.python_method
    def _effective_blank_state(self) -> str:
        """"effective blank state" row and status line.

        Read from the *command* file rather than from anything this
        window holds, because the manual override is the menu bar's to
        write and may have changed since this window opened.
        """
        try:
            state = control.read_control(paths.command_path())
        except Exception:  # noqa: BLE001 - a status line never raises
            state = None
        if state is None:
            return blank_schedule.describe(None, 0.0, self._schedule)
        return blank_schedule.describe(
            state.blanked, state.written_at, self._schedule
        )

    @objc.python_method
    def _sync_status(self) -> None:
        status = ms.read_status(paths.status_path())
        raw = read_json_object(paths.status_path(), "status") or {}
        rows = ss.status_lines(raw, blank_state=self._effective_blank_state())
        for index, row in enumerate(self._status_rows):
            if index < len(rows):
                label, value = rows[index]
                row.setStringValue_(f"{label}: {value}")
            else:
                row.setStringValue_("")
        # `status` is read for its `present` flag alone: a status file
        # that has never existed means the display has never run, and
        # "Last checked: never" is the honest rendering of that.
        if not status.present:
            self._status_rows[0].setStringValue_(
                "Last checked: never — the display agent has not run yet."
            )

    @objc.python_method
    def _sync_login_boxes(self) -> None:
        """"Both must reflect actual `launchctl` state, not the
        last-written setting.\""""
        for key, label in (
            ("login_display", DISPLAY_AGENT_LABEL),
            ("login_ui", paths.UI_AGENT_LABEL),
        ):
            state = ss.agent_state(f"gui/{self._uid()}/{label}", self._launchctl)
            self._controls[key].setState_(
                AppKit.NSControlStateValueOn
                if state.checked
                else AppKit.NSControlStateValueOff
            )
            self._controls[f"{key}_note"].setStringValue_(
                ss.login_checkbox_note(state)
            )

    @objc.python_method
    def _sync_test_result(self) -> None:
        result = self._test_result
        label = self._controls["test_result"]
        if self._testing:
            label.setStringValue_("Checking…")
            label.setTextColor_(AppKit.NSColor.secondaryLabelColor())
        elif result is None:
            label.setStringValue_("")
        else:
            label.setStringValue_(result.message)
            label.setTextColor_(
                AppKit.NSColor.secondaryLabelColor()
                if result.ok
                else AppKit.NSColor.systemRedColor()
            )
        self._controls["test"].setEnabled_(not self._testing)
        self._controls["save"].setEnabled_(self._save_allowed())

    @objc.python_method
    def _save_allowed(self) -> bool:
        """"Save disabled on zero."

        Save is blocked while a test is running, while the selected row
        does not validate, and whenever the last test against the
        *current* form came back non-OK. A test result for a form that
        has since been edited is discarded rather than trusted — see
        `_invalidate_test`.
        """
        if self._testing:
            return False
        if self._form.to_settings() is None:
            return False
        if self._test_result is not None and not self._test_result.save_enabled:
            return False
        return True

    @objc.python_method
    def _invalidate_test(self) -> None:
        """Any edit to the source form drops the previous Test result.

        Without this, a user could Test a good URL, edit it to a bad one,
        and Save on the strength of the stale green result — which is
        exactly the "fat-fingering a URL after setup" case this
        validation exists to catch.
        """
        self._test_result = None
        self._test_token += 1
        self._dirty = True

    # -- actions: sources -----------------------------------------------

    def sourceRowChanged_(self, sender) -> None:
        try:
            index = int(sender.tag())
            self._form.kind = ss.SOURCE_ROWS[index][0]
            self._invalidate_test()
            self._sync()
        except Exception:
            print(f"sourceRowChanged_:\n{traceback.format_exc()}", file=sys.stderr)

    def folderChanged_(self, sender) -> None:
        try:
            self._form.folder = str(sender.stringValue())
            self._invalidate_test()
            self._sync_test_result()
        except Exception:
            print(f"folderChanged_:\n{traceback.format_exc()}", file=sys.stderr)

    def listUrlChanged_(self, sender) -> None:
        try:
            self._form.list_url = str(sender.stringValue())
            self._invalidate_test()
            self._sync_test_result()
        except Exception:
            print(f"listUrlChanged_:\n{traceback.format_exc()}", file=sys.stderr)

    def baseUrlChanged_(self, sender) -> None:
        try:
            self._form.base_url = str(sender.stringValue())
            self._invalidate_test()
            self._sync_test_result()
        except Exception:
            print(f"baseUrlChanged_:\n{traceback.format_exc()}", file=sys.stderr)

    def controlTextDidChange_(self, notification) -> None:
        """`setContinuous_(True)` covers most fields, but an
        `NSTextField`'s action does not fire on every keystroke for all
        input methods. The delegate callback does, so the two together
        mean the form never lags the visible text."""
        try:
            field = notification.object()
            for key, action in (
                ("folder", self.folderChanged_),
                ("list_url", self.listUrlChanged_),
                ("base_url", self.baseUrlChanged_),
                ("schedule_start", self.scheduleChanged_),
                ("schedule_end", self.scheduleChanged_),
            ):
                if self._controls.get(key) == field:
                    action(field)
                    return
        except Exception:
            print(f"controlTextDidChange_:\n{traceback.format_exc()}", file=sys.stderr)

    def includeSubfoldersChanged_(self, sender) -> None:
        try:
            self._form.include_subfolders = (
                sender.state() == AppKit.NSControlStateValueOn
            )
            self._invalidate_test()
            self._sync_test_result()
        except Exception:
            print(f"includeSubfoldersChanged_:\n{traceback.format_exc()}", file=sys.stderr)

    def sortOrderChanged_(self, sender) -> None:
        try:
            index = int(sender.indexOfSelectedItem())
            self._form.sort_order = ss.SORT_ORDER_CHOICES[index][1]
            self._dirty = True
        except Exception:
            print(f"sortOrderChanged_:\n{traceback.format_exc()}", file=sys.stderr)

    def poolChanged_(self, sender) -> None:
        try:
            index = int(sender.indexOfSelectedItem())
            self._form.pool = ss.POOL_CHOICES[index][1]
            # Pool changes what the source returns, so a previous Test is
            # no longer evidence — this is precisely the "Connected, but
            # no starred pictures. Try 'All'." path.
            self._invalidate_test()
            self._sync_test_result()
        except Exception:
            print(f"poolChanged_:\n{traceback.format_exc()}", file=sys.stderr)

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
            self._form.folder = str(url.path())
            self._invalidate_test()
            self._sync()
            # Test immediately. The panel is also what triggers the TCC
            # grant, so this is the moment the folder is readable
            # and the moment the answer is most useful.
            self.testSource_(None)
        except Exception:
            print(f"chooseFolder_:\n{traceback.format_exc()}", file=sys.stderr)

    def testSource_(self, sender) -> None:
        """Test button.

        The probe runs on a background thread: a folder read can block on
        a spinning disk or a network volume, and an HTTP probe can block
        for the full timeout. Doing either on the main thread would
        freeze the window — including the Cancel button — for seconds,
        during the one interaction whose whole purpose is to feel
        responsive.
        """
        try:
            self._testing = True
            self._test_result = None
            self._test_token += 1
            token = self._test_token
            # A snapshot, not the live form: the user can keep typing
            # while this runs, and the result must be attributed to what
            # was actually tested.
            form = ss.SourceForm(**vars(self._form))
            self._sync_test_result()
            thread = threading.Thread(
                target=self._run_test, args=(form, token), daemon=True
            )
            thread.start()
        except Exception:
            self._testing = False
            print(f"testSource_:\n{traceback.format_exc()}", file=sys.stderr)

    @objc.python_method
    def _run_test(self, form, token: int) -> None:
        """Background half of the Test button. Never raises: this is a
        bare thread, and an exception here would leave the window showing
        `Checking…` forever with no way back."""
        try:
            result = ss.probe(form)
        except Exception:  # noqa: BLE001 - see docstring
            print(f"settings: probe failed:\n{traceback.format_exc()}", file=sys.stderr)
            result = ss.TestResult(
                ss.Outcome.UNREACHABLE, "Could not reach that address"
            )
        AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(
            lambda: self._test_finished(result, token)
        )

    @objc.python_method
    def _test_finished(self, result, token: int) -> None:
        try:
            # A result from a superseded run is dropped. The token is
            # bumped by every edit and every new Test, so a slow probe
            # cannot overwrite the answer to a later, faster question.
            if token != self._test_token:
                return
            self._testing = False
            self._test_result = result
            self._sync_test_result()
        except Exception:
            print(f"_test_finished:\n{traceback.format_exc()}", file=sys.stderr)

    # -- actions: timing, picker, schedule -------------------------------

    def intervalChanged_(self, sender) -> None:
        try:
            self._interval_index = int(sender.indexOfSelectedItem())
            self._dirty = True
        except Exception:
            print(f"intervalChanged_:\n{traceback.format_exc()}", file=sys.stderr)

    def orderChanged_(self, sender) -> None:
        try:
            self._shuffle = ss.ORDER_CHOICES[int(sender.indexOfSelectedItem())][1]
            self._dirty = True
        except Exception:
            print(f"orderChanged_:\n{traceback.format_exc()}", file=sys.stderr)

    def displayChanged_(self, sender) -> None:
        try:
            index = int(sender.indexOfSelectedItem())
            if index <= 0:
                self._chosen_display_id = ""
            elif index - 1 < len(self._displays):
                record = self._record_for(self._displays[index - 1])
                self._chosen_display_id = (record or {}).get("display_id", "")
            self._dirty = True
        except Exception:
            print(f"displayChanged_:\n{traceback.format_exc()}", file=sys.stderr)

    def checkDisplaysAgain_(self, sender) -> None:
        try:
            self._refresh_displays()
            self._sync_display_picker()
        except Exception:
            print(f"checkDisplaysAgain_:\n{traceback.format_exc()}", file=sys.stderr)

    def identifyDisplay_(self, sender) -> None:
        """Identify. The implementation moved to `ui.identify`
        when the first-run flow needed the same action — see that module
        for why it flashes every display rather than the selected one."""
        try:
            identify.flash_screens()
        except Exception:
            print(f"identifyDisplay_:\n{traceback.format_exc()}", file=sys.stderr)

    def scheduleToggled_(self, sender) -> None:
        try:
            enabled = sender.state() == AppKit.NSControlStateValueOn
            self._schedule = ss.schedule_from_fields(
                enabled,
                self._controls["schedule_start"].stringValue(),
                self._controls["schedule_end"].stringValue(),
                self._schedule,
            )
            self._dirty = True
            self._sync_schedule()
        except Exception:
            print(f"scheduleToggled_:\n{traceback.format_exc()}", file=sys.stderr)

    def scheduleChanged_(self, sender) -> None:
        try:
            self._schedule = ss.schedule_from_fields(
                self._schedule.enabled,
                self._controls["schedule_start"].stringValue(),
                self._controls["schedule_end"].stringValue(),
                self._schedule,
            )
            self._dirty = True
            self._controls["blank_state"].setStringValue_(
                self._effective_blank_state()
            )
        except Exception:
            print(f"scheduleChanged_:\n{traceback.format_exc()}", file=sys.stderr)

    # -- actions: login checkboxes ---------------------------------------

    def loginDisplayToggled_(self, sender) -> None:
        try:
            self._toggle_agent(
                DISPLAY_AGENT_LABEL,
                sender.state() == AppKit.NSControlStateValueOn,
            )
        except Exception:
            print(f"loginDisplayToggled_:\n{traceback.format_exc()}", file=sys.stderr)

    def loginUiToggled_(self, sender) -> None:
        try:
            self._toggle_agent(
                paths.UI_AGENT_LABEL,
                sender.state() == AppKit.NSControlStateValueOn,
            )
        except Exception:
            print(f"loginUiToggled_:\n{traceback.format_exc()}", file=sys.stderr)

    @objc.python_method
    def _toggle_agent(self, label: str, enabled: bool) -> None:
        """Apply a login checkbox immediately, then re-read launchctl.

        Applied immediately rather than on Save because these are not
        settings — there is nothing in `settings.json` for them, and
        the boxes must reflect launchd's state rather than a
        stored intention. Re-reading afterwards is what makes a failed
        toggle visible: the box springs back to what is actually true
        instead of showing what was asked for.
        """
        ui_agent.set_enabled(label, enabled)
        self._sync_login_boxes()

    # -- save and close --------------------------------------------------

    def save_(self, sender) -> None:
        try:
            if self._save():
                self._finish()
        except Exception:
            print(f"save_:\n{traceback.format_exc()}", file=sys.stderr)

    @objc.python_method
    def _save(self) -> bool:
        """Two durable writes, both atomic, both merges.

        `settings.json` carries the source, timing and schedule;
        `calibration.json` carries the display choice, because the resolution order puts
        `target_screen` there deliberately — `get_view_screen` derives
        the target from that file, and storing the picker's answer in
        view-lab's private settings instead would let this app and
        another tool silently target different monitors with no way to
        detect it.
        """
        source = self._form.to_settings()
        if source is None:
            self._alert(
                "That source isn't complete.",
                "Fill in the option under the row you picked, then press "
                "Test.",
            )
            return False

        document = ss.settings_document(
            _settings_document(),
            source=source,
            rotation_interval_s=ss.interval_seconds(self._interval_index),
            shuffle=self._shuffle,
            schedule=self._schedule,
        )
        path = paths.settings_path()
        try:
            paths.ensure_dir(path.parent)
            atomic_write_json(path, document)
        except OSError:
            print(f"settings: save failed:\n{traceback.format_exc()}", file=sys.stderr)
            self._alert(
                "Couldn't save your settings.",
                f"Nothing was changed. Could not write {path}.",
            )
            return False

        if not self._save_display_choice():
            return False

        # `Check for new pictures now`, sent automatically: the
        # display hot-reloads settings.json within a tick and re-lists on
        # a source change by itself, but a *pool* or *sort order* change
        # is not a source change to the poll timer. Nudging the counter
        # makes every save take visible effect at once rather than some
        # of them waiting up to thirty minutes.
        self._request_refresh()
        print(f"settings: saved {path}.", file=sys.stderr)
        return True

    @objc.python_method
    def _save_display_choice(self) -> bool:
        """Write `target_screen` into calibration.json, preserving the
        rest of the document (is additive-only)."""
        path = paths.calibration_path()
        document = read_json_object(path, "calibration") or {}
        if not document:
            # No calibration file to merge into. The picker's choice is
            # not worth creating one from nothing — the circle numbers
            # would be missing and the display would fall back anyway.
            return True
        if self._chosen_display_id:
            document["target_screen"] = {
                "resolve_strategy": display_target.EXPLICIT_STRATEGY,
                "display_id": self._chosen_display_id,
            }
        else:
            document["target_screen"] = {
                "resolve_strategy": "match_by_resolution_excluding_main"
            }
        try:
            paths.ensure_dir(path.parent)
            atomic_write_json(path, document)
        except OSError:
            print(
                f"settings: could not write the screen choice:\n"
                f"{traceback.format_exc()}",
                file=sys.stderr,
            )
            self._alert(
                "Couldn't save which screen to use.",
                f"Your other settings were saved. Could not write {path}.",
            )
            return False
        return True

    @objc.python_method
    def _request_refresh(self) -> None:
        """Bump `refresh` counter. Read-modify-write of the whole
        command file, like every write in `menubar.py`: this process is
        not the only thing that touches it, and clobbering `blanked`
        because a settings window happened to save would be a genuinely
        confusing bug to chase."""
        try:
            path = paths.command_path()
            paths.ensure_dir(path.parent)
            data = read_json_object(path, "command") or {}
            data["refresh"] = int(data.get("refresh") or 0) + 1
            data["written_at"] = time.time()
            state = control.parse_control(data)
            if not control.write_control(path, state):
                print(f"settings: could not write {path}.", file=sys.stderr)
        except Exception:  # noqa: BLE001 - never fail a save over this
            print(f"settings: _request_refresh:\n{traceback.format_exc()}", file=sys.stderr)

    def cancel_(self, sender) -> None:
        try:
            self._finish()
        except Exception:
            print(f"cancel_:\n{traceback.format_exc()}", file=sys.stderr)

    def windowShouldClose_(self, sender) -> bool:
        try:
            self._finish()
        except Exception:
            print(f"windowShouldClose_:\n{traceback.format_exc()}", file=sys.stderr)
        return False

    @objc.python_method
    def _finish(self) -> None:
        """Tear the window down. Idempotent via `_closing`, matching
        `CalibrateController._finish`: Esc during a modal, or a second
        Cancel click, would otherwise remove the key monitor twice.

        **No unsaved-changes prompt.** the
        calibration window's confirmation is "the only confirmation in
        the app", and that is a deliberate budget rather than an
        oversight: closing this window changes nothing, everything in it
        is re-openable in one click, and nothing here is hand-measured
        the way a calibration is.
        """
        if self._closing:
            return
        self._closing = True
        if self._monitor is not None:
            AppKit.NSEvent.removeMonitor_(self._monitor)
            self._monitor = None
        if self._window is not None:
            self._window.orderOut_(None)
        self._window = None
        if self._on_close is not None:
            callback, self._on_close = self._on_close, None
            callback()

    # -- keyboard --------------------------------------------------------

    @objc.python_method
    def _install_key_monitor(self) -> None:
        def handler(event):
            try:
                if (
                    self._window is not None
                    and self._window.isKeyWindow()
                    and event.keyCode() == KEY_CODE_ESCAPE
                ):
                    self.cancel_(None)
                    return None
            except Exception:  # noqa: BLE001 - an AppKit callback
                print(f"settings: key handler:\n{traceback.format_exc()}", file=sys.stderr)
            return event

        self._monitor = AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            AppKit.NSEventMaskKeyDown, handler
        )

    # -- helpers ---------------------------------------------------------

    @objc.python_method
    def _uid(self) -> int:
        import os

        return os.getuid()

    @objc.python_method
    def _launchctl(self, args: list[str]) -> tuple[int, str]:
        """Run one launchctl subcommand. argv list, never `shell=True`. Returns `(returncode, combined output)`.

        Blocks the main thread for up to `timeout`, which is acceptable
        here for the same reason it is in `menubar.py`: it is bounded, it
        runs only when the window opens or a checkbox is clicked, and
        `launchctl print` on a healthy domain returns in milliseconds.
        """
        try:
            done = subprocess.run(
                ["/bin/launchctl", *args],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return 1, str(exc)
        return done.returncode, f"{done.stdout}\n{done.stderr}"

    @objc.python_method
    def _alert(self, message: str, informative: str) -> None:
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_(message)
        alert.setInformativeText_(informative)
        alert.addButtonWithTitle_("OK")
        AppKit.NSApp().activateIgnoringOtherApps_(True)
        alert.runModal()


def main() -> int:
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
    controller = SettingsController.alloc().init()
    if not controller.start(on_close=lambda: AppKit.NSApp().terminate_(None)):
        return 1
    app.setDelegate_(controller)
    print("settings: running.", file=sys.stderr)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
