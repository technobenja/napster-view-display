"""Full event loop.

Assembles every earlier module into the actual running app: polls Image
Studio on a timer, syncs the local cache, walks the rotation, and
crossfades the View to whatever's current. Every external failure
degrades toward "keep showing the last good frame" — never a
crash, never a blank/black screen.

Run interactively for Step 6's soak test:

    ./.venv/bin/python3 app.py

Not yet a LaunchAgent — that's Step 7's separate approval gate. Run only from a Terminal at the mini's own physical console, or
Screen Sharing with control.
"""

from __future__ import annotations

import json
import sys
import time
import traceback

import AppKit
import objc

from display import control
from display import paths
from display import single_instance
from display.atomic_io import atomic_write_json
from display.cache import ImageCache
from display.calibration import (
    Calibration,
    apply_preview,
    calibration_watcher,
    load_calibration_resolved,
)
from display.config_store import ConfigSource
from display.display_target import get_view_screen
from display.image_store import DirectStore
from display.log_rotation import rotate_if_oversized
from display.rotation import Rotation
from display.sources.factory import build_source
from display.settings import Settings, load_settings_resolved, settings_watcher
from display.smoke_test import install_signal_handlers
from display.window import build_window

# All writable paths now come from paths.py rather than
# Path(__file__).parent — inside a py2app bundle the latter is read-only,
# and writing there invalidates the code signature.
STATE_DIR = paths.state_dir()
STATUS_PATH = paths.status_path()

# These are checked and rotated (log_rotation.py) before this process
# does any real logging of its own. Rotating them is a no-op when run
# interactively (nothing here yet, since launchd isn't redirecting
# stdout/stderr to these paths in that case) - safe to call
# unconditionally either way.
#
# ** KNOWN MISMATCH, deliberately not fixed in Step -1. ** These must
# match the LaunchAgent plist's StandardOutPath/StandardErrorPath, and as
# of Step -1 they no longer do: the installed plist still redirects to
# <repo>/display/logs/, so rotation currently runs against files launchd
# is not writing. Left alone on purpose, because launchd does NOT create
# intermediate directories for those keys - a plist pointing at
# ~/Library/Logs/ImageView/ before that directory exists makes the job
# fail to spawn at all, and paths.ensure_all() below runs far too late to
# help. The installer generates the plists at install time (Step 5b), which is
# where directory creation and this path belong together. Until then the
# only cost is that log rotation is inert for the launchd-managed run.
LOG_DIR = paths.log_dir()
STDOUT_LOG_PATH = paths.stdout_log_path()
STDERR_LOG_PATH = paths.stderr_log_path()

SCREEN_RETRY_INTERVAL_S = 10.0

# A frequent no-op timer exists purely so the Cocoa run loop yields back
# to the Python interpreter often enough for signal.signal()'s SIGINT/
# SIGTERM handlers to actually fire. Confirmed necessary the hard way:
# blocked inside app.run(), Python's signal handling only runs when
# control returns to bytecode dispatch, and this app's real timers
# (poll: 1800s, rotation: 900s) are nowhere near frequent enough for
# that — without this, Ctrl+C/SIGTERM would hang indefinitely, exactly
# as observed with every earlier display/*.py script that lacked it.
# Measured latency with this in place: ~0.1s.
#
# ** DO NOT ADD WORK TO THIS TIMER. ** Its body is `lambda timer: None`
# and cannot fail, which is the whole point — every other timer selector
# in this file carries a broad `except` because an exception off a timer
# kills the run loop, and this one has no such wrapper. We
# considered and explicitly REVERTED the "reuse the heartbeat at zero
# added cost" idea: the failure mode of a raising heartbeat is
# specifically *losing the ability to stop the service* (`launchctl
# kickstart` hangs indefinitely, confirmed the hard way in Phase 2).
# Config polling goes on CONTROL_INTERVAL_S's separate timer below.
SIGNAL_RESPONSIVENESS_INTERVAL_S = 0.25

# The control timer: a *separate* 0.25s timer with the standard
# broad-except body, carrying the config hot-reload check and, from
# Step 1, the command-file poll. Cost over reusing the heartbeat: one
# timer object. Benefit: the heartbeat stays incapable of failing.
CONTROL_INTERVAL_S = 0.25

# status.json carries `heartbeat_at` so the UI can tell a
# running display from a dead one, and sets the staleness threshold at
# **5 seconds**. Writing it "on each control tick", which at
# CONTROL_INTERVAL_S would be four atomic rewrites of status.json every
# second — ~345,000 write-and-rename pairs a day, forever, on a machine
# that is meant to sit quietly in someone's home for years. Writing it
# every 2s instead satisfies the same 5s threshold with 2.5x margin
# (two heartbeats must be missed before the UI calls it dead) at 1/8th
# the disk churn. Deliberate deviation from the letter of the heartbeat cadence; flagged
# in the Step 1 report.
HEARTBEAT_INTERVAL_S = 2.0

# blank/restore animation length. Matched to the crossfade the
# device was confirmed against in Phase 2 rather than made configurable:
# The settings UI explicitly cut the crossfade-duration slider ("2.0s was confirmed
# on the physical device; a slider invites unconfirming it"), and a
# blank that runs at a different speed from every other transition on
# the same display would read as a glitch.
BLANK_FADE_DURATION_S = 2.0


class _ClearSentinel:
    """Marker meaning "set this status field back to null".

    `merge_status` treats a plain `None` as "no opinion, leave whatever
    is there" so that a partial update never blanks a field it does not
    know about. `last_error` needs the opposite — it is specified
    as "cleared on success" — and without a sentinel the first error of
    the day would stay in the settings window until the next restart,
    describing a problem that fixed itself an hour ago."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "CLEAR"


CLEAR = _ClearSentinel()


def merge_status(**fields) -> None:
    """Merge `fields` into status.json, atomically ("a small local
    status file ... the owner can check if they think to").

    Module-level rather than a Coordinator method because two callers
    outside any Coordinator need it: main() records which config source
    resolution order settled on (the plan requires a silent
    fallback be observable) before a Coordinator exists, and the
    Bootstrapper records hot-reloads while the View may still be absent.

    Never raises — a status file that cannot be written must not affect
    what is on the display."""
    existing: dict[str, object] = {}
    try:
        existing = json.loads(STATUS_PATH.read_text())
    except (OSError, ValueError):
        pass
    if not isinstance(existing, dict):
        existing = {}
    existing["updated_at"] = time.time()
    for key, value in fields.items():
        if isinstance(value, _ClearSentinel):
            existing[key] = None
        elif value is not None or key not in existing:
            existing[key] = value
    try:
        atomic_write_json(STATUS_PATH, existing)
    except (OSError, TypeError, ValueError) as exc:
        # TypeError/ValueError, not just OSError: `json.dump` raises
        # TypeError on a value it cannot serialize, and this function's
        # contract — "never raises; a status file that cannot be written
        # must not affect what is on the display" — was only true for
        # disk failures before Step 1. It matters more now that the status
        # file added eight fields, several of them computed: `_show_image`
        # calls this, so an unserializable status value used to escape
        # into pollImageServer_'s broad except and discard an otherwise
        # entirely successful poll.
        print(f"merge_status: could not write {STATUS_PATH} ({exc}).", file=sys.stderr)


class Coordinator(AppKit.NSObject):
    """Owns every timer and every piece of running state once the View's
    screen has been resolved. One Coordinator per successful screen
    resolution — Bootstrapper below owns the "screen not found yet"
    startup phase."""

    def initWithScreen_calibration_settings_(self, screen, calibration, settings):
        self = objc.super(Coordinator, self).init()
        if self is None:
            return None
        self._calibration = calibration
        self._settings = settings
        # Where pictures come from is now the source's business,
        # not the Coordinator's. `caches` decides whether bytes are copied
        # into `~/Library/Caches/<bundle-id>/` at all — a local folder is
        # already on this disk, so DirectStore serves its paths straight
        # through while presenting the same four methods ImageCache does.
        self._source = build_source(settings.source)
        if self._source.caches:
            self._cache = ImageCache.for_source(
                self._source, max_size=settings.cache_max
            )
        else:
            self._cache = DirectStore()
        # Fix 2/5: tracks whether a real (non-fallback) image has ever
        # been shown on the view. Fix 2 uses it so a rotation tick that
        # finds an empty pool never un-shows a good frame in favor of the
        # empty-fill fallback. Fix 5 uses it so the very first successful
        # poll after a cold start (nothing cached yet) refreshes the
        # display immediately instead of waiting for the next scheduled
        # rotation tick, possibly up to rotation_interval_s away.
        self._has_shown_real_image = False
        self._rotation = Rotation(
            self._cache.known_ids(),
            is_valid=self._is_image_valid,
            shuffle=settings.shuffle,
        )
        self._window = build_window(screen, calibration)
        # build_window() only constructs the window - it doesn't put it on
        # screen. window.py's standalone script calls orderFrontRegardless()
        # itself after build_window() returns; Coordinator must do the same
        # or the window server never composites it (confirmed the hard way:
        # correct level/frame/alpha in the window list, but no
        # kCGWindowIsOnscreen key at all - just the desktop wallpaper showing
        # through underneath).
        self._window.orderFrontRegardless()
        self._started_at = time.time()
        self._poll_timer = None
        self._rotation_timer = None
        # Id -> human-readable label, refreshed from each poll's
        # records. Held here rather than in the cache manifest because it
        # is display metadata, not something worth persisting: a label
        # nobody can see because the app is not running has no value, and
        # the next poll rebuilds it anyway.
        self._labels: dict[str, str] = {}
        # The desired state this Coordinator has actually applied.
        # Deliberately separate from control.ControlChannel's `state`,
        # which is a reading of the UI's file: the two differ for exactly
        # one tick after an action, and after a Next-while-blanked they
        # differ until the UI writes again.
        self._blanked = False
        self._paused = False
        self._applied_paused_on_id: str | None = None
        self._preview_calibration: dict[str, float] | None = None
        # The window was just ordered front successfully above, so the
        # View's screen is present as of construction time.
        self._screen_present = True
        AppKit.NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            self,
            "screenParametersChanged:",
            AppKit.NSApplicationDidChangeScreenParametersNotification,
            None,
        )
        return self

    def screenParametersChanged_(self, notification) -> None:
        """Handles the View's screen disappearing/reappearing after the
        Coordinator is already running — Bootstrapper only covers the
        pre-Coordinator "not found yet" phase (see its docstring); this
        is the post-startup counterpart.

        AppKit's default behavior when a window's screen is removed is to
        relocate the window onto a remaining screen — for this app's
        borderless, NSScreenSaverWindowLevel, always-on-top overlay, that
        would mean it silently jumps onto the user's primary monitor.
        orderOut_() below is what prevents that: an ordered-out window
        isn't on any screen, so there's nothing for AppKit to relocate.

        Because the last frame stays frozen on the View until replugged, the
        rotation/poll timers and the window's actual drawn content are
        left completely alone while the screen is absent — only
        visibility changes. This matches cache.py's "never lose state,
        just don't show it" handling of missing files.

        Wrapped in a broad except for the same NSTimer/notification-
        selector-safety reason as pollImageServer_/advanceRotation_/
        Bootstrapper.tryResolve_ above: this runs off a system
        notification callback, so nothing in its call graph should ever
        be able to propagate an exception out and kill the run loop."""
        try:
            screen = get_view_screen(self._calibration)
            if screen is None and self._screen_present:
                print(
                    "screenParametersChanged_: View screen disappeared; "
                    "hiding window (last frame stays frozen until "
                    "replugged; timers keep running in the background).",
                    file=sys.stderr,
                )
                self._window.orderOut_(None)
                self._screen_present = False
            elif screen is not None and not self._screen_present:
                print(
                    f"screenParametersChanged_: View screen reappeared: "
                    f"{screen.frame()}. Restoring window.",
                    file=sys.stderr,
                )
                self._window.setFrame_display_(screen.frame(), True)
                self._window.orderFrontRegardless()
                self._screen_present = True
            # else: no change in the View's presence - e.g. a
            # notification fired because the *other* monitor's
            # resolution/arrangement changed. Nothing to do.
        except Exception:
            print(
                f"screenParametersChanged_: unexpected exception, "
                f"ignoring:\n{traceback.format_exc()}",
                file=sys.stderr,
            )

    @objc.python_method
    def _is_image_valid(self, image_id: str) -> bool:
        """Fix 4: wired into Rotation construction/sync_pool as the
        `is_valid` predicate the crossfade path already designed for — a cached
        entry that exists in the manifest but whose file is missing or
        fails to decode (corrupt/truncated download) never becomes a
        display candidate, instead of being selected every cycle forever.
        Decorated @objc.python_method per this project's established
        pattern for private helpers on an NSObject subclass (a prior bug
        here was a private underscore-prefixed method colliding with an
        internal AppKit selector)."""
        path = self._cache.get_path(image_id)
        if path is None:
            return False
        return AppKit.NSImage.alloc().initWithContentsOfFile_(str(path)) is not None

    @objc.python_method
    def start(self) -> None:
        # Show whatever's already current (persisted rotation state over
        # whatever's already cached) immediately, no fade — a fade from
        # nothing to the first frame would just be a fade-in from the
        # empty-pool fill, not meaningfully different from a hard cut.
        self._show_current(fade=False)
        self.pollImageServer_(None)  # immediate first poll, don't wait 30 min

        # A folder's natural cadence is ten seconds, not the
        # configured thirty minutes — half an hour between dropping a
        # photo into the folder and seeing it appear reads as broken. The
        # source's declared interval wins whenever it is the shorter of
        # the two, so the setting still bounds the HTTP sources.
        poll_interval_s = min(
            self._settings.poll_interval_s, self._source.poll_interval_s
        )
        self._poll_timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            poll_interval_s, self, "pollImageServer:", None, True
        )
        self._rotation_timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            self._settings.rotation_interval_s, self, "advanceRotation:", None, True
        )
        print(
            f"Coordinator started: source={self._source.kind!r}, "
            f"poll every {poll_interval_s}s, "
            f"rotate every {self._settings.rotation_interval_s}s, "
            f"fade {self._settings.fade_duration_s}s.",
            file=sys.stderr,
        )

    def pollImageServer_(self, timer) -> None:
        """An empty list from list_images() covers both "network/
        parse failure" and "genuinely zero images match the pool filter"
        — this project doesn't distinguish them (image_pool.py's tested,
        shipped API returns [] for both). Treating any empty result as
        "no update this poll" matches every row of failure table
        ("no visible change") and avoids cache.py's grace-period pruning
        ever running against a false-empty poll, which would otherwise
        evict the entire cache during a transient Image Server outage.

        Fix 3: the whole body is wrapped in a broad except — this method
        runs off an NSTimer selector, so any unexpected exception from the
        cache/rotation/network/disk call graph must never propagate out
        and take down the run loop."""
        try:
            records = self._source.list_images()
            if records:
                # This is bounded to MAX_DOWNLOADS_PER_SYNC downloads;
                # the remainder arrive over the next few polls.
                self._cache.sync(records, self._source)
                self._rotation.sync_pool(
                    self._cache.known_ids(), is_valid=self._is_image_valid
                )
                self._remember_labels(records)
                print(f"Poll OK: {len(records)} usable images.", file=sys.stderr)
                # Fix 5, and deliberately AHEAD of the status write
                # below. Step 1 added several computed status fields
                # here, and status.json is the least important thing this
                # method does: putting the frame refresh first means no
                # future status field can ever cost the picture it was
                # describing by raising into the broad except.
                if not self._has_shown_real_image and self._rotation.current() is not None:
                    self._show_current(fade=True)
                self._write_status(
                    last_poll_at=time.time(),
                    last_poll_ok=True,
                    last_poll_count=len(records),
                    # `image_count` is the number of pictures the
                    # rotation can actually show right now, which is not
                    # len(records): a record whose download was deferred
                    # to the next poll is listed but not yet showable,
                    # and "47 pictures found" that cannot be displayed is
                    # the kind of number that makes a user distrust every
                    # other number on the screen.
                    image_count=self._image_count(),
                    source_label=self._source_label(),
                    last_error=CLEAR,
                    last_error_at=CLEAR,
                )
            else:
                print(
                    "Poll returned no usable images (failure or a genuinely "
                    "empty pool) — cache and rotation left unchanged.",
                    file=sys.stderr,
                )
                self._write_status(
                    last_poll_at=time.time(),
                    last_poll_ok=False,
                    last_poll_count=0,
                    image_count=self._image_count(),
                    source_label=self._source_label(),
                    # "there is no error string; last_poll_ok:
                    # false says *that* something broke, never *what*."
                    # This is the honest wording for the one thing the
                    # source contract actually lets us know here
                    # has list_images() return [] for both "unreachable"
                    # and "genuinely empty", and claiming to distinguish
                    # them would be inventing a diagnosis.
                    last_error=(
                        f"Couldn't get pictures from {self._source_label()}. "
                        f"It may be unreachable, or it may have no pictures "
                        f"in it."
                    ),
                    last_error_at=time.time(),
                )
        except Exception:
            print(
                f"pollImageServer_: unexpected exception, ignoring this "
                f"poll:\n{traceback.format_exc()}",
                file=sys.stderr,
            )
            self._write_status(
                last_error="Something went wrong while looking for pictures.",
                last_error_at=time.time(),
            )

    def advanceRotation_(self, timer) -> None:
        """The rotation timer's target — advances to the *next* id, unlike
        _show_current (used only at startup to display whatever's already
        current without consuming a step).

        Fix 2: if the pool has gone empty, next_image() returns None. If
        a real image is already on screen, leave it alone rather than
        crossfading it away into the empty-fill fallback — "keep showing the last good frame, never blank" applies here just
        as much as to a network/disk failure. The empty-fill fallback is
        only for the genuine first-boot case, already covered by start()'s
        _show_current(fade=False) call, which this branch doesn't touch.

        Pause and blanking both hold the rotation here.
        `Rotation.next_image()` is already a no-op while pinned, so this
        early return is not what makes Pause work — it is what stops a
        pointless 2-second crossfade from the current picture to itself
        every rotation interval, and what keeps the log honest about why
        nothing moved.

        Blanking holds rotation for an explicit reason:
        rotation is *paused, not hidden* while blanked, so restoring
        shows the same picture you left rather than whatever would have
        played on in the dark.

        Fix 3: wrapped in a broad except for the same NSTimer-safety
        reason as pollImageServer_ above."""
        try:
            if self._blanked:
                print(
                    "advanceRotation_: blanked; holding the picture "
                    "underneath (rotation is paused, not hidden).",
                    file=sys.stderr,
                )
                return
            if self._rotation.is_pinned:
                print(
                    "advanceRotation_: paused; leaving the pinned picture "
                    "in place.",
                    file=sys.stderr,
                )
                return
            next_id = self._rotation.next_image()
            if next_id is None and self._has_shown_real_image:
                print(
                    "advanceRotation_: pool is empty; leaving the "
                    "currently-shown image in place rather than blanking it.",
                    file=sys.stderr,
                )
                return
            self._show_image(next_id, fade=True)
        except Exception:
            print(
                f"advanceRotation_: unexpected exception, ignoring this "
                f"tick:\n{traceback.format_exc()}",
                file=sys.stderr,
            )

    @objc.python_method
    def _show_current(self, fade: bool) -> None:
        self._show_image(self._rotation.current(), fade=fade)

    @objc.python_method
    def _show_image(self, image_id: str | None, fade: bool) -> None:
        path = self._cache.get_path(image_id) if image_id else None
        view = self._window.contentView()
        if fade:
            view.transitionToImagePath_duration_(path, self._settings.fade_duration_s)
        else:
            view.setImagePath_(path)
        print(f"Showing: {image_id} ({path})", file=sys.stderr)
        if image_id is not None:
            self._has_shown_real_image = True
            # Fix 6: mark_shown() is what makes cache.py's
            # least-recently-shown eviction order reflect actual display
            # recency instead of every entry tying at last_shown_at=0.
            self._cache.mark_shown(image_id)
        self._write_status(
            last_shown_id=image_id,
            last_shown_at=time.time(),
            display_label=self._display_label(image_id),
        )

    # -- labels -----------------------------------------------------

    @objc.python_method
    def _remember_labels(self, records) -> None:
        """Keep the id -> label map from the freshest listing.

        Rebuilt wholesale rather than merged: a label is only useful for
        a picture that is still in the pool, and merging would grow this
        dict without bound across a long-running service watching a
        folder someone keeps editing."""
        labels: dict[str, str] = {}
        for record in records:
            # getattr, not attribute access: this walks records handed
            # over by a source, and writing a source is something
            # a stranger is invited to do. A third-party source that
            # returns something record-shaped-but-not-quite must cost its
            # own labels, not the whole poll — losing a label means the
            # menu bar falls back to `Picture 12 of 47`, while raising
            # here would land in pollImageServer_'s broad except and
            # discard the entire successful poll.
            image_id = getattr(record, "id", None)
            label = getattr(record, "display_label", "") or ""
            if isinstance(image_id, str) and isinstance(label, str) and label:
                labels[image_id] = label
        self._labels = labels

    @objc.python_method
    def _display_label(self, image_id: str | None) -> str:
        """The source's own label when it has one, and otherwise
        `Picture 12 of 47` — which the plan notes is "more useful than a
        filename anyway, and it is the string a stranger can actually act
        on". Never a bare UUID, which is the whole reason this field
        exists."""
        if image_id is None:
            return ""
        label = self._labels.get(image_id)
        if label:
            return label
        # Never allowed to raise: this is called from _show_image, whose
        # job is putting a picture on a display. A status field failing
        # to compute must not cost the frame it was describing.
        try:
            total = len(self._rotation)
            position = self._rotation.position()
            if total and position:
                return f"Picture {position} of {total}"
        except Exception:  # noqa: BLE001 - see above
            pass
        return ""

    @objc.python_method
    def _image_count(self) -> int:
        """`image_count` — how many pictures the rotation can
        actually show, not how many the source listed. Never raises, for
        the same reason `_display_label` does not."""
        try:
            return len(self._rotation)
        except Exception:  # noqa: BLE001 - a status number is never worth a frame
            return 0

    @objc.python_method
    def _source_label(self) -> str:
        """`source_label`. Falls back to the source's `kind` if a
        source somehow has no label — never raises out of a status
        write, which is the one thing status.json must never do."""
        try:
            return self._source.label or self._source.kind
        except Exception:  # noqa: BLE001 - a label is never worth a crash
            return ""

    # -- desired state ----------------------------------------------

    @objc.python_method
    def control_snapshot(self) -> dict[str, object]:
        """What this Coordinator has actually applied, for status.json.
        Distinct from what the command file *says* — see `_blanked`'s
        declaration."""
        return {
            "blanked": self._blanked,
            "paused": self._paused,
            "paused_on_id": self._rotation.pinned_id(),
        }

    @objc.python_method
    def apply_control_state(self, state, steps: int) -> None:
        """Apply one control-file reading.

        Level-triggered and therefore safe to call with the same state
        repeatedly — every branch below is a no-op when nothing changed,
        which is what lets the caller apply state on startup, on every
        file change, and on Coordinator construction without any of the
        three needing to know about the others.

        Order matters, and encodes rule that **any action
        un-blanks except Pause**:

        1. Pause/resume first, because it must not disturb blanking.
        2. Then blanking from the file's own desired state.
        3. Then actions — a step, or a new calibration preview — which
           override (2) and restore the picture.

        Step 3 overriding step 2 is a deliberate backstop, not the
        primary mechanism: the responsibility sits with the UI to
        clear `blanked` when it sends a Next. The backstop exists so that
        a UI bug cannot produce a permanently dark View that visibly
        ignores every button the user presses — the single worst failure
        this feature has available to it. Because the display never
        writes the command file, the override lasts until the UI writes
        again, which is exactly as long as it should."""
        preview = state.preview_calibration
        preview_is_new = preview is not None and preview != self._preview_calibration
        preview_changed = preview != self._preview_calibration

        self._apply_pause(state)

        # schedule participates in the decision here, but this
        # method only runs when the command *file* changes — and a clock
        # crossing 21:00 changes no file. `apply_blank_state`, called on
        # every control tick, is what actually fires the window edges;
        # the schedule is passed here too so that a command written while
        # a window is open resolves the same way in both paths.
        if state.effective_blanked(self._settings.blank_schedule):
            self._apply_blanked(True)
        else:
            self._apply_blanked(False)

        if steps:
            # Next/Previous restore the picture.
            self._apply_blanked(False)
            shown = self._rotation.step(steps)
            print(
                f"Control: {steps:+d} step(s) -> {shown}.",
                file=sys.stderr,
            )
            self._show_image(shown, fade=True)

        if preview_is_new:
            self._apply_blanked(False)
        self._preview_calibration = preview
        if preview_changed:
            # live nudge actually reaching the glass. Also fires
            # when `preview` goes back to None, which is what makes
            # Cancel (and closing the window) restore the saved circle
            # without the UI having to write the old numbers back.
            self._refresh_drawn_calibration()

    @objc.python_method
    def _apply_pause(self, state) -> None:
        """Pause is the one action that does **not** un-blank:
        pausing a blanked View is a coherent thing to want, and
        un-blanking on it would make Pause the only control that turns
        the picture back *on*.

        `paused_on_id` is honoured only when it changes, or when `paused`
        newly becomes True. It cannot be re-applied on every tick: a
        Next-while-paused moves this display's pin immediately, while the
        UI's `paused_on_id` still names the previous picture until its
        next write — re-pinning from the stale value would drag the
        picture straight back."""
        if state.paused and not self._paused:
            pinned = self._rotation.pin(state.paused_on_id)
            print(f"Control: paused on {pinned}.", file=sys.stderr)
        elif state.paused and state.paused_on_id != self._applied_paused_on_id:
            pinned = self._rotation.pin(state.paused_on_id)
            print(f"Control: pause moved to {pinned}.", file=sys.stderr)
            self._show_image(pinned, fade=True)
        elif not state.paused and self._paused:
            self._rotation.unpin()
            print("Control: rotation resumed.", file=sys.stderr)
        self._paused = state.paused
        self._applied_paused_on_id = state.paused_on_id

    @objc.python_method
    def _apply_blanked(self, blanked: bool) -> None:
        """Drive window.py's renderer pair. Idempotent at both
        levels — this early-returns, and the view's own methods are
        idempotent too."""
        if blanked == self._blanked:
            return
        self._blanked = blanked
        view = self._window.contentView()
        if blanked:
            print("Control: blanking the View.", file=sys.stderr)
            view.fadeToBlackWithDuration_(BLANK_FADE_DURATION_S)
        else:
            print("Control: showing the View again.", file=sys.stderr)
            # No path argument: "restoring shows the same picture
            # you left". The picture was never unloaded.
            view.restoreToImagePath_duration_(None, BLANK_FADE_DURATION_S)

    @objc.python_method
    def _write_status(self, **fields) -> None:
        """"a small local status file (last-successful-fetch
        timestamp, last error) the owner can check if they think to" —
        out-of-band observability instead of any on-screen indication.
        Merges into the existing file rather than overwriting wholesale,
        so e.g. a rotation update doesn't blank out the last poll info."""
        merge_status(started_at=self._started_at, **fields)

    @objc.python_method
    def apply_calibration(self, calibration: Calibration) -> None:
        """Adopt a hot-reloaded calibration — the drawn circle
        changes on the next redraw, with no restart.

        Only the content view's mask geometry depends on calibration; the
        window frame is the screen's frame and does not. Deliberately
        does NOT re-resolve the target screen: `get_view_screen()` keys
        off framebuffer dimensions, so a mid-edit intermediate value (a
        half-typed width) could momentarily match a different monitor and
        move the overlay onto it. Screen re-resolution stays where it
        already is — startup, and the screen-parameters notification.

        @objc.python_method per this project's convention for private
        helpers on an NSObject subclass (a prior bug here was an
        underscore-prefixed method colliding with an internal AppKit
        selector)."""
        self._calibration = calibration
        self._refresh_drawn_calibration()

    @objc.python_method
    def apply_blank_state(self, state, schedule) -> None:
        """Re-resolve effective blank state against the clock.

        Called every control tick, unlike `apply_control_state`, which
        runs only when the command file changes. That difference is the
        whole reason this exists: a schedule's edges are events in time,
        not writes to a file, so nothing else would ever notice 21:00
        arriving.

        Free when nothing changed — `_apply_blanked` early-returns on an
        unchanged value, so the steady-state cost is one comparison and
        a little date arithmetic four times a second.
        """
        self._apply_blanked(state.effective_blanked(schedule))

    @objc.python_method
    def apply_settings(self, settings) -> None:
        """Adopt a hot-reloaded `settings.json` **live**.

        Step -1 deliberately did not do this: the timing knobs were baked
        into scheduled `NSTimer`s and the source into a constructed
        client, and it argued that applying some fields and not others
        would be worse than applying none. That was right at the time and
        is wrong now — settings window is a surface whose entire
        promise is that changing a setting changes what the View does, and
        "restart the agent" is not a sentence that window can contain.

        So every field it can write is applied here, and the ones that
        need no application at all (`fade_duration_s`, `blank_schedule`)
        are read from `self._settings` at point of use and therefore
        follow for free.

        Each branch is guarded on an actual change rather than run
        unconditionally. The watcher fires on any edit to the file, and
        rebuilding the source — which drops the HTTP client, re-lists,
        and re-syncs the cache — on a keystroke that only moved the
        blanking time would be a self-inflicted stall during the exact
        interaction that is supposed to feel immediate.
        """
        previous, self._settings = self._settings, settings

        if settings.shuffle != previous.shuffle:
            # Re-orders in place and keeps the current picture under the
            # cursor where it can, so toggling this does not jump the
            # View to something else as a side effect.
            self._rotation.set_shuffle(settings.shuffle)
            print(
                f"Settings: rotation order is now "
                f"{'shuffled' if settings.shuffle else 'the source order'}.",
                file=sys.stderr,
            )

        source_changed = settings.source != previous.source
        if source_changed:
            self._rebuild_source()

        if source_changed or settings.poll_interval_s != previous.poll_interval_s:
            self._reschedule_poll()
        if settings.rotation_interval_s != previous.rotation_interval_s:
            self._reschedule_rotation()
            print(
                f"Settings: now rotating every "
                f"{settings.rotation_interval_s}s.",
                file=sys.stderr,
            )

    @objc.python_method
    def refresh_now(self) -> None:
        """`Check for new pictures now`. Just the existing poll,
        off its schedule — the timer keeps its own cadence, so this does
        not shift the next scheduled poll."""
        print("Control: checking for new pictures now.", file=sys.stderr)
        self.pollImageServer_(None)

    @objc.python_method
    def _rebuild_source(self) -> None:
        """Swap in a freshly-configured source, cache and rotation.

        The old source is closed first so its HTTP client's sockets go
        with it; `close()` is on the never-raises side of the interface,
        but it is wrapped anyway because failing to build the *new*
        source because the *old* one objected to being closed would be an
        absurd way to lose a display.

        A new source means a new cache namespace and a pool with
        entirely different ids, so the rotation is rebuilt rather than
        re-synced — resuming a persisted walk order keyed by the previous
        source's ids would be meaningless.
        """
        try:
            self._source.close()
        except Exception:  # noqa: BLE001 - never lose a display over this
            print(
                f"Settings: closing the old source failed, continuing:\n"
                f"{traceback.format_exc()}",
                file=sys.stderr,
            )
        self._source = build_source(self._settings.source)
        if self._source.caches:
            self._cache = ImageCache.for_source(
                self._source, max_size=self._settings.cache_max
            )
        else:
            self._cache = DirectStore()
        self._labels = {}
        self._rotation = Rotation(
            self._cache.known_ids(),
            is_valid=self._is_image_valid,
            shuffle=self._settings.shuffle,
        )
        print(
            f"Settings: source is now {self._source.kind!r} "
            f"({self._source_label()}).",
            file=sys.stderr,
        )
        # Poll immediately. Waiting up to poll_interval_s after the user
        # pressed Save in the settings window is indistinguishable from
        # the change not having worked.
        self.pollImageServer_(None)

    @objc.python_method
    def _reschedule_poll(self) -> None:
        if self._poll_timer is not None:
            self._poll_timer.invalidate()
        interval = min(
            self._settings.poll_interval_s, self._source.poll_interval_s
        )
        self._poll_timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            interval, self, "pollImageServer:", None, True
        )

    @objc.python_method
    def _reschedule_rotation(self) -> None:
        """Rebuild the rotation timer at the new interval.

        The elapsed time on the old timer is deliberately discarded: a
        user who just changed 1 day to 1 minute wants a minute from now,
        not "whatever remains of the day". The reverse case — 1 minute to
        1 day — is the same argument in the direction that matters more,
        since inheriting the elapsed fraction could fire it immediately.
        """
        if self._rotation_timer is not None:
            self._rotation_timer.invalidate()
        self._rotation_timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            self._settings.rotation_interval_s, self, "advanceRotation:", None, True
        )

    @objc.python_method
    def _refresh_drawn_calibration(self) -> None:
        """Push `file calibration + any transient preview` at the view.

        The two inputs are kept separate all the way down to here on
        purpose. `self._calibration` is always what the *file* says, so a
        A config hot-reload landing mid-nudge updates the base without
        discarding the preview, and clearing the preview restores the
        file's circle exactly — neither of which is true if the preview
        is allowed to overwrite `self._calibration` on the way in.
        """
        drawn = apply_preview(self._calibration, self._preview_calibration)
        view = self._window.contentView()
        view.calibration = drawn
        view.setNeedsDisplay_(True)


class Bootstrapper(AppKit.NSObject):
    """Owns the "View not found yet" startup phase. "View unplugged
    is not a crash condition... treat it as temporarily absent, idle and
    poll rather than exiting" — this is what makes that true even before
    a Coordinator (and its window) exist at all."""

    def initWithCalibration_settings_(self, calibration, settings):
        self = objc.super(Bootstrapper, self).init()
        if self is None:
            return None
        self._calibration = calibration
        self._settings = settings
        self._coordinator = None
        self._retry_timer = None
        self._control_timer = None
        # Watchers over `~/.viewlab/` only — never the repo's
        # display/config/. Constructed here rather than in Coordinator
        # because the control timer must run during the "View not found
        # yet" phase too: a calibration edit is exactly how a user fixes
        # a framebuffer mismatch that is *why* the View isn't resolving.
        self._calibration_watch = calibration_watcher()
        self._settings_watch = settings_watcher()
        # Constructed here, alongside the config watchers, and for
        # the same reason: the control timer runs during the "View not
        # found yet" phase too, so desired state has to be tracked before
        # a Coordinator exists to apply it to.
        self._control = control.ControlChannel()
        self._last_heartbeat_at = 0.0
        return self

    @objc.python_method
    def start(self) -> None:
        # "on startup, adopt current state as already-seen". Read
        # BEFORE the first tryResolve_ so that a Coordinator constructed
        # in the same breath comes up already blanked/paused if that is
        # what the user left it as — rather than flashing the picture for
        # one tick and then going dark, which would look like a fault.
        self._control.adopt_current()
        print(
            f"Control channel: adopted advance={self._control.last_seen_advance} "
            f"as already-seen; blanked="
            f"{self._control.state.effective_blanked(self._settings.blank_schedule)} "
            f"paused={self._control.state.paused}.",
            file=sys.stderr,
        )
        self.tryResolve_(None)
        self._control_timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            CONTROL_INTERVAL_S, self, "controlTick:", None, True
        )
        if self._coordinator is None:
            print(
                f"View not found yet — retrying every "
                f"{SCREEN_RETRY_INTERVAL_S}s (idle-and-poll, not exiting).",
                file=sys.stderr,
            )
            self._retry_timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                SCREEN_RETRY_INTERVAL_S, self, "tryResolve:", None, True
            )

    def tryResolve_(self, timer) -> None:
        """Fix 3: wrapped in a broad except for the same NSTimer-safety
        reason as Coordinator's pollImageServer_/advanceRotation_ — this
        also runs off an NSTimer selector during the pre-Coordinator
        startup phase and must not be able to take down the run loop."""
        try:
            screen = get_view_screen(self._calibration)
            if screen is None:
                return  # display_target.py already logged why; keep waiting
            if self._retry_timer is not None:
                self._retry_timer.invalidate()
                self._retry_timer = None
            print(f"View found: {screen.frame()}. Starting.", file=sys.stderr)
            self._coordinator = Coordinator.alloc().initWithScreen_calibration_settings_(
                screen, self._calibration, self._settings
            )
            self._coordinator.start()
            # desired state is level-triggered, so replaying it at
            # every point a Coordinator can come into existence is both
            # safe and necessary: this path also covers the View being
            # unplugged and plugged back in, which builds a fresh
            # Coordinator that would otherwise come up un-blanked.
            # `steps=0` deliberately — a Next pressed while the View was
            # unplugged is not a Next anyone still wants.
            self._coordinator.apply_control_state(self._control.state, 0)
        except Exception:
            print(
                f"tryResolve_: unexpected exception, will retry on the "
                f"next tick:\n{traceback.format_exc()}",
                file=sys.stderr,
            )

    def controlTick_(self, timer) -> None:
        """The control timer's target — a *separate* timer from the
        signal-responsiveness heartbeat, deliberately (see
        SIGNAL_RESPONSIVENESS_INTERVAL_S's comment: putting file-stat and
        JSON parsing into the heartbeat, which has no exception wrapper,
        risks losing the ability to stop the service).

        Carries the config hot-reload check, the command-file
        poll, and the heartbeat.

        Wrapped in a broad except for the same NSTimer-safety reason as
        pollImageServer_/advanceRotation_/tryResolve_ — and here it is
        also what makes the plan's acceptance criterion true: a
        malformed config must not stop the control timer. An exception
        escaping this selector would kill the run loop outright, taking
        the display with it.

        The three pieces of work are in one try, not three: they run four
        times a second, and the only correct response to any of them
        failing is identical — log it and try again in 250ms."""
        try:
            self._reload_config()
            self._poll_control()
            self._apply_schedule()
            self._write_heartbeat()
        except Exception:
            print(
                f"controlTick_: unexpected exception, ignoring this "
                f"tick:\n{traceback.format_exc()}",
                file=sys.stderr,
            )

    @objc.python_method
    def _poll_control(self) -> None:
        """Command-file poll.

        `ControlChannel.poll()` returns None for "nothing changed",
        "file is gone" and "file is corrupt" alike, and all three mean
        the same thing here: leave the last good desired state exactly
        as it is. A corrupt command file must never un-blank a blanked
        View, and — because the channel records the file's stat
        signature before parsing it — must never re-fire either."""
        update = self._control.poll()
        if update is None:
            return
        if update.refresh_requested and self._coordinator is not None:
            # Ahead of applying desired state, so that a source the user
            # just changed in the settings window is re-listed in the
            # same tick the rest of the command lands.
            self._coordinator.refresh_now()
        if self._coordinator is None:
            # The View is not resolved yet. The state is already stored
            # on the channel and gets applied by tryResolve_ the moment a
            # Coordinator exists; the counter delta is dropped on
            # purpose, for the same reason startup drops it.
            print(
                "Control: desired state noted, but the View is not "
                "resolved yet; it will be applied when it appears.",
                file=sys.stderr,
            )
            return
        self._coordinator.apply_control_state(update.state, update.steps)
        # Reporting what was applied must not be able to undo having
        # applied it — and specifically must not cost this tick's
        # heartbeat, which is what tells the UI the display is alive
        #. Same rule as _display_label/_image_count.
        try:
            snapshot = self._coordinator.control_snapshot()
            merge_status(
                last_handled_advance=self._control.last_seen_advance, **snapshot
            )
        except Exception:  # noqa: BLE001 - see above
            print(
                f"_poll_control: could not record the applied state; the "
                f"state itself was applied:\n{traceback.format_exc()}",
                file=sys.stderr,
            )

    @objc.python_method
    def _write_heartbeat(self) -> None:
        """`heartbeat_at` — how the UI tells a running
        display from a dead one (stale >5s). Throttled to
        HEARTBEAT_INTERVAL_S rather than written on every tick; see that
        constant for why.

        Written from the Bootstrapper, not the Coordinator, deliberately:
        a display that is alive and healthy but has no View plugged in is
        exactly the case the heartbeat must distinguish from a dead one, and
        it has no Coordinator to write for it. `View not connected` is a
        different menu bar state from `Not showing pictures`, and only a
        heartbeat that ticks in both cases can tell them apart."""
        now = time.time()
        if now - self._last_heartbeat_at < HEARTBEAT_INTERVAL_S:
            return
        self._last_heartbeat_at = now
        merge_status(
            heartbeat_at=now,
            last_handled_advance=self._control.last_seen_advance,
            view_connected=self._coordinator is not None,
        )

    @objc.python_method
    def _apply_schedule(self) -> None:
        """Window edges, checked on the tick (see
        `Coordinator.apply_blank_state`).

        Skipped entirely when no schedule is active, which is the
        default and the case for every user who never opens that part of
        the settings window. That guard is not just a saving: without it
        this would drive `_apply_blanked(False)` every tick and fight the
        manual Blank menu item for control of the same flag.
        """
        if self._coordinator is None:
            return
        schedule = self._settings.blank_schedule
        if not schedule.is_active:
            return
        self._coordinator.apply_blank_state(self._control.state, schedule)

    @objc.python_method
    def _reload_config(self) -> None:
        """Each watcher returns a value only when its file both
        changed and validated; a malformed or vanished file returns None
        and leaves the last-good in-memory config in place, never the
        safe default (mid-calibration, a default would make the circle
        jump wildly on every half-typed edit)."""
        calibration = self._calibration_watch.poll()
        if calibration is not None:
            self._calibration = calibration
            if self._coordinator is not None:
                self._coordinator.apply_calibration(calibration)
            print(
                f"Calibration hot-reloaded: center=({calibration.center_x}, "
                f"{calibration.center_y}) "
                f"effective_radius={calibration.effective_radius_px:.1f}",
                file=sys.stderr,
            )
            merge_status(
                calibration_source=ConfigSource.USER.value,
                calibration_reloaded_at=time.time(),
            )

        settings = self._settings_watch.poll()
        if settings is not None:
            # Applied live as of Step 4. Step -1 recorded the change and
            # told the user to restart, which was honest while the only
            # way to edit this file was by hand; settings window
            # makes it untenable. `Coordinator.apply_settings` owns what
            # each field costs to apply.
            self._settings = settings
            if self._coordinator is not None:
                self._coordinator.apply_settings(settings)
            print("settings.json changed and validated; applied.", file=sys.stderr)
            merge_status(
                settings_source=ConfigSource.USER.value,
                settings_reloaded_at=time.time(),
            )


def main() -> None:
    # Create the writable roots before anything tries to write to them
    #. Non-fatal: ensure_all() logs each failure and the app
    # degrades to "runs, shows images, persists nothing" rather than
    # refusing to start.
    paths.ensure_all()

    # log-rotation requirement, checked once at startup, before any
    # real logging happens - deliberately ahead of even the first print()
    # below, since that print is itself the first byte that could push a
    # long-lived log past its cap on this run. See log_rotation.py for why
    # rotate-and-keep-one-old-generation was chosen over truncate-in-place.
    rotate_if_oversized(STDOUT_LOG_PATH)
    rotate_if_oversized(STDERR_LOG_PATH)

    print("view-lab app.py starting.", file=sys.stderr)

    # single-instance guard, before any window or timer exists.
    # Returning None means another display process already holds the
    # lock; exit(0) — CLEANLY — because KeepAlive{SuccessfulExit: false}
    # does not respawn a clean exit, so the loser stays down instead of
    # respawn-looping against the winner.
    if single_instance.acquire(paths.lock_path()) is None:
        print("view-lab app.py exiting cleanly (another instance is running).", file=sys.stderr)
        sys.exit(0)

    calibration_resolved = load_calibration_resolved()
    settings_resolved = load_settings_resolved()
    calibration: Calibration = calibration_resolved.value
    settings: Settings = settings_resolved.value
    print(
        f"  calibration: center=({calibration.center_x}, {calibration.center_y}) "
        f"effective_radius={calibration.effective_radius_px:.1f} "
        f"[source: {calibration_resolved.source.value} "
        f"{calibration_resolved.path}]",
        file=sys.stderr,
    )
    print(
        f"  settings: rotation_interval_s={settings.rotation_interval_s} "
        f"poll_interval_s={settings.poll_interval_s} "
        f"fade_duration_s={settings.fade_duration_s} "
        f"pictures_from={settings.source.kind!r} "
        f"[source: {settings_resolved.source.value}]",
        file=sys.stderr,
    )
    # "Both apps record which calibration source they used in their
    # status file, so a silent fallback is observable."
    merge_status(
        calibration_source=calibration_resolved.source.value,
        calibration_path=str(calibration_resolved.path) if calibration_resolved.path else None,
        settings_source=settings_resolved.source.value,
    )

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

    # Return value intentionally discarded: the run loop retains this
    # timer internally, so nothing here needs to hold a reference to it.
    AppKit.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
        SIGNAL_RESPONSIVENESS_INTERVAL_S, True, lambda timer: None
    )

    bootstrapper = Bootstrapper.alloc().initWithCalibration_settings_(calibration, settings)
    bootstrapper.start()

    install_signal_handlers(app)
    print("Running until killed (Ctrl+C or SIGTERM).", file=sys.stderr)
    app.run()
    print("view-lab app.py exiting cleanly.", file=sys.stderr)


if __name__ == "__main__":
    main()
