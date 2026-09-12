"""The window a losing launch puts on screen — and, since Phase 5, the
window this app shows instead of an `NSAlert`.

`ui/contention_state.py` decided what to say and `ui/notice_state.py`
decided where to put it. This is the shell that draws it, and **it
decides nothing** — no verdict, no threshold, not a word of the copy. The
seam is deliberate: everything that can be wrong in an interesting way
lives in a module a headless test can reach, and what is left here is the
part no test on this machine can check anyway.

🔴 **TWO ENTRY POINTS, AND THE DIFFERENCE BETWEEN THEM IS WHO OWNS THE
RUN LOOP.** Getting this wrong does not misplace a window; it quits the
app.

* **`show()`** is the contended-launch path. It runs in a process that
  has *no* run loop of its own — it lost `ui.lock` and exists only to
  say so — so it starts one with `app.run()` and dismissal must
  `stop_()` it, or the process sits there after its window is gone.
* **`advise()`** is Phase 5's. It runs **inside the live menu bar**,
  whose `app.run()` is already spinning and is the whole application. A
  dismissal that called `stop_()` here would return from
  `menubar.main()`'s `app.run()` and **exit the app** — closing the
  About box would quit ImageView and take the status item with it.

So the stop is conditional on `stops_app`, set at construction by
whichever entry point built the controller, and never inferred.

**Phase 5 sends two more callers here**, and both are sites where a
modal had **no window of this app's on screen to open in front of** —
the shape of 2026-09-08, where an alert that opened behind everything
was invisible, unanswerable, and holding the run loop forever:

* `menubar._advise` — the About box and both `startDisplay_` failures.
* `calibrate_window._advise` — *the View isn't connected*, which fires
  inside `start()` before either of that window's windows is built.

Every one of them was a `runModal()` that froze `ui_heartbeat_at` for as
long as it was up. MEASURED at **51.2 seconds** for the About box on
2026-09-10.

**A third site was converted and does not come here.**
`first_run_window._advise` is shown by a window that *is* on screen, so
it became a **sheet** — window-modal, unlosable behind anything, and no
nested run loop. A floating panel there would have been centred on top
of the centred first-run window. The alerts owned by `settings_window`,
and the three remaining in `calibrate_window`, were deliberately left
modal; see the plan's Phase 5 section and `calibrate_window._confirm_close`,
which is the one alert in this app whose answer something depends on.

(Not `display/notice.py`, which is a different thing with a similar name:
that one is what the *picture display* draws on the glass when it has
nothing good to show. This one is a window on the desk, shown by a menu
bar that could not start.)

🔴 **Three calls this file must never make, and the reason is the same
for all three: the process arriving to diagnose a wedge must not make the
call it is diagnosing.**

* **`runModal()`** — a modal run loop is the mechanism suspected in the
  original wedge. It stops servicing default-mode timers, so a Quit Apple
  Event is never fetched, and an alert that opens behind everything is
  invisible, unanswerable and permanent. A second copy that hung the same
  way while explaining the first one would be the whole bug again.
* **`activateIgnoringOtherApps_`** — Apple (DevForums 807805 /
  FB21087054) reports activation failing intermittently on macOS 26, and
  the app this notice is about runs on macOS 26.
  `orderFrontRegardless()` is the documented way for an accessory app to
  show a window without asking to be activated at all.
* **`statusItemWithLength_`** — the named blocking call in the "it
  started but never reached the menu bar" hypothesis. This process holds
  no lock, so if it wedges it wedges alone; it must not wedge in the one
  place it is trying to describe.

`ui/test_notice_window.py` asserts all three are absent from this file's
source. That is a pattern check, and a pattern check usually stands in
for a behaviour instead of measuring it — but the claim here is *about
the source*: the absence of a call is the property, so reading the text
is measuring the thing rather than standing in for it.

🔴 **`setHidesOnDeactivate_(False)` is the single most important line in
this file.** MEASURED on this machine, 2026-09-10: an `NSPanel`'s default
is **True**. So the default panel would vanish the instant the user
clicked another application — and this window's whole job, in its worst
case, is to say *"force quit pid 41234, and leave the other ImageView
alone"*. The user clicks Activity Monitor; the window disappears, taking
the pid with it; they are back exactly where 2026-09-08 left them, with
an app that will not start and nothing to read. Deleting that line looks
like tidying up a default. It is not.

**Exit 0, always.** Everything here is wrapped, `show()` returns a bool
rather than raising, and its caller returns 0 regardless —
`KeepAlive {SuccessfulExit: false}` respawns exactly the non-zero exits a
traceback would produce.
"""

from __future__ import annotations

import sys
import traceback

import AppKit
import objc
from Foundation import NSMakeRect

from ui import contention_state as cstate
from ui import menubar_state as ms
from ui import notice_state as ns

#: Inset from the panel's left and right edges to the text column.
MARGIN_X = 20.0
#: Above the headline, and below the button row.
MARGIN_TOP = 18.0
MARGIN_BOTTOM = 16.0
#: Between headline and body, and between body and the button row.
HEADLINE_GAP = 8.0
BUTTON_GAP = 16.0
#: Between two buttons.
BUTTON_SPACING = 10.0

#: The app's existing `NSAlert` register: a bold 13pt message text over
#: an 11pt informative text. Matched rather than re-chosen so that a
#: notice and an alert from the same app do not look like two apps.
HEADLINE_POINT_SIZE = 13.0
BODY_POINT_SIZE = 11.0

#: A generous height to measure wrapped text against. Not a limit on the
#: panel: the measured height is what the panel gets.
MEASURE_HEIGHT = 10_000.0

KEY_CODE_ESCAPE = 53

#: 🔴 **Every controller with a panel on screen, held so that nothing is
#: collected out from under its own key monitor.** `menubar.py:145-156`
#: records this exact defect class for `CalibrateController`: "nothing
#: else retains it — a controller collected out from under its own key
#: monitor takes both its windows with it". An `NSWindow` does **not**
#: retain its delegate, and `setReleasedWhenClosed_(False)` keeps the
#: panel alive, not the object that owns its buttons.
#:
#: 🔵 **`menubar.py` used to hold an ivar beside this, and no longer
#: does.** That ivar was the anti-stacking policy, not the lifetime; it
#: moved to `_SLOTS` below when `calibrate_window` needed the same policy
#: and could not hold anything at all. This list is the **lifetime**, and
#: it is what the slot table rests on — a slot entry is a second
#: reference to a controller this list is already keeping alive.
#:
#: A list rather than a set, compared by identity: PyObjC hands back
#: fresh proxies for the same underlying object often enough that this
#: codebase has already lost a feature to an identity comparison in
#: `calibrate_window`, and a `set` would additionally rest on `__hash__`
#: and `isEqual:` behaving. `_finish` is the only remover.
_ON_SCREEN: list = []

#: 🔴 **A slot is "a place on screen that holds at most one panel", and
#: it lives here rather than on a caller because one caller cannot hold
#: state at all.**
#:
#: A modal blocked a second click; a panel does not, so every advisory
#: site can now stack identical windows. `menubar.py` first solved that
#: with an ivar on `MenuBarController` — and `calibrate_window.start()`
#: cannot: it shows its advisory, returns False, and `menubar.calibrate_`
#: then drops the controller *and clears its own re-entry guard*, so four
#: clicks with the View unplugged gave four panels, each needing its own
#: close. MEASURED.
#:
#: A second implementation of "same words, re-show; different words,
#: replace" is the shape that produces this project's confident wrong
#: answers: one question asked down two code paths, where the second one
#: has never heard of what the first knows. So there is one, here, and
#: `menubar._advise` delegates to it.
_SLOTS: dict = {}

#: A slot name no shipping caller uses, so a test can drive the rule
#: without borrowing a real site's name and leaving a panel registered
#: under it. Here rather than in the test file because the invariant it
#: protects — that it collides with nothing — is about this table.
SLOT_UNDER_TEST = "test.slot"


def on_screen() -> tuple:
    """The controllers this module is holding a panel alive for."""
    return tuple(_ON_SCREEN)


def slots() -> dict:
    """The slot table, for tests and for a caller that wants to look."""
    return dict(_SLOTS)


def _rect(frame) -> ns.Rect:
    """An AppKit rectangle as this project's plain `Rect`."""
    return ns.Rect(
        x=float(frame.origin.x),
        y=float(frame.origin.y),
        width=float(frame.size.width),
        height=float(frame.size.height),
    )


def screen_records() -> tuple[ns.Screen, ...]:
    """Every attached screen, as plain rectangles. `()` if none, or if
    AppKit could not be asked — the caller centres the window itself in
    that case rather than inventing a coordinate."""
    try:
        return tuple(
            ns.Screen(frame=_rect(screen.frame()), visible_frame=_rect(screen.visibleFrame()))
            for screen in AppKit.NSScreen.screens()
        )
    except Exception:  # noqa: BLE001 - a diagnostic must not raise
        return ()


def zero_screen_height(screens: tuple[ns.Screen, ...]) -> float:
    """The height of the screen Quartz measures from.

    The window server's origin is the top-left of the screen whose Cocoa
    frame origin is `(0, 0)`. That is usually the first screen and is not
    guaranteed to be, so it is looked up rather than assumed; with no
    screens at all the answer is 0.0, which makes every conversion
    nonsense — and `show()` never converts anything in that case, because
    an empty screen list takes the centred path.
    """
    for screen in screens:
        if screen.frame.x == 0.0 and screen.frame.y == 0.0:
            return screen.frame.height
    return screens[0].frame.height if screens else 0.0


class NoticeController(AppKit.NSObject):
    """Owns one notice panel. `alloc().init()` then `build()`, matching
    every other controller in this UI."""

    def init(self):
        self = objc.super(NoticeController, self).init()
        if self is None:
            return None
        self._panel = None
        self._message = None
        self._details = ""
        self._copy_button = None
        self._timer = None
        self._monitor = None
        self._finished = False
        self._on_dismiss = None
        # The slot this panel occupies, or None. Cleared in `_finish`
        # together with the module-level hold, so that a dismissed panel
        # cannot be re-shown from a table entry pointing at a window that
        # is gone.
        self._slot = None
        # 🔴 Whether dismissing this window must stop the application's
        # run loop. False is the safe default — a controller that never
        # learns the answer leaves a process running one moment too long,
        # where the other way round **quits the menu bar when the user
        # closes the About box**. Both entry points set it explicitly all
        # the same; see this module's docstring.
        self._stops_app = False
        return self

    # -- construction-time configuration -------------------------------

    @objc.python_method
    def set_stops_app(self, stops: bool) -> None:
        """Say whether `_finish` may stop the application's run loop."""
        self._stops_app = bool(stops)

    @objc.python_method
    def stops_app(self) -> bool:
        return self._stops_app

    @objc.python_method
    def set_on_dismiss(self, callback) -> None:
        """A zero-argument callable run after the window goes away.

        How a caller holding an ivar learns to clear it — the same shape
        as `settings_window`'s and `calibrate_window`'s `on_close`, so
        the menu bar's "is one already open" test cannot go stale.
        """
        self._on_dismiss = callback

    @objc.python_method
    def message(self) -> ns.Message | None:
        """What this panel is showing, so a caller can tell whether a
        panel it is already holding still says the right thing."""
        return self._message

    @objc.python_method
    def says(self, headline: str, body: str) -> bool:
        """Whether this panel already shows exactly these words.

        🔴 **The words, not merely "a panel exists".** `startDisplay_`
        has two different failures — the display agent could not be
        installed, and Login Items is blocking it — and a user who hits
        the first, moves the app, and tries again must not be re-shown
        the message from last time. Re-showing a panel is only right when
        it still says the right thing; otherwise it is replaced.
        """
        message = self._message
        return (
            message is not None
            and message.headline == headline
            and message.body == body
        )

    @objc.python_method
    def set_slot(self, slot) -> None:
        """Record which slot this panel occupies, so `_finish` can free
        it. See `_SLOTS`."""
        self._slot = slot

    @objc.python_method
    def slot(self):
        return self._slot

    @objc.python_method
    def dismiss(self) -> None:
        """Close this panel as if the user had. Idempotent."""
        self._finish()

    # -- building ------------------------------------------------------

    @objc.python_method
    def _label(self, text: str, size: float, bold: bool = False, secondary: bool = False):
        """One text field.

        🔴 **`setStringValue_` and nothing else.** Never
        `NSAttributedString`'s `initWithHTML:documentAttributes:`, never a
        format string, never anything that interprets markup — the rule
        `contention_state.Notice` states and this is where it is kept.
        `headline` and `body` carry strings read off disk: the holder's
        executable path, the stacks path out of `ui_status.json`, the lock
        path off the filesystem. The HTML initializer is a full document
        parser that can fetch remote resources; it is not a text renderer
        and must not be reached for because a message would look nicer
        with a bold pid.

        **Selectable, not editable.** The pid and the paths are the whole
        point of the wedge messages, and a user who cannot select them
        must retype a path from a screen.
        """
        field = AppKit.NSTextField.alloc().init()
        field.setStringValue_(text)
        field.setBezeled_(False)
        field.setDrawsBackground_(False)
        field.setEditable_(False)
        field.setSelectable_(True)
        font = (
            AppKit.NSFont.boldSystemFontOfSize_(size)
            if bold
            else AppKit.NSFont.systemFontOfSize_(size)
        )
        field.setFont_(font)
        if secondary:
            field.setTextColor_(AppKit.NSColor.secondaryLabelColor())
        field.cell().setWraps_(True)
        return field

    @objc.python_method
    def _measured_height(self, field, width: float) -> float:
        """How tall the wrapped text actually is, asked of the cell.

        Measured rather than estimated from a line count: the bodies run
        from one sentence to five, they contain paths that do not wrap at
        spaces, and a panel sized from a guess is a panel with its last
        line clipped — in a window whose last line is regularly the
        instruction."""
        size = field.cell().cellSizeForBounds_(NSMakeRect(0.0, 0.0, width, MEASURE_HEIGHT))
        return float(size.height)

    @objc.python_method
    def _button(self, title: str, action: str):
        button = AppKit.NSButton.alloc().init()
        button.setTitle_(title)
        button.setBezelStyle_(AppKit.NSBezelStyleRounded)
        button.setTarget_(self)
        button.setAction_(action)
        button.sizeToFit()
        return button

    @objc.python_method
    def build(self, message: ns.Message, pointer: str | None = None):
        """Create the panel and lay it out. Returns it, or None.

        Takes a `notice_state.Message` rather than a
        `contention_state.Notice`: since Phase 5 there are two sources
        for this window and only one of them has a verdict. The
        translation is `notice_state.message_for`, and `Message`'s
        docstring records why inventing a ninth `Verdict` for the other
        caller would have been the worse trade.

        Does **not** show it and does not touch the run loop, so a test
        can build one and read its properties back without anything
        appearing on the owner's screen.

        `pointer` is `notice_state.pointer_sentence`'s answer, and it goes
        in a **field of its own** rather than being appended to `body`.
        Two reasons, and the first is the load-bearing one: 4a's words
        then reach the screen byte-identical to the words 4a tests, so the
        tested copy and the shown copy cannot drift. The second is that a
        sentence this layer wrote should not look like one the classifier
        wrote — it is set in the secondary label colour for the same
        reason.
        """
        self._message = message
        self._details = message.details

        text_width = ns.PANEL_WIDTH - 2.0 * MARGIN_X
        headline = self._label(message.headline, HEADLINE_POINT_SIZE, bold=True)
        body = self._label(message.body, BODY_POINT_SIZE)
        headline_height = self._measured_height(headline, text_width)
        body_height = self._measured_height(body, text_width)

        hint = None
        hint_height = 0.0
        if pointer:
            hint = self._label(pointer, BODY_POINT_SIZE, secondary=True)
            hint_height = self._measured_height(hint, text_width) + HEADLINE_GAP

        buttons = []
        for title in message.buttons:
            action = "copyDetails:" if title == ns.COPY_BUTTON else "closeNotice:"
            button = self._button(title, action)
            if title == ns.COPY_BUTTON:
                self._copy_button = button
            buttons.append(button)
        button_height = max((float(b.frame().size.height) for b in buttons), default=0.0)

        total = (
            MARGIN_TOP
            + headline_height
            + HEADLINE_GAP
            + body_height
            + hint_height
            + BUTTON_GAP
            + button_height
            + MARGIN_BOTTOM
        )

        panel = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0.0, 0.0, ns.PANEL_WIDTH, total),
            AppKit.NSWindowStyleMaskTitled
            | AppKit.NSWindowStyleMaskClosable
            | AppKit.NSWindowStyleMaskUtilityWindow,
            AppKit.NSBackingStoreBuffered,
            False,
        )
        panel.setTitle_(ms.APP_NAME)
        panel.setReleasedWhenClosed_(False)
        panel.setDelegate_(self)

        # 🔴 THE LINE. An NSPanel's measured default is True, which would
        # hide this window the moment the user clicked the very app it
        # told them to open. See this module's docstring.
        panel.setHidesOnDeactivate_(False)

        # Reading a notice must not steal focus from whatever the user was
        # doing; nothing here needs key input, and the Close button works
        # on a window that never becomes key.
        panel.setBecomesKeyOnlyIfNeeded_(True)

        # Deliberately no `setLevel_`. An NSPanel's default is
        # NSFloatingWindowLevel (measured: 3), which is above ordinary
        # windows and below everything that matters. 1001 belongs to the
        # calibration overlay; a diagnostic notice has no business over a
        # screen saver or a full-screen picture.

        content = panel.contentView()

        y = MARGIN_BOTTOM
        x = ns.PANEL_WIDTH - MARGIN_X
        for button in reversed(buttons):
            size = button.frame().size
            x -= float(size.width)
            button.setFrame_(NSMakeRect(x, y, float(size.width), float(size.height)))
            content.addSubview_(button)
            x -= BUTTON_SPACING

        y += button_height + BUTTON_GAP
        if hint is not None:
            hint.setFrame_(NSMakeRect(MARGIN_X, y, text_width, hint_height - HEADLINE_GAP))
            content.addSubview_(hint)
            y += hint_height

        body.setFrame_(NSMakeRect(MARGIN_X, y, text_width, body_height))
        content.addSubview_(body)

        y += body_height + HEADLINE_GAP
        headline.setFrame_(NSMakeRect(MARGIN_X, y, text_width, headline_height))
        content.addSubview_(headline)

        self._panel = panel
        return panel

    # -- placing -------------------------------------------------------

    @objc.python_method
    def place(self, icons=(), avoid=()) -> ns.Placement | None:
        """Put the panel under the holder's icon, on a screen a person is
        looking at. Returns the placement used, or None when it fell back
        to AppKit's own centring.

        `icons` and `avoid` are 4a's `WindowFact`s — the holder's
        **visible** status items and the picture display's windows. Both
        arrive in window-server coordinates and are converted here,
        because this is the only place that knows which screen Quartz
        measures from.
        """
        if self._panel is None:
            return None
        try:
            screens = screen_records()
            if not screens:
                self._panel.center()
                return None
            height = zero_screen_height(screens)
            size = (
                float(self._panel.frame().size.width),
                float(self._panel.frame().size.height),
            )
            placement = ns.placement_for(
                [ns.rect_of(icon, height) for icon in icons],
                [ns.rect_of(window, height) for window in avoid],
                screens,
                size,
            )
            if placement is None:
                self._panel.center()
                return None
            self._panel.setFrameOrigin_(AppKit.NSPoint(placement.x, placement.y))
            return placement
        except Exception:  # noqa: BLE001 - a misplaced window beats no window
            print(f"notice: could not place the panel:\n{traceback.format_exc()}", file=sys.stderr)
            try:
                self._panel.center()
            except Exception:  # noqa: BLE001 - nothing left to try
                pass
            return None

    @objc.python_method
    def center(self) -> None:
        """AppKit's own centring, and the whole of an advisory's placement.

        🔴 **Deliberately not `place()`.** `place()` exists to put the
        contended-launch notice under the *holder's* menu bar icon, and
        an advisory has no such icon to point at: it is shown by the very
        process it is about, which is running and whose icon is wherever
        the user can already see it. Routing an advisory through
        `place()` would take the no-icon fallback — top-centre of screen
        0 — and screen 0 is not necessarily a screen anybody is looking
        at. On this machine one of the attached screens is a 2.1-inch
        round panel showing pictures.

        `center()` is where the `NSAlert` this replaced put itself, so
        the conversion moves the window's *modality* and not its
        position. It also means no `Placement` exists, so
        `pointer_sentence` is never consulted and the spatial claim is
        structurally unavailable rather than conditionally withheld.
        """
        if self._panel is None:
            return
        try:
            self._panel.center()
        except Exception:  # noqa: BLE001 - a misplaced window beats no window
            print(f"notice: could not centre the panel:\n{traceback.format_exc()}", file=sys.stderr)

    # -- showing -------------------------------------------------------

    @objc.python_method
    def bring_forward(self) -> None:
        """Re-show a panel that is already up, without building another.

        `orderFrontRegardless()`, the same call `present()` makes and for
        the same reason — this app may not ask to be activated. No
        `makeKey`: an advisory that stole focus from the window the user
        was typing in would be worse than the modal it replaced.
        """
        if self._panel is None:
            return
        try:
            self._panel.orderFrontRegardless()
        except Exception:  # noqa: BLE001 - a diagnostic must not raise
            print(f"notice: could not re-show the panel:\n{traceback.format_exc()}", file=sys.stderr)

    @objc.python_method
    def present(self) -> None:
        """Order the panel front, arm the dismissal paths, and return.

        `orderFrontRegardless()` rather than `makeKeyAndOrderFront_` plus
        an activation: see the module docstring for why this process may
        not ask to be activated.
        """
        if self._panel is None or self._message is None:
            return
        self._install_key_monitor()
        self._panel.orderFrontRegardless()
        seconds = self._message.dismiss_after_s
        if seconds is not None:
            self._timer = (
                AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                    seconds, self, "autoDismiss:", None, False
                )
            )

    @objc.python_method
    def _install_key_monitor(self) -> None:
        """Esc and Cmd-W, while this panel is key.

        A local monitor, the same mechanism `calibrate_window` uses, and
        for a reason particular to this window: an accessory app has no
        menu bar menus, so there is no Close item for Cmd-W to reach and
        no responder chain handling Esc. Both are conveniences —
        `becomesKeyOnlyIfNeeded` means the panel may never become key at
        all — and the **Close button is the guaranteed path**.
        """

        def handler(event):
            try:
                return self._handle_key(event)
            except Exception:  # noqa: BLE001 - an AppKit callback
                print(f"notice: key handler:\n{traceback.format_exc()}", file=sys.stderr)
                return event

        try:
            self._monitor = AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
                AppKit.NSEventMaskKeyDown, handler
            )
        except Exception:  # noqa: BLE001 - the Close button still works
            self._monitor = None

    @objc.python_method
    def _handle_key(self, event):
        """`None` swallows the event; returning it passes it on.

        `isKeyWindow()` rather than comparing against `NSApp().keyWindow()`
        — PyObjC hands back a fresh proxy for the same underlying window,
        so an identity comparison is False even when it is the same
        window. That cost this project a whole feature in
        `calibrate_window` once.
        """
        if self._panel is None or not self._panel.isKeyWindow():
            return event
        if event.keyCode() == KEY_CODE_ESCAPE:
            self._finish()
            return None
        flags = event.modifierFlags()
        if flags & AppKit.NSEventModifierFlagCommand:
            if (event.charactersIgnoringModifiers() or "").lower() == "w":
                self._finish()
                return None
        return event

    # -- dismissal -----------------------------------------------------

    def closeNotice_(self, sender) -> None:
        self._finish()

    def autoDismiss_(self, timer) -> None:
        self._finish()

    def windowWillClose_(self, notification) -> None:
        """The title bar's close button arrives here and nowhere else."""
        self._finish()

    def copyDetails_(self, sender) -> None:
        """Put the evidence on the clipboard.

        The only thing this window does to the machine, and it is a write
        to the pasteboard. It does not quit, kill, take over or restart
        anything — an ordinary modal dialog freezes the holder's heartbeat
        exactly the way a wedge does (measured: an open About box, 51.2
        seconds), so every verdict here is a statement plus an
        instruction, never an action.
        """
        try:
            board = AppKit.NSPasteboard.generalPasteboard()
            board.clearContents()
            board.setString_forType_(self._details, AppKit.NSPasteboardTypeString)
            if self._copy_button is not None:
                self._copy_button.setTitle_("Copied")
        except Exception:  # noqa: BLE001 - a failed copy must not take the window down
            print(f"notice: could not copy details:\n{traceback.format_exc()}", file=sys.stderr)

    @objc.python_method
    def _finish(self) -> None:
        """Tear down once, release the hold, and — only for `show()` —
        let its run loop return.

        🔴 **THE `stops_app` TEST IS THE DIFFERENCE BETWEEN CLOSING A
        WINDOW AND QUITTING THE APPLICATION.** Until Phase 5 this method
        stopped the run loop unconditionally, which was right when the
        only caller was a process whose run loop existed solely to hold
        this window up. `menubar._advise` runs in the live menu bar,
        whose `app.run()` **is** the application: an unconditional
        `stop_()` there returns from `menubar.main()`, so clicking Close
        on the About box would quit ImageView and take the status item
        with it. Reached from `closeNotice_`, `autoDismiss_`,
        `windowWillClose_` and Esc alike, so every dismissal path is the
        same hazard.
        """
        if self._finished:
            return
        self._finished = True
        try:
            if self._timer is not None:
                self._timer.invalidate()
                self._timer = None
            if self._monitor is not None:
                AppKit.NSEvent.removeMonitor_(self._monitor)
                self._monitor = None
            if self._panel is not None:
                self._panel.setDelegate_(None)
                self._panel.orderOut_(None)
        except Exception:  # noqa: BLE001 - stopping must not raise
            print(f"notice: teardown:\n{traceback.format_exc()}", file=sys.stderr)
        _ON_SCREEN[:] = [held for held in _ON_SCREEN if held is not self]
        if self._slot is not None and _SLOTS.get(self._slot) is self:
            del _SLOTS[self._slot]
        if self._stops_app:
            self._stop_run_loop()
        callback, self._on_dismiss = self._on_dismiss, None
        if callback is not None:
            try:
                callback()
            except Exception:  # noqa: BLE001 - a caller's bookkeeping
                print(f"notice: on_dismiss:\n{traceback.format_exc()}", file=sys.stderr)

    @objc.python_method
    def _stop_run_loop(self) -> None:
        """Stop the run loop `show()` started, so its process can exit.

        `stop_()` alone is not enough and the gap is the auto-dismiss
        case: it sets a flag that `NSApplication` checks **after it
        finishes processing an event**, and a timer callback is not an
        event. Without the posted event the run loop would sit there until
        the user moved the mouse over the window — an eight-second
        self-dismissing notice that leaves its process alive until
        something happens to wake it. Posting an application-defined event
        is the documented way to wake it immediately.

        Its own method so that `_finish`'s decision to call it is one
        readable line, and so a test can replace it and watch whether it
        was reached — which is the only way to check a call that would
        otherwise stop the run loop the test itself is running under.
        """
        try:
            app = AppKit.NSApp()
            if app is not None:
                app.stop_(None)
                wake = AppKit.NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
                    AppKit.NSEventTypeApplicationDefined,
                    AppKit.NSPoint(0.0, 0.0),
                    0,
                    0.0,
                    0,
                    None,
                    0,
                    0,
                    0,
                )
                app.postEvent_atStart_(wake, True)
        except Exception:  # noqa: BLE001 - the process still exits 0
            print(f"notice: could not stop the run loop:\n{traceback.format_exc()}", file=sys.stderr)


def prepare(notice: cstate.Notice, icons=(), avoid=()):
    """Build the panel, place it, and give it a pointer if one is earned.

    Returns `(controller, placement, pointer)`, or `(None, None, None)` if
    the panel could not be built. **Shows nothing and touches no run
    loop** — which is the only reason the wiring below is testable at all.
    `show()` is not: it blocks until a human dismisses a window, so every
    line inside it is a line no test can reach. Splitting the decisions
    out here is not tidiness; a mutation run found the pointer wiring
    surviving with the whole of it inside `show()`.

    🔴 **This is the CONTENDED-LAUNCH entry point, and its controllers
    own the run loop** — `set_stops_app(True)` on both, because the only
    caller is `show()`, whose process has no other reason to be running.
    `prepare_advisory` is the other entry point and sets the opposite.
    Neither is inferred from anything.

    **Two passes, because the pointer and the size depend on each other:**
    the panel's height depends on whether the pointer is there, and the
    pointer depends on where a panel of that height landed.

    🔴 **AND THE CLAIM IS RE-DERIVED FROM THE PLACEMENT ACTUALLY USED, NOT
    FROM THE ONE IT WAS DECIDED FROM.** This function used to argue that
    the second pass was safe because `anchored` and `clamped` depend only
    on the icon, the screens and the fixed `PANEL_WIDTH` — true, and
    incomplete. It covers the second placement *succeeding*; it does not
    cover it landing somewhere else, or failing. The screens can change
    between the two calls: a display sleeps, the View's USB-C is nudged, a
    lid closes — or `place()` catches an exception and centres, which is
    its documented behaviour. MEASURED by removing a screen between the
    passes: the panel centres on the remaining screen and still says
    *"directly above this window"* about an icon that is no longer there.

    So the second placement is re-checked, and on disagreement this
    **retreats to the unclaimed panel** — the one built in pass 1, which
    says nothing spatial — and re-places it. The retreat needs no third
    check, and that is the argument rather than an oversight: a panel that
    makes no claim cannot make a claim the geometry fails to support, so
    however the screens move afterwards it is still telling the truth.
    Bounded at two builds and three placements, no loop.
    """
    message = ns.message_for(notice)
    plain = NoticeController.alloc().init()
    if plain is None or plain.build(message) is None:
        return None, None, None
    plain.set_stops_app(True)
    placement = plain.place(icons, avoid)
    pointer = ns.pointer_sentence(placement)
    if pointer is None:
        return plain, placement, None

    pointed = NoticeController.alloc().init()
    if pointed is None or pointed.build(message, pointer) is None:
        # The claim was earned and the panel that would carry it could not
        # be built. The plain one is already placed and asserts nothing.
        return plain, placement, None
    pointed.set_stops_app(True)
    final = pointed.place(icons, avoid)
    if ns.pointer_sentence(final) == pointer:
        return pointed, final, pointer
    return plain, plain.place(icons, avoid), None


def reuse_slot(slot, headline: str, body: str):
    """The panel already on screen for `slot`, re-shown — or None when a
    new one has to be built.

    🔴 **The words, not merely "a panel exists".** `startDisplay_` has
    two distinct failures, and a user who hits "move it to Applications",
    moves it, and tries again must not be re-shown last time's message.
    Same words re-shows; different words dismisses the stale one and
    answers None so the caller builds a fresh panel.

    One implementation, used by every advisory site. Two would be the
    same question asked down a second code path — which is where this
    project's confident wrong answers have come from before.
    """
    if slot is None:
        return None
    existing = _SLOTS.get(slot)
    if existing is None:
        return None
    if existing.says(headline, body):
        existing.bring_forward()
        return existing
    existing.dismiss()
    return None


def prepare_advisory(headline: str, body: str, on_dismiss=None, slot=None):
    """Build, centre, hold and log one advisory. **Shows nothing.**

    Returns the controller, or None if the panel could not be built. The
    caller calls `present()`; `advise()` below is that one line plus the
    wrapping every caller of this module needs.

    Split out of `advise()` for the reason `prepare()` was split out of
    `show()`: a mutation run found 4b's entire pointer wiring surviving
    because it lived inside a function that blocks. Nothing here blocks,
    so all of it is reachable by a test — the run-loop decision, the
    hold, the centring and the log line included.

    🔴 **`set_stops_app(False)`, and this is the line the phase turns
    on.** See the module docstring: the caller's `app.run()` is the
    application, and stopping it closes ImageView rather than this
    window.

    🔴 **NOTHING BETWEEN TAKING THE HOLD AND RETURNING MAY RAISE, AND
    THE LOG LINE COULD.** `advise()` below recovers from a failure here
    by dismissing the controller it was handed — but if this function
    raises *after* `_ON_SCREEN.append` and *before* `return`, there is no
    controller to hand back, the `except` branch has nothing to dismiss,
    and the hold is permanent. PROVEN by execution, not reasoned: with
    the log write made to raise, `advise()` correctly returned None and
    honoured its never-raises contract while leaving one controller held
    for the life of the process. A closed fd, a broken pipe or a full log
    volume is all it takes. So the write is wrapped: a diagnostic that
    cannot be written is not worth a leak, and it is certainly not worth
    an exception on the path that exists to report a failure.
    """
    message = ns.advisory(headline, body)
    controller = NoticeController.alloc().init()
    if controller is None or controller.build(message) is None:
        return None
    controller.set_stops_app(False)
    controller.set_on_dismiss(on_dismiss)
    controller.center()
    _ON_SCREEN.append(controller)
    if slot is not None:
        controller.set_slot(slot)
        _SLOTS[slot] = controller
    try:
        print(ns.advisory_log_line(message), file=sys.stderr)
    except Exception:  # noqa: BLE001 - see the paragraph above
        pass
    return controller


def advise(headline: str, body: str, on_dismiss=None, slot=None):
    """Say one thing to the user, without a modal run loop, and return.

    Phase 5's replacement for `NSAlert.runModal()` at the sites reachable
    with no window of this app's on screen. Returns the controller so a
    caller can hold it and re-show it rather than stacking a second copy,
    or None if nothing went up. **Never raises** — a message about a
    failure must not become a second failure.

    🔴 **THE CALL RETURNS IMMEDIATELY, WHICH `runModal()` DID NOT.** That
    is the whole risk of this conversion and it is the same one Phase 3
    took with the folder picker: any statement that used to run *after*
    the alert was dismissed now runs while it is still on screen. Every
    converted site was checked — in all four, the alert is the last
    statement before a `return`, and nothing reads a result — but a new
    caller must check its own.

    `slot` names a place on screen that holds at most one panel. Pass one
    from **every** site a user can reach twice; see `_SLOTS` and
    `reuse_slot`, and the four panels that four clicks produced before
    `calibrate_window` had one.
    """
    controller = None
    try:
        shown = reuse_slot(slot, headline, body)
        if shown is not None:
            return shown
        controller = prepare_advisory(headline, body, on_dismiss, slot)
        if controller is None:
            return None
        controller.present()
        return controller
    except Exception:  # noqa: BLE001 - a diagnostic must never become the fault
        try:
            print(
                f"notice: could not show an advisory:\n{traceback.format_exc()}",
                file=sys.stderr,
            )
            # `prepare_advisory` has already taken the module-level hold.
            # Without this, a panel that was built and then failed to
            # present would be held for the life of the process with
            # nothing on screen able to release it.
            if controller is not None:
                controller.dismiss()
        except Exception:  # noqa: BLE001 - nothing left to do
            pass
        return None


def show(notice: cstate.Notice, icons=(), avoid=()) -> bool:
    """Put one notice on screen and run until it is dismissed.

    Returns True when a window was shown. **Never raises**, because the
    caller is `menubar.main()`'s contended path, which must `return 0`.

    This blocks. That is the design: everything except the one benign
    "already running, here is your icon" notice carries an instruction,
    and an instruction that disappears before it is read is worse than
    silence. The process holds **no lock** while it waits — it lost the
    lock, which is why it is here — so a notice left open costs a few
    megabytes and nothing else. Two double-clicks give two notices;
    suppressing that would need a second state file with ambiguous writer
    semantics on the recovery path, which is the worse trade and is
    recorded as a known rough edge rather than papered over.
    """
    try:
        app = AppKit.NSApplication.sharedApplication()
        # No Dock icon: this process is a diagnostic that happens to have
        # a window, and it must not look like a second copy of the app
        # starting up. Setting the policy is not activating — it asks for
        # nothing from the window server and cannot fail the way
        # `activateIgnoringOtherApps_` is reported to on macOS 26.
        app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
        controller, placement, pointer = prepare(notice, icons, avoid)
        if controller is None:
            return False
        # Unconditionally, and via a pure function. The line used to live
        # here inside `if placement is not None`, so the one case where
        # nothing could be worked out logged nothing at all.
        print(ns.placement_log_line(placement, pointer), file=sys.stderr)
        controller.present()
        app.run()
        return True
    except Exception:  # noqa: BLE001 - a diagnostic must never become the fault
        try:
            print(
                f"notice: could not show the contended-launch notice:\n"
                f"{traceback.format_exc()}",
                file=sys.stderr,
            )
        except Exception:  # noqa: BLE001 - nothing left to do
            pass
        return False


__all__ = [
    "BODY_POINT_SIZE",
    "HEADLINE_POINT_SIZE",
    "MARGIN_X",
    "NoticeController",
    "advise",
    "on_screen",
    "prepare",
    "prepare_advisory",
    "reuse_slot",
    "screen_records",
    "slots",
    "show",
    "zero_screen_height",
]


def _demo(name: str = "unresponsive") -> int:
    """Show one **sample** notice, and return when it is dismissed.

    The only other way to look at this window is to launch a second
    ImageView against a running one — which writes to that copy's log
    and, on the two wedge verdicts, signals it. This reads and writes
    nothing: every value below is invented, and the pid is not a pid.

    The *words* are not invented. They come from `contention_state`, the
    same call the real path makes, so what appears on screen is the real
    copy for that verdict rather than a mock-up of it.

        display/.venv/bin/python3 -m ui.notice_window
        display/.venv/bin/python3 -m ui.notice_window serving_visible
    """
    import time

    try:
        verdict = cstate.Verdict(name)
    except ValueError:
        print(
            f"unknown verdict {name!r}; try one of: "
            + ", ".join(v.value for v in cstate.Verdict),
            file=sys.stderr,
        )
        return 2

    now = time.time()
    icon = cstate.WindowFact(layer=25, on_screen=True, x=1169.0, y=0.0, width=34.0, height=24.0)
    facts = cstate.Facts(
        lock_path="~/.viewlab/ui.lock",
        lock_pid=41234,
        holder_exe="/Applications/ImageView.app/Contents/MacOS/ImageView",
        holder_started_at=None,
        our_exe="/Applications/ImageView.app/Contents/MacOS/ImageView",
        display_pid=41200,
        heartbeat_at=now - 37.0,
        heartbeat_present=True,
        heartbeat_pid=41234,
        stacks_armed=True,
        stacks_path="~/Library/Logs/ImageView/ui.stacks.log",
        state_dir="~/.viewlab/state",
        state_writable=verdict is not cstate.Verdict.MUTED,
        state_detail="Permission denied" if verdict is cstate.Verdict.MUTED else None,
        windows=(icon,),
        display_windows=(),
        now=now,
    )
    print(
        f"notice: SAMPLE {verdict.value}. Anchored under a pretend status item "
        f"at Quartz x=1169, so the panel's centre should be at x=1186.",
        file=sys.stderr,
    )
    show(cstate.notice(facts, verdict), (icon,), ())
    return 0


if __name__ == "__main__":
    raise SystemExit(_demo(*sys.argv[1:2]))
