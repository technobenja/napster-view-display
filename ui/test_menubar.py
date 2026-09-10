"""Tests for the menu bar shell's own timer behaviour.

Almost everything in `menubar.py` needs a real menu bar to verify, and
this project has always said so — the decisions live in `menubar_state.py`
and the tests live with them. What is here is the part that is neither a
pure decision nor a drawing: the **timer contract**, three properties of
which are silent when broken.

1. **Cadence.** The stamp is written on a 2.0s throttle sampled by a 0.4s
   poll, and the realized gap is therefore up to 2.4s — which is what has
   to fit inside `STALE_AFTER_S`, not the 2.0s constant.
2. **Ordering and isolation.** The heartbeat is stamped before anything
   else in the tick and in a `try` of its own, so a bug on either side
   cannot be mistaken for a wedge or caused by one.
3. **Run-loop mode.** A default-mode timer stops firing in every other
   mode. That is what lets a stale stamp catch a modal wedge, and it is
   also the source of this mechanism's unavoidable false positives, so it
   is measured here rather than asserted in a comment.

`MenuBarController.init` builds no AppKit object (that is what `start()`
is for), so it can be allocated headlessly — the same technique
`display/test_app.py` uses on `Bootstrapper`.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import AppKit
import objc

from display import paths
from ui import menubar
from ui import menubar_state as ms


class UiHeartbeatTimerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        # `Path.home()`, not `paths.ui_status_path` — redirecting only the
        # one function under test would leave every *other* path in this
        # class resolving to the developer's live `~/.viewlab/`, so a
        # future edit that reached one would write into a running
        # installation and the test would still pass. Redirect the root
        # instead, and the whole class is hermetic.
        self._home_patch = patch("pathlib.Path.home", return_value=self.home)
        self._home_patch.start()
        self.addCleanup(self._home_patch.stop)
        self.assertTrue(str(paths.ui_status_path()).startswith(str(self.home)))
        self.path = paths.ui_status_path()
        self._note_patch = patch.object(menubar.diagnostics, "note")
        self.note = self._note_patch.start()
        self.addCleanup(self._note_patch.stop)
        self.controller = menubar.MenuBarController.alloc().init()

    def at(self, moment: float) -> None:
        """Run one heartbeat with the clock frozen at `moment`."""
        with patch.object(menubar.time, "time", return_value=moment):
            self.controller._write_ui_heartbeat()

    def tick(self, moment: float) -> None:
        """Run one whole `pollTick_` with the clock frozen at `moment`.

        `_refresh` is stubbed out: these tests are about the heartbeat,
        and the real one needs a status item and a built menu."""
        with patch.object(menubar.MenuBarController, "_refresh", lambda self: None):
            with patch.object(menubar.time, "time", return_value=moment):
                self.controller.pollTick_(None)

    def beat(self) -> float:
        return ms.read_ui_heartbeat(self.path)

    # -- cadence ------------------------------------------------------

    def test_the_realized_gap_leaves_two_missed_beats_inside_the_threshold(self):
        """The margin has to be stated in terms of what actually reaches
        the disk, not the throttle constant.

        A 2.0s throttle sampled every 0.4s does not write every 2.0s: the
        tick at t=1.999 is rejected and the next lands at 2.399. So the
        realized worst case is `UI_HEARTBEAT_INTERVAL_S + POLL_INTERVAL_S`
        = 2.4s, two missed beats are 4.8s, and `STALE_AFTER_S` is 5.0 —
        0.2s of slack, which is thin but real.

        Asserting `STALE_AFTER_S >= 2 * UI_HEARTBEAT_INTERVAL_S` instead,
        as this test first did, ignores poll granularity entirely and is
        vacuous where it matters: an interval of 2.5 satisfies it while
        realizing a 2.9s gap, so ONE missed beat (5.8s) already reads
        stale. That mutation survived the whole suite."""
        worst = ms.UI_HEARTBEAT_INTERVAL_S + menubar.POLL_INTERVAL_S
        base = 1000.0
        self.assertFalse(
            ms.is_stale(base, now=base + 2 * worst),
            "two missed beats must not read as stale, or a healthy menu "
            "bar is reported wedged by ordinary timer jitter",
        )
        self.assertTrue(
            ms.is_stale(base, now=base + 3 * worst),
            "three missed beats must read as stale, or a wedge is invisible",
        )

    def test_the_realized_gap_is_measured_not_assumed(self):
        """Drive the real 0.4s poll for a minute and look at what landed
        on disk. Bounds the claim above with observation instead of
        arithmetic."""
        moment, written = 1000.0, []
        for _ in range(150):
            before = self.beat()
            self.at(moment)
            after = self.beat()
            if after != before:
                written.append(after)
            moment += menubar.POLL_INTERVAL_S
        gaps = [b - a for a, b in zip(written, written[1:])]
        self.assertGreater(len(gaps), 20)
        self.assertLessEqual(
            max(gaps), ms.UI_HEARTBEAT_INTERVAL_S + menubar.POLL_INTERVAL_S
        )
        for earlier, later in zip(written, written[1:]):
            self.assertFalse(ms.is_stale(earlier, now=later))

    # -- throttle -----------------------------------------------------

    def test_the_first_tick_writes(self):
        """`_last_ui_heartbeat_at` is seeded to 0.0 precisely so that the
        first tick after startup is unconditional."""
        self.at(1000.0)
        self.assertEqual(self.beat(), 1000.0)

    def test_a_tick_inside_the_interval_does_not_write(self):
        """Writing on every 0.4s tick would be four atomic rewrites a
        second, forever, on a machine meant to sit quietly in someone's
        home for years — the same arithmetic `app.py` did for
        `status.json`."""
        self.at(1000.0)
        self.at(1000.0 + ms.UI_HEARTBEAT_INTERVAL_S - 0.01)
        self.assertEqual(self.beat(), 1000.0)

    def test_a_tick_at_the_interval_writes_again(self):
        self.at(1000.0)
        self.at(1000.0 + ms.UI_HEARTBEAT_INTERVAL_S)
        self.assertEqual(self.beat(), 1000.0 + ms.UI_HEARTBEAT_INTERVAL_S)

    def test_the_throttle_still_applies_when_the_write_is_failing(self):
        """The throttle clock advances whether or not the write succeeded.

        Without that, an unwritable state directory makes every 0.4s tick
        attempt a write — turning a reporting failure into a performance
        one, at five times the intended rate, for as long as the disk
        stays full.

        Driven through `pollTick_` at the real poll interval on purpose:
        calling `_write_ui_heartbeat` directly at 2s spacing never
        exercises the throttle at all, which is why moving this advance
        onto the success path survived the entire suite once."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.mkdir()  # unwritable: a directory where the file goes
        calls: list[float] = []

        def counting(path, now=None):
            calls.append(now)
            return False

        with patch.object(menubar.ms, "write_ui_heartbeat", counting):
            moment = 1000.0
            for _ in range(25):  # 10s of 0.4s ticks
                self.tick(moment)
                moment += menubar.POLL_INTERVAL_S
        self.assertEqual(
            len(calls),
            5,
            f"expected a write attempt every {ms.UI_HEARTBEAT_INTERVAL_S}s "
            f"across 10s, got {len(calls)} — the throttle is not holding "
            f"while the write fails",
        )

    def test_a_backwards_clock_step_does_not_stall_the_heartbeat(self):
        """NTP steps, a VM resuming, daylight-adjacent handling.

        If `now` jumps backwards the elapsed time goes negative, and a
        bare `elapsed < INTERVAL` throttle then writes nothing until the
        clock catches up. That window is invisible rather than merely
        wrong: `is_stale()` deliberately treats a future timestamp as
        fresh, so a reader sees a healthy process for the whole of it and
        a wedge inside it cannot be seen at all."""
        self.at(1000.0)
        self.at(940.0)  # the clock stepped back a minute
        self.assertEqual(
            self.beat(),
            940.0,
            "a backwards clock step must not suspend the heartbeat",
        )

    # -- ordering and isolation inside the tick ------------------------

    def test_the_heartbeat_is_stamped_even_when_the_refresh_raises(self):
        """The reason the heartbeat goes first. `_refresh` reads two files
        written by other processes and drives the title; if it started
        raising every tick, a heartbeat ordered after it would stop, this
        process would read as wedged, and the remedy for a wedge is a
        SIGKILL of a run loop that was in fact perfectly healthy."""

        def boom(self) -> None:
            raise RuntimeError("refresh is broken")

        with patch.object(menubar.MenuBarController, "_refresh", boom):
            with patch.object(menubar.time, "time", return_value=1000.0):
                with contextlib.redirect_stderr(io.StringIO()) as logged:
                    self.controller.pollTick_(None)
        self.assertEqual(self.beat(), 1000.0)
        self.assertIn("refresh is broken", logged.getvalue())

    def test_a_raising_heartbeat_does_not_suppress_the_menu(self):
        """The mirror image, and the reason for two `try` blocks rather
        than one. A single block ordered heartbeat-first would let any
        fault in the heartbeat stop the menu updating on every tick,
        permanently, at 0.4s — which is a worse outcome than the one the
        ordering was chosen to avoid."""
        refreshed: list[int] = []

        def boom(self) -> None:
            raise RuntimeError("heartbeat is broken")

        with patch.object(menubar.MenuBarController, "_write_ui_heartbeat", boom):
            with patch.object(
                menubar.MenuBarController, "_refresh", lambda self: refreshed.append(1)
            ):
                with contextlib.redirect_stderr(io.StringIO()) as logged:
                    self.controller.pollTick_(None)
        self.assertEqual(refreshed, [1], "the menu must still be refreshed")
        self.assertIn("heartbeat is broken", logged.getvalue())

    def test_a_raising_refresh_does_not_escape_the_tick(self):
        """An exception off an NSTimer selector kills the run loop."""

        def boom(self) -> None:
            raise RuntimeError("refresh is broken")

        with patch.object(menubar.MenuBarController, "_refresh", boom):
            with contextlib.redirect_stderr(io.StringIO()):
                self.controller.pollTick_(None)  # must not raise

    # -- one writer per file ------------------------------------------

    def test_the_menu_bar_never_writes_the_display_agents_status_file(self):
        """One writer per file is the load-bearing rule of the two-process
        design, and it is a *behavioural* claim — that these are different
        paths is only a naming test. Drive real ticks and confirm nothing
        in this process ever creates `status.json`."""
        moment = 1000.0
        for _ in range(25):
            self.tick(moment)
            moment += menubar.POLL_INTERVAL_S
        self.assertTrue(self.path.exists())
        self.assertFalse(
            paths.status_path().exists(),
            "the menu bar created the display agent's status file",
        )
        self.assertFalse(paths.command_path().exists())

    # -- failure reporting --------------------------------------------

    def test_an_unwritable_heartbeat_never_raises_out_of_the_tick(self):
        """The safety argument for putting this on a live timer. `app.py`
        reverted the same idea for its signal-responsiveness timer because
        the failure mode of a raising heartbeat is losing the ability to
        stop the service."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.mkdir()
        self.at(1000.0)  # must not raise

    def test_a_write_failure_is_reported_once_not_every_tick(self):
        """A menu bar that cannot write this file is healthy but reads as
        wedged, so it has to say so. Once: at 2s intervals, saying it every
        time would be ~43,000 lines a day through a rotated 10 MB log."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.mkdir()
        self.at(1000.0)
        self.at(1000.0 + 10 * ms.UI_HEARTBEAT_INTERVAL_S)
        self.at(1000.0 + 20 * ms.UI_HEARTBEAT_INTERVAL_S)
        self.assertEqual(self.note.call_count, 1)
        self.assertIn("wedged", self.note.call_args[0][0])

    def test_recovery_is_reported_too(self):
        """Otherwise the log's last word on the subject is a warning that
        stopped being true hours ago."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.mkdir()
        self.at(1000.0)
        self.path.rmdir()
        self.at(1000.0 + 10 * ms.UI_HEARTBEAT_INTERVAL_S)
        self.assertEqual(self.note.call_count, 2)
        self.assertIn("writable again", self.note.call_args[0][0])
        self.assertEqual(self.beat(), 1000.0 + 10 * ms.UI_HEARTBEAT_INTERVAL_S)

    def test_a_healthy_heartbeat_says_nothing_at_all(self):
        self.at(1000.0)
        self.at(1000.0 + 10 * ms.UI_HEARTBEAT_INTERVAL_S)
        self.note.assert_not_called()

    # -- the file itself ----------------------------------------------

    def test_the_controller_writes_the_path_paths_py_names(self):
        """The path comes from `paths.py` like every other writable
        location — not from a string built here."""
        self.at(1000.0)
        self.assertEqual(
            json.loads(self.path.read_text()), {"ui_heartbeat_at": 1000.0}
        )


class _ModeCounter(AppKit.NSObject):
    """Counts timer fires. An `NSObject` because `NSTimer` needs a target."""

    def init(self):
        self = objc.super(_ModeCounter, self).init()
        if self is None:
            return None
        self.fires = 0
        return self

    def tick_(self, timer) -> None:
        self.fires += 1


class RunLoopModeContractTests(unittest.TestCase):
    """The premise the whole mechanism rests on, measured rather than cited.

    `scheduledTimerWithTimeInterval_...` registers in
    `NSDefaultRunLoopMode` only. A run loop spun in any other mode — by
    `runModal()`, or by an open menu tracking events — does not fire it.

    This cuts both ways and both directions matter:

    * it is what lets a stale `ui_heartbeat_at` detect a modal wedge, the
      leading suspect for the 2026-09-08 incident;
    * it is why an open file picker also looks stale, which is why nothing
      may act destructively on staleness alone.

    Neither claim is safe to leave in a comment. Note the positive control
    in the first test: without it a mode test passes for the uninteresting
    reason that nothing was ever going to fire.
    """

    def _fires_in(self, mode: str, seconds: float = 0.3) -> int:
        counter = _ModeCounter.alloc().init()
        timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.02, counter, "tick:", None, True
        )
        try:
            loop = AppKit.NSRunLoop.currentRunLoop()
            deadline = AppKit.NSDate.dateWithTimeIntervalSinceNow_(seconds)
            while AppKit.NSDate.date().compare_(deadline) < 0:
                loop.runMode_beforeDate_(
                    mode, AppKit.NSDate.dateWithTimeIntervalSinceNow_(0.02)
                )
        finally:
            timer.invalidate()
        return counter.fires

    def test_a_scheduled_timer_fires_in_the_default_mode(self):
        """The positive control. Everything below is only meaningful
        because this one is non-zero on the same machine, in the same
        process, moments earlier."""
        self.assertGreater(self._fires_in(AppKit.NSDefaultRunLoopMode), 0)

    def test_a_scheduled_timer_does_not_fire_while_a_modal_is_up(self):
        """`runModal()` spins the loop in this mode. This is the property
        that makes a wedged modal detectable — and it is also why the
        About box and `NSOpenPanel.runModal()` in the setup flow go stale
        while working perfectly."""
        self.assertEqual(self._fires_in(AppKit.NSModalPanelRunLoopMode), 0)

    def test_a_scheduled_timer_does_not_fire_while_a_menu_is_open(self):
        """An open `NSMenu` tracks in this mode, so a user simply reading
        the menu freezes the heartbeat for as long as they read it."""
        self.assertEqual(self._fires_in(AppKit.NSEventTrackingRunLoopMode), 0)


class TimerRegistrationTests(unittest.TestCase):
    def test_the_poll_timer_is_never_moved_to_common_modes(self):
        """A source assertion, deliberately, and the weakest test in this
        file — it checks a pattern, not a behaviour, which this project
        normally refuses to accept.

        It earns its place because the failure it guards is silent and
        catastrophic to the feature: adding the poll timer to
        `NSRunLoopCommonModes` would make it keep firing through a modal
        wedge, so a process that can answer nothing would stamp itself
        healthy every 2 seconds and be believed. Nothing observable would
        change until the day it mattered. Verifying it behaviourally would
        need the timer's registration to be injectable, which is a change
        to `start()` this phase does not own.

        The string is allowed in commentary — that is where the reasoning
        lives — and nowhere else."""
        source = (Path(__file__).resolve().parent / "menubar.py").read_text()
        offenders = [
            f"{n}: {line.strip()}"
            for n, line in enumerate(source.splitlines(), start=1)
            if "NSRunLoopCommonModes" in line and not line.lstrip().startswith("#")
        ]
        self.assertEqual(
            offenders,
            [],
            "the poll timer must stay in the default run-loop mode; see the "
            f"DO NOT MOVE block in menubar.py:\n{offenders}",
        )

    def test_the_guard_can_actually_see_the_string_it_looks_for(self):
        """Learning 2, applied to the test above: a guard that scans for a
        term nothing contains passes for the wrong reason. The DO NOT MOVE
        comment names the mode, so a zero total hit count means the file
        moved or the comment was deleted, not that the code is safe."""
        source = (Path(__file__).resolve().parent / "menubar.py").read_text()
        self.assertIn("NSRunLoopCommonModes", source)


if __name__ == "__main__":
    unittest.main()
