"""The calibration window. The centerpiece feature.

Run standalone: `display/.venv/bin/python3 ui/calibrate_window.py`
(normally opened from the menu bar's `Adjust the circle…`.)

**Two windows, not one.**

- A borderless overlay on the View itself, at
  `NSScreenSaverWindowLevel + 1` — one level above the display agent's
  own window — drawing two rings over whatever picture the
  display is currently showing. Transparent, ignores mouse events, and
  never becomes key: the user is looking at it, not clicking on it.
- A normal titled controls window on whatever screen the user is
  actually sitting at, with the numeric fields, the legend, and the
  buttons.

They are separate because the thing you look at is two inches wide and
the thing you type into cannot be. escape hatch assumes this
split too: "if someone picks the wrong display, a borderless
screensaver-level window lands on their main monitor with the controls
behind it, and they need a stated way out" — Esc closes both, from
either.

**The file is written exactly once, on Save.** Every nudge goes
out as transient `preview_calibration` in `~/.viewlab/state/command.json`
and is applied by the display agent's `apply_preview`; nothing touches
`~/.viewlab/calibration.json` until the Save button. This is not an
optimisation. Writing per keypress would make every intermediate value
durable, leave no real Cancel, persist a half-nudged circle on a crash,
and — because that file is shared — make any other reader jitter along
with every arrow key.

**Conventions this file follows**, each already paid for elsewhere in
this project: `@objc.python_method` on every private helper of an
`NSObject` subclass; a broad `try/except` around the body of every
selector AppKit can call; atomic write-then-rename for every write;
reads that never raise. No emoji and no icons — rule covers every
label and control here, and the only graphics are the two rings
themselves.
"""

from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path

import AppKit
import objc
from Foundation import NSMakePoint, NSMakeRect

from display import control, paths
from display.atomic_io import atomic_write_json
from display.calibration import load_calibration_resolved
from display.config_store import read_json_object
from display.display_target import get_view_screen
from ui import calibrate_state as cs

#: How often the overlay re-reads nothing at all — it does not poll. The
#: overlay redraws when the session changes, which is the only time it
#: can have changed. Left as a named absence because every other window
#: in this project has a timer and the missing one is worth explaining.

#: Pass/fail criterion, lifted verbatim from
#: `display/STEP1_INSTRUCTIONS.md` part (c). It stays on screen for the
#: whole session rather than appearing on demand: it is the only thing in
#: the window that says what "done" looks like, and a user who has to go
#: find it has already decided what they think and gone looking for
#: agreement.
PASS_FAIL_TEXT = (
    "Right: the bright ring sits where the black bezel starts, all the way "
    "round.\n"
    "Wrong: it disappears under the bezel, leaves a gap of dead panel, or is "
    "closer to the bezel on one side than the other."
)

#: Stated as one plain line in the window rather than discovered.
KEY_HELP_TEXT = (
    "Arrow keys nudge the selected box by 1. Hold Shift to nudge by 10. "
    "Command-Z undoes, Shift-Command-Z redoes. Esc closes."
)

OUTER_RING_LABEL = "Line this up with the edge of the glass."
INNER_RING_LABEL = "Pictures fill this."

#: Field order and prose labels. naming rule applies — these are
#: read by someone holding a 2.1-inch screen, not by a developer, so
#: "Center, left to right" beats "center_x" even though the JSON key is
#: the latter.
FIELD_LABELS: tuple[tuple[str, str], ...] = (
    ("center_x", "Center, left to right"),
    ("center_y", "Center, top to bottom"),
    ("radius_px", "Radius to the edge of the glass"),
)

CONTROLS_WIDTH = 460.0

#: Esc. Checked by key code rather than by character, because the
#: character for Esc is not typeable in a source file and
#: `charactersIgnoringModifiers` for it varies with the input source.
KEY_CODE_ESCAPE = 53


def _calibration_document() -> dict:
    """The current `~/.viewlab/calibration.json` as a plain dict, or {}.

    Read raw rather than through `load_calibration_resolved()` because
    v1 is additive-only, so Save has to preserve keys this
    version does not understand — and a validated `Calibration` has
    already dropped them."""
    return read_json_object(paths.calibration_path(), "calibration") or {}


# -- the overlay --------------------------------------------------------


class RingOverlayView(AppKit.NSView):
    """Draws two rings, and nothing else.

    `geometry` is set as a plain attribute after construction, matching
    `window.py`'s `CircularImageView.calibration` convention. `None`
    means "not ready yet" and draws nothing rather than guessing — a ring
    at a guessed radius is worse than no ring, because the user cannot
    tell the difference and will calibrate against it.

    The view is fully transparent apart from the rings themselves: the
    picture underneath is the real-image preview, drawn by
    the display agent at the *inner* radius while this view puts the
    outer ring on top of it. That is the whole mechanism by which the
    33-pixel gap reads as designed rather than as an error.
    """

    geometry: cs.RingGeometry | None = None

    def isOpaque(self) -> bool:
        return False

    def acceptsFirstResponder(self) -> bool:
        # Every key press belongs to the controls window. If this view
        # could take focus, arrow keys would go to whichever window the
        # user last clicked, which on a screen with no visible controls
        # is not a question they can answer.
        return False

    def drawRect_(self, dirty_rect) -> None:
        """Broad except for the same reason every selector body in this
        project has one: this is called by AppKit's display machinery,
        and an exception escaping it is at best logged where nobody
        looks. A frame that fails to draw its rings must not take the
        window with it."""
        try:
            self._draw()
        except Exception:  # noqa: BLE001 - see docstring
            print(
                f"RingOverlayView.drawRect_: {traceback.format_exc()}",
                file=sys.stderr,
            )

    @objc.python_method
    def _draw(self) -> None:
        geometry = self.geometry
        if geometry is None:
            return
        AppKit.NSColor.clearColor().set()
        AppKit.NSRectFill(self.bounds())

        # Inner ring first, so that where the two nearly coincide (a
        # safety margin near 1.0) the bright one is what survives on top
        # — bright ring is the one being aligned, and it is the
        # one that must never be ambiguous.
        inner = AppKit.NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(*geometry.inner_rect())
        )
        inner.setLineWidth_(2.0)
        # Dashed as well as dimmer. Brightness alone is a weak signal
        # against an arbitrary photograph — a dim ring over a dark image
        # is invisible and over a bright one reads as the main ring —
        # and the dash pattern survives both. Not an icon: this is the
        # drawing, not a label.
        inner.setLineDash_count_phase_([6.0, 6.0], 2, 0.0)
        AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(
            1.0, 1.0, 1.0, 0.45
        ).set()
        inner.stroke()

        outer = AppKit.NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(*geometry.outer_rect())
        )
        outer.setLineWidth_(3.0)
        AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(
            1.0, 1.0, 1.0, 1.0
        ).set()
        outer.stroke()

        self._draw_labels(geometry)

    @objc.python_method
    def _draw_labels(self, geometry: cs.RingGeometry) -> None:
        """"labeled in plain text", on the overlay itself.

        **These are barely legible on the View and that is understood.**
        The panel is 2.1 inches across at 960 pixels — roughly 457 PPI —
        so 15-pixel text is about a millimetre tall. The controls window
        carries the same two strings at a readable size, and that is
        where the user actually reads them.

        They are drawn here anyway for the case where the
        overlay lands on the *wrong* display, it is a borderless white
        ring on a stranger's main monitor with no title bar and no
        explanation. Two lines of text is the difference between "what is
        this" and "oh, that is the calibration overlay, and Esc closes
        it." Stacked low and inside the inner ring so they never sit on
        the ring edges the eye is judging.
        """
        lines = (
            f"Bright ring: {OUTER_RING_LABEL}",
            f"Dashed ring: {INNER_RING_LABEL}",
            "Esc closes this.",
        )
        font = AppKit.NSFont.systemFontOfSize_(15.0)
        attributes = {
            AppKit.NSFontAttributeName: font,
            AppKit.NSForegroundColorAttributeName: AppKit.NSColor.whiteColor(),
        }
        # Bottom of the stack sits a fifth of the inner radius up from
        # the center, which keeps all three lines inside the inner circle
        # at every radius this window can produce.
        y = geometry.center_y - geometry.inner_radius * 0.55
        for text in reversed(lines):
            string = AppKit.NSString.stringWithString_(text)
            size = string.sizeWithAttributes_(attributes)
            origin_x = geometry.center_x - float(size.width) / 2.0
            plate = NSMakeRect(
                origin_x - 6.0, y - 3.0, float(size.width) + 12.0,
                float(size.height) + 6.0,
            )
            AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(
                0.0, 0.0, 0.0, 0.55
            ).set()
            AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                plate, 4.0, 4.0
            ).fill()
            string.drawAtPoint_withAttributes_(
                NSMakePoint(origin_x, y), attributes
            )
            y += float(size.height) + 10.0


def build_overlay_window(screen) -> AppKit.NSWindow:
    """A borderless, transparent, click-through window covering `screen`.

    `NSScreenSaverWindowLevel + 1` puts it exactly one level above the
    display agent's window (`window.py` sets `NSScreenSaverWindowLevel`)
    — high enough to be seen over the picture, and not so high that it
    outranks a system alert the user needs to read.

    `setIgnoresMouseEvents_(True)` matters more than it looks: without
    it, a full-screen transparent window over the View swallows clicks in
    that region, and on a display the user cannot see a cursor on, those
    clicks simply vanish.
    """
    window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_screen_(
        screen.frame(),
        AppKit.NSWindowStyleMaskBorderless,
        AppKit.NSBackingStoreBuffered,
        False,
        screen,
    )
    window.setLevel_(AppKit.NSScreenSaverWindowLevel + 1)
    window.setOpaque_(False)
    window.setBackgroundColor_(AppKit.NSColor.clearColor())
    window.setIgnoresMouseEvents_(True)
    window.setHasShadow_(False)
    # Without this the overlay disappears the moment the controls window
    # takes focus, which is immediately and permanently.
    window.setHidesOnDeactivate_(False)
    # Same collection behavior as the display agent's own window: the
    # View has no Space of its own, and without CanJoinAllSpaces the
    # overlay vanishes as soon as the user switches Space to get back to
    # the controls.
    window.setCollectionBehavior_(
        AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
        | AppKit.NSWindowCollectionBehaviorStationary
        | AppKit.NSWindowCollectionBehaviorIgnoresCycle
    )
    view = RingOverlayView.alloc().initWithFrame_(
        NSMakeRect(0, 0, screen.frame().size.width, screen.frame().size.height)
    )
    window.setContentView_(view)
    # **Required, and not redundant with the initializer.** Verified on
    # the device: with the `screen:` initializer alone the overlay was
    # composited somewhere that was not the View — the rings simply did
    # not appear in a `screencapture -D 2`. `window.py`'s `build_window`
    # carries the same trailing `setFrame_display_` for the same reason;
    # the contentRect passed to the initializer is not the last word on
    # where a borderless window lands.
    window.setFrame_display_(screen.frame(), True)
    return window


# -- the controls -------------------------------------------------------


class CalibrateController(AppKit.NSObject):
    """Owns both windows, the session, and the control-file writes.

    `alloc().init()` then `start()`, matching `MenuBarController`: a
    failure to resolve the View's screen is a logged error and an alert,
    not an exception thrown out of `init`.
    """

    def init(self):
        self = objc.super(CalibrateController, self).init()
        if self is None:
            return None
        self._session = None
        self._overlay = None
        self._window = None
        self._fields = {}
        self._steppers = {}
        self._effective_label = None
        self._buttons = {}
        self._monitor = None
        self._on_close = None
        self._closing = False
        return self

    # -- setup ---------------------------------------------------------

    @objc.python_method
    def start(self, on_close=None) -> bool:
        """Open both windows. Returns whether anything opened.

        `on_close` lets the menu bar drop its reference when the window
        goes away, so that `Adjust the circle…` opens a fresh session
        next time rather than re-showing a stale one.
        """
        self._on_close = on_close
        resolved = load_calibration_resolved()
        calibration = resolved.value
        document = _calibration_document()

        bounds = cs.bounds_from_document(
            document,
            cs.Bounds(
                width=calibration.framebuffer_width,
                height=calibration.framebuffer_height,
            ),
        )
        saved = cs.circle_from_document(document) or cs.CircleValues(
            center_x=calibration.center_x,
            center_y=calibration.center_y,
            radius_px=calibration.radius_px,
        )
        defaults = self._shipped_defaults(saved)
        self._session = cs.CalibrationSession(
            saved=saved,
            defaults=defaults,
            bounds=bounds,
            safety_margin_pct=calibration.safety_margin_pct,
        )

        screen = get_view_screen(calibration)
        if screen is None:
            # The menu item is disabled while `View not connected`, so
            # this is the race — unplugged between the click and here —
            # rather than the common case. Still worth its own sentence:
            # opening a calibration window with nothing to calibrate
            # against is how someone ends up nudging numbers for a
            # minute before noticing.
            self._alert(
                "The View isn't connected.",
                "Plug the View back in and open Adjust the circle again.",
            )
            return False

        self._overlay = build_overlay_window(screen)
        self._build_controls_window()
        self._install_key_monitor()
        self._sync_from_session()
        self._overlay.orderFrontRegardless()
        self._window.makeKeyAndOrderFront_(None)
        AppKit.NSApp().activateIgnoringOtherApps_(True)
        # Push the opening values immediately. Without this the device
        # shows the saved circle while the window shows the same numbers
        # — identical, so harmless — right up until the first nudge, at
        # which point a single write has to carry both the preview
        # mechanism and the change. Sending one now means the first
        # nudge is exercising a path that has already worked once.
        self._write_preview()
        return True

    @objc.python_method
    def _shipped_defaults(self, fallback: cs.CircleValues) -> cs.CircleValues:
        """"Reset to defaults" — the numbers the app ships, from
        the bundled seed, **not** the conservative built-in fallback.

        The rule is specific: "Reset restores the shipped numbers." The
        built-in fallback (centered, 90% radius) is a different thing
        that exists for a corrupt file, and resetting to it would move
        the circle somewhere no device has ever been calibrated to.
        """
        bundled = read_json_object(paths.bundled_calibration_path(), "calibration")
        return cs.circle_from_document(bundled) or fallback

    @objc.python_method
    def _build_controls_window(self) -> None:
        """Manual frame layout, bottom-up in AppKit's coordinate space.

        No autolayout: this is one fixed-size window of static rows, and
        a constraint graph would be more code to read and more to get
        wrong than the arithmetic it replaces.
        """
        y = 16.0
        rows: list[tuple[AppKit.NSView, float]] = []

        def stack(view, height: float, gap: float = 10.0) -> None:
            nonlocal y
            rows.append((view, y))
            y += height + gap

        # Bottom row: Cancel and Save, right-aligned, Save as default.
        save = self._button("Save", "save:")
        cancel = self._button("Cancel", "cancel:")
        save.setKeyEquivalent_("\r")
        save.setFrame_(NSMakeRect(CONTROLS_WIDTH - 106.0, y, 90.0, 32.0))
        cancel.setFrame_(NSMakeRect(CONTROLS_WIDTH - 200.0, y, 90.0, 32.0))
        rows.append((save, None))
        rows.append((cancel, None))
        self._buttons["save"] = save
        self._buttons["cancel"] = cancel
        y += 32.0 + 14.0

        # escape hatches. "Revert to saved" is listed **above**
        # "Reset to defaults" and worded differently on purpose: they are
        # one keystroke apart in effect and a world apart in consequence,
        # and the plan is explicit that the former is what people
        # actually want.
        restore = self._button("Restore previous calibration", "restorePrevious:")
        restore.setFrame_(NSMakeRect(16.0, y, CONTROLS_WIDTH - 32.0, 28.0))
        rows.append((restore, None))
        self._buttons["restore"] = restore
        y += 28.0 + 6.0

        reset = self._button("Reset to defaults", "resetDefaults:")
        reset.setFrame_(NSMakeRect(16.0, y, CONTROLS_WIDTH - 32.0, 28.0))
        rows.append((reset, None))
        self._buttons["reset"] = reset
        y += 28.0 + 6.0

        revert = self._button("Revert to saved", "revertSaved:")
        revert.setFrame_(NSMakeRect(16.0, y, CONTROLS_WIDTH - 32.0, 28.0))
        rows.append((revert, None))
        self._buttons["revert"] = revert
        y += 28.0 + 16.0

        help_label = self._label(KEY_HELP_TEXT, size=11.0, secondary=True)
        help_label.setFrame_(NSMakeRect(16.0, y, CONTROLS_WIDTH - 32.0, 32.0))
        rows.append((help_label, None))
        y += 32.0 + 14.0

        self._effective_label = self._label("", size=11.0, secondary=True)
        self._effective_label.setFrame_(
            NSMakeRect(16.0, y, CONTROLS_WIDTH - 32.0, 16.0)
        )
        rows.append((self._effective_label, None))
        y += 16.0 + 12.0

        # The three fields, bottom-up so radius ends up last visually.
        for field, title in reversed(FIELD_LABELS):
            label = self._label(title, size=12.0)
            label.setFrame_(NSMakeRect(16.0, y + 4.0, 220.0, 18.0))
            rows.append((label, None))

            text = AppKit.NSTextField.alloc().initWithFrame_(
                NSMakeRect(246.0, y, 90.0, 24.0)
            )
            text.setAlignment_(AppKit.NSTextAlignmentRight)
            text.setTarget_(self)
            text.setAction_("fieldChanged:")
            text.setTag_(cs.FIELDS.index(field))
            rows.append((text, None))
            self._fields[field] = text

            stepper = AppKit.NSStepper.alloc().initWithFrame_(
                NSMakeRect(342.0, y, 19.0, 24.0)
            )
            stepper.setMinValue_(0.0)
            stepper.setMaxValue_(
                max(self._session.bounds.width, self._session.bounds.height)
            )
            stepper.setIncrement_(cs.NUDGE_STEP)
            stepper.setValueWraps_(False)
            stepper.setTarget_(self)
            stepper.setAction_("stepperChanged:")
            stepper.setTag_(cs.FIELDS.index(field))
            rows.append((stepper, None))
            self._steppers[field] = stepper
            y += 24.0 + 10.0

        y += 8.0
        criterion = self._label(PASS_FAIL_TEXT, size=11.0)
        criterion.setFrame_(NSMakeRect(16.0, y, CONTROLS_WIDTH - 32.0, 60.0))
        rows.append((criterion, None))
        y += 60.0 + 12.0

        legend = self._label(
            f"Bright ring — {OUTER_RING_LABEL}\n"
            f"Dashed ring — {INNER_RING_LABEL}",
            size=12.0,
        )
        legend.setFrame_(NSMakeRect(16.0, y, CONTROLS_WIDTH - 32.0, 36.0))
        rows.append((legend, None))
        y += 36.0 + 16.0

        height = y
        window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, CONTROLS_WIDTH, height),
            AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable,
            AppKit.NSBackingStoreBuffered,
            False,
        )
        window.setTitle_("Adjust the circle")
        window.setReleasedWhenClosed_(False)
        window.setDelegate_(self)
        content = window.contentView()
        for view, _ in rows:
            content.addSubview_(view)
        window.center()
        self._window = window

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
        # Multi-line labels need this; single-line ones are unaffected.
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

    # -- keyboard ------------------------------------------------------

    @objc.python_method
    def _install_key_monitor(self) -> None:
        """A local key monitor for arrows, Esc and Cmd-Z.

        A monitor rather than `keyDown_` on a view, because nudge
        has to reach the **focused text field**, and a focused
        `NSTextField` has already handed its key events to a field editor
        that consumes arrow keys for cursor movement. Intercepting ahead
        of the responder chain is the only place the arrow keys are still
        available, and it is also the only place Esc is guaranteed to be
        seen from either window.
        """

        def handler(event):
            try:
                return self._handle_key(event)
            except Exception:  # noqa: BLE001 - an AppKit callback
                print(
                    f"calibrate: key handler:\n{traceback.format_exc()}",
                    file=sys.stderr,
                )
                return event

        self._monitor = AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            AppKit.NSEventMaskKeyDown, handler
        )

    @objc.python_method
    def _handle_key(self, event):
        """Return `None` to swallow the event, or `event` to pass it on."""
        if self._session is None:
            return event
        # Only while this controller's controls window has key focus.
        # Without the check the monitor would eat arrow keys app-wide,
        # including in the menu bar's own open menu.
        #
        # `isKeyWindow()` rather than `NSApp().keyWindow() is
        # self._window`. **PyObjC hands back a fresh proxy object for the
        # same underlying window**, so the identity comparison is False
        # even when it is the same window — verified on the device, where
        # it silently swallowed every arrow key and made the whole
        # feature look like it did nothing. Asking the window about
        # itself has no proxy to get wrong.
        if self._window is None or not self._window.isKeyWindow():
            return event

        if event.keyCode() == KEY_CODE_ESCAPE:
            self.closeSession_(None)
            return None

        flags = event.modifierFlags()
        command = bool(flags & AppKit.NSEventModifierFlagCommand)
        shift = bool(flags & AppKit.NSEventModifierFlagShift)
        characters = event.charactersIgnoringModifiers() or ""

        if command and characters.lower() == "z":
            if shift:
                self._session.redo()
            else:
                self._session.undo()
            self._sync_from_session()
            self._write_preview()
            return None

        field = self._focused_field()
        if field is None:
            return event
        step = cs.NUDGE_STEP_LARGE if shift else cs.NUDGE_STEP
        # AppKit's arrow key codes. Up/Right increase, Down/Left
        # decrease — the same direction the stepper moves, so the two
        # controls never disagree about which way is up.
        if event.keyCode() == 126 or event.keyCode() == 124:  # up, right
            delta = step
        elif event.keyCode() == 125 or event.keyCode() == 123:  # down, left
            delta = -step
        else:
            return event
        if self._session.nudge(field, delta):
            self._sync_from_session()
            self._write_preview()
        return None

    @objc.python_method
    def _focused_field(self) -> str | None:
        """Which of the three fields has focus, if any.

        A focused `NSTextField` is not itself the first responder — its
        *field editor*, a shared `NSTextView`, is, with the field as
        delegate. Walking back through the delegate is the documented way
        to answer this and the reason this is a helper rather than a
        comparison at the call site.

        Compared with `==`, never `is`, for the same PyObjC
        proxy-identity reason documented in `_handle_key`: these objects
        come back out of AppKit, so the Python-level identity of the
        wrapper is not a fact about which control has focus.
        """
        if self._window is None:
            return None
        responder = self._window.firstResponder()
        if responder is None:
            return None
        target = responder
        if isinstance(responder, AppKit.NSTextView):
            delegate = responder.delegate()
            if delegate is not None:
                target = delegate
        for field, text in self._fields.items():
            if target == text:
                return field
        return None

    # -- the control channel (transient, never the durable file) --

    @objc.python_method
    def _write_preview(self, clear: bool = False) -> None:
        """Push the current values down the channel as
        `preview_calibration`, or clear it.

        Read-modify-write of the whole command file, like every write in
        `menubar.py` and for the same reason: this process is not the
        only thing that touches it, and clobbering `blanked` or `paused`
        because a calibration window happened to be open would be a
        genuinely confusing bug to chase.
        """
        try:
            path = paths.command_path()
            paths.ensure_dir(path.parent)
            data = read_json_object(path, "command") or {}
            data["preview_calibration"] = (
                None if clear or self._session is None
                else self._session.preview_payload()
            )
            data["written_at"] = time.time()
            state = control.parse_control(data)
            if not control.write_control(path, state):
                print(f"calibrate: could not write {path}.", file=sys.stderr)
        except Exception:  # noqa: BLE001 - never take the window down
            print(f"calibrate: _write_preview:\n{traceback.format_exc()}", file=sys.stderr)

    # -- syncing the UI to the session ---------------------------------

    @objc.python_method
    def _sync_from_session(self) -> None:
        """One direction only: session -> widgets, overlay, buttons.

        Every mutation goes through the session and then calls this.
        Widgets are never read back as state — a text field mid-edit
        holds a string, not a value, and treating it as the source of
        truth is how a half-typed `4` becomes a radius.
        """
        if self._session is None:
            return
        values = self._session.values
        for field, text in self._fields.items():
            rendered = cs.format_value(values.get(field))
            if text.stringValue() != rendered:
                text.setStringValue_(rendered)
            self._steppers[field].setDoubleValue_(values.get(field))

        geometry = self._session.geometry()
        if self._effective_label is not None:
            self._effective_label.setStringValue_(
                f"Pictures fill a radius of "
                f"{cs.format_value(geometry.inner_radius)}, which is "
                f"{cs.format_value(geometry.gap_px)} inside the bright ring."
            )
        if self._overlay is not None:
            view = self._overlay.contentView()
            view.geometry = geometry
            view.setNeedsDisplay_(True)

        self._buttons["save"].setEnabled_(self._session.dirty)
        self._buttons["revert"].setEnabled_(self._session.dirty)
        self._buttons["restore"].setEnabled_(self._backup_values() is not None)

    @objc.python_method
    def _backup_values(self) -> cs.CircleValues | None:
        """`calibration.json.bak`, or None if there isn't a usable
        one. Never raises — a missing or corrupt backup just means the
        button stays disabled."""
        return cs.circle_from_document(
            read_json_object(self._backup_path(), "calibration backup")
        )

    @objc.python_method
    def _backup_path(self) -> Path:
        return paths.calibration_path().with_suffix(".json.bak")

    # -- actions -------------------------------------------------------

    def fieldChanged_(self, sender) -> None:
        """A text field committed (Return, or focus left it)."""
        try:
            if self._session is None:
                return
            field = cs.FIELDS[int(sender.tag())]
            self._session.set_field(field, _parse_number(sender.stringValue()))
            # Sync unconditionally, even when the value did not change:
            # the field may contain `472.0000` or ` 472 `, and the user
            # is owed the canonical rendering back.
            self._sync_from_session()
            self._write_preview()
        except Exception:
            print(f"fieldChanged_:\n{traceback.format_exc()}", file=sys.stderr)

    def stepperChanged_(self, sender) -> None:
        try:
            if self._session is None:
                return
            field = cs.FIELDS[int(sender.tag())]
            if self._session.set_field(field, sender.doubleValue()):
                self._sync_from_session()
                self._write_preview()
            else:
                # Clamped: put the stepper back where the session says,
                # or it keeps its own out-of-range value and the next
                # click steps from a number the user never saw.
                self._sync_from_session()
        except Exception:
            print(f"stepperChanged_:\n{traceback.format_exc()}", file=sys.stderr)

    def revertSaved_(self, sender) -> None:
        try:
            if self._session is not None and self._session.revert_to_saved():
                self._sync_from_session()
                self._write_preview()
        except Exception:
            print(f"revertSaved_:\n{traceback.format_exc()}", file=sys.stderr)

    def resetDefaults_(self, sender) -> None:
        try:
            if self._session is not None and self._session.reset_to_defaults():
                self._sync_from_session()
                self._write_preview()
        except Exception:
            print(f"resetDefaults_:\n{traceback.format_exc()}", file=sys.stderr)

    def restorePrevious_(self, sender) -> None:
        """"Restore previous calibration".

        Loads the backup into the session as an ordinary, undoable edit
        rather than writing it to disk. Everything in this window is
        transient until Save, and an escape hatch that was itself
        durable would be the one control that could not be escaped from.
        """
        try:
            values = self._backup_values()
            if values is None or self._session is None:
                return
            if self._session.set_values(values):
                self._sync_from_session()
                self._write_preview()
        except Exception:
            print(f"restorePrevious_:\n{traceback.format_exc()}", file=sys.stderr)

    def save_(self, sender) -> None:
        try:
            if self._save():
                self._finish()
        except Exception:
            print(f"save_:\n{traceback.format_exc()}", file=sys.stderr)

    def cancel_(self, sender) -> None:
        try:
            self.closeSession_(sender)
        except Exception:
            print(f"cancel_:\n{traceback.format_exc()}", file=sys.stderr)

    def closeSession_(self, sender) -> None:
        """Esc, Cancel, and the window's close button all land here.

        Closing with unsaved changes reverts the device and prompts
        Save / Discard / Cancel. **This is the only confirmation in the
        app**, so a clean close asks nothing — the prompt appearing at
        all is the signal that there is something to lose.
        """
        try:
            if self._session is None or not self._session.dirty:
                self._finish()
                return
            # Revert the device *first*, before the modal. The alert
            # blocks this thread, and leaving the View showing nudged
            # values behind a dialog that says "unsaved changes" invites
            # exactly the wrong answer: the user looks at the device,
            # sees what they wanted, and clicks Discard.
            self._write_preview(clear=True)
            choice = self._confirm_close()
            if choice == "cancel":
                # Put the preview back — they are still working.
                self._write_preview()
                return
            if choice == "save" and not self._save():
                return
            self._finish()
        except Exception:
            print(f"closeSession_:\n{traceback.format_exc()}", file=sys.stderr)

    def windowShouldClose_(self, sender) -> bool:
        """The red close button. Always returns False and routes through
        `closeSession_`, so that the confirmation cannot be bypassed by
        the one control that closes windows without asking anything."""
        try:
            self.closeSession_(sender)
        except Exception:
            print(f"windowShouldClose_:\n{traceback.format_exc()}", file=sys.stderr)
        return False

    # -- save and teardown ---------------------------------------------

    @objc.python_method
    def _save(self) -> bool:
        """Single durable write. Returns success.

        Order is deliberate: back up, write, mark saved, then clear the
        preview. Clearing the preview first would drop the device back to
        the old circle for one control tick before the new file is read —
        a visible flicker at the exact moment the user is watching to
        confirm their change took.
        """
        if self._session is None:
            return False
        path = paths.calibration_path()
        previous = _calibration_document()
        if previous and not self._write_backup(previous):
            # A backup that could not be written is not fatal to the
            # save, but it does silently remove "Restore previous
            # calibration" escape hatch, so it is said out loud rather
            # than logged.
            self._alert(
                "Couldn't save a backup of the current circle.",
                "Saving anyway. 'Restore previous calibration' will not "
                "bring these numbers back.",
            )
        document = cs.calibration_document(
            self._session.values,
            safety_margin_pct=self._session.safety_margin_pct,
            bounds=self._session.bounds,
            previous=previous,
        )
        try:
            paths.ensure_dir(path.parent)
            atomic_write_json(path, document)
        except OSError:
            print(f"calibrate: save failed:\n{traceback.format_exc()}", file=sys.stderr)
            self._alert(
                "Couldn't save the circle.",
                f"Nothing was changed. Could not write {path}.",
            )
            return False
        self._session.mark_saved()
        self._sync_from_session()
        print(f"calibrate: saved {path}.", file=sys.stderr)
        return True

    @objc.python_method
    def _write_backup(self, previous: dict) -> bool:
        try:
            atomic_write_json(self._backup_path(), previous)
        except OSError:
            print(
                f"calibrate: could not write backup:\n{traceback.format_exc()}",
                file=sys.stderr,
            )
            return False
        return True

    @objc.python_method
    def _finish(self) -> None:
        """Tear both windows down and clear the preview.

        Idempotent via `_closing`: Esc during the close alert, or a
        second Cancel click, would otherwise remove the key monitor
        twice and order out a released window.
        """
        if self._closing:
            return
        self._closing = True
        self._write_preview(clear=True)
        if self._monitor is not None:
            AppKit.NSEvent.removeMonitor_(self._monitor)
            self._monitor = None
        for window in (self._overlay, self._window):
            if window is not None:
                window.orderOut_(None)
        self._overlay = None
        self._window = None
        self._session = None
        if self._on_close is not None:
            callback, self._on_close = self._on_close, None
            callback()

    # -- helpers -------------------------------------------------------

    @objc.python_method
    def _confirm_close(self) -> str:
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_("Save the circle you adjusted?")
        alert.setInformativeText_(
            "The View has gone back to the circle it had before. Saving "
            "keeps your new one."
        )
        alert.addButtonWithTitle_("Save")
        alert.addButtonWithTitle_("Discard")
        alert.addButtonWithTitle_("Cancel")
        AppKit.NSApp().activateIgnoringOtherApps_(True)
        response = alert.runModal()
        if response == AppKit.NSAlertFirstButtonReturn:
            return "save"
        if response == AppKit.NSAlertSecondButtonReturn:
            return "discard"
        return "cancel"

    @objc.python_method
    def _alert(self, message: str, informative: str) -> None:
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_(message)
        alert.setInformativeText_(informative)
        alert.addButtonWithTitle_("OK")
        AppKit.NSApp().activateIgnoringOtherApps_(True)
        alert.runModal()


def _parse_number(text: str) -> object:
    """A text field's string as a number, or the string itself.

    Returning the unparseable input unchanged rather than a sentinel lets
    `calibrate_state.set_field` apply its own "not a number means leave
    it alone" rule, so there is exactly one place that decides what a
    half-typed value means.
    """
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return text


def main() -> int:
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
    controller = CalibrateController.alloc().init()
    if not controller.start(on_close=lambda: AppKit.NSApp().terminate_(None)):
        return 1
    app.setDelegate_(controller)
    print("calibrate: running.", file=sys.stderr)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
