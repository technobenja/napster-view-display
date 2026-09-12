"""The truth table for what a losing launch says, and nothing else.

Pure-function tests: no AppKit, no window server, no filesystem, no
clock. `ui/test_contention.py` covers the half that touches the machine.

Two of these are load-bearing beyond their own assertion:

* `LayerTests` walks the **measured** `NSWindow` level table. The plan
  this phase implements says "layer 0 = a panel", and an `NSAlert` is
  level **8**. A `layer == 0` test would have sent the About box — the
  exact 51.2-second freeze the plan measured — into the "not responding,
  force quit pid N" branch, which is the accusation the whole phase
  exists to prevent. That constant is worth a test per level.

* `VisibleIconTests` covers the fact that `kCGWindowIsOnscreen` is not a
  claim about a person. macOS gives a status item a window on every
  attached screen, and one of this app's screens is a round 2.1-inch
  panel with a full-screen picture on it.
"""

from __future__ import annotations

import unittest

from ui import contention_state as cs
from ui import menubar_state as ms

#: A real installed-bundle executable path, as `proc_pidpath` reports it.
BUNDLE = "/Applications/ImageView.app/Contents/MacOS/ImageView"

#: What a source-tree run reports — measured; note it is NOT
#: `sys.executable`.
VENV = (
    "/opt/homebrew/Cellar/python@3.13/3.13.12_1/Frameworks/Python.framework"
    "/Versions/3.13/Resources/Python.app/Contents/MacOS/Python"
)

NOW = 10_000.0
FRESH = NOW - 1.0
STALE = NOW - (ms.UI_UNRESPONSIVE_AFTER_S + 1.0)

#: The observed geometry: the display agent's window and the two status
#: items the menu bar owns, one on each screen.
#:
#: Pids here are FIXTURES (4242 the menu bar, 4243 the display agent) and
#: must stay fixtures — a real pid from the machine a test was written on
#: is an environment fact, and these files ship. Note the holder is the
#: *lower* of the two on purpose: nothing may encode an ordering the
#: modules explicitly forbid inferring from.
DISPLAY_WINDOW = cs.WindowFact(layer=1000, on_screen=True, x=313, y=-960, width=960, height=960)
ICON_ON_MENU_BAR = cs.WindowFact(layer=25, on_screen=True, x=1169, y=0, width=34, height=24)
ICON_ON_THE_VIEW = cs.WindowFact(layer=25, on_screen=True, x=842, y=-960, width=34, height=24)
AN_ALERT = cs.WindowFact(layer=8, on_screen=True, x=400, y=300, width=420, height=160)


def facts(**overrides) -> cs.Facts:
    """A holder that is identified, fresh, writable and visible."""
    base = dict(
        lock_path="/home/.viewlab/ui.lock",
        lock_pid=4242,
        holder_exe=BUNDLE,
        holder_started_at=(1_700_000_000, 123_456),
        our_exe=BUNDLE,
        display_pid=4243,
        heartbeat_at=FRESH,
        heartbeat_present=True,
        heartbeat_pid=4242,
        stacks_armed=True,
        stacks_path="/logs/ui.stacks.log",
        state_dir="/home/.viewlab/state",
        state_writable=True,
        state_detail=None,
        windows=(ICON_ON_MENU_BAR,),
        display_windows=(DISPLAY_WINDOW,),
        now=NOW,
    )
    base.update(overrides)
    return cs.Facts(**base)


class IdentityTests(unittest.TestCase):
    def test_the_installed_bundle_is_ours(self) -> None:
        self.assertTrue(cs.is_ours(BUNDLE, VENV))

    def test_our_own_executable_is_ours_even_when_it_is_a_bare_interpreter(self) -> None:
        """The development case, and the reason the comparison is against
        `proc_pidpath(getpid())` rather than `sys.executable`."""
        self.assertTrue(cs.is_ours(VENV, VENV))

    def test_a_foreign_executable_is_not_ours(self) -> None:
        self.assertFalse(cs.is_ours("/bin/sleep", BUNDLE))

    def test_a_different_interpreter_is_not_ours(self) -> None:
        """Two unrelated Python processes must not identify each other."""
        self.assertFalse(cs.is_ours("/usr/bin/python3", VENV))

    def test_nothing_is_not_ours(self) -> None:
        self.assertFalse(cs.is_ours(None, BUNDLE))
        self.assertFalse(cs.is_ours("", BUNDLE))

    def test_an_unknown_own_executable_still_accepts_a_bundle(self) -> None:
        self.assertTrue(cs.is_ours(BUNDLE, None))

    def test_a_lookalike_basename_outside_a_bundle_is_not_ours(self) -> None:
        self.assertFalse(cs.is_ours("/tmp/ImageView", BUNDLE))

    def test_a_bundle_named_executable_in_another_bundle_is_not_ours(self) -> None:
        self.assertFalse(
            cs.is_ours("/Applications/Other.app/Contents/MacOS/ImageView", BUNDLE)
        )


class UnidentifiedTests(unittest.TestCase):
    """Five ways the holder cannot be identified, each named in the
    message — "something holds the lock" with no reason sends the next
    reader to the wrong process."""

    def test_an_identified_holder_has_no_reason(self) -> None:
        self.assertIsNone(cs.unidentified_reason(facts()))

    def test_no_pid_in_the_lock_file(self) -> None:
        reason = cs.unidentified_reason(facts(lock_pid=None))
        self.assertIsNotNone(reason)
        self.assertIn("no usable pid", reason)

    def test_a_pid_that_does_not_exist(self) -> None:
        reason = cs.unidentified_reason(facts(holder_exe=None))
        self.assertIn("does not exist", reason)

    def test_a_foreign_executable(self) -> None:
        reason = cs.unidentified_reason(facts(holder_exe="/bin/sleep"))
        self.assertIn("/bin/sleep", reason)

    def test_the_lock_and_the_heartbeat_disagree(self) -> None:
        """🔴 The row that gates the signal. If these disagree, nothing
        may be signalled — the pid in the lock may be a recycled one."""
        reason = cs.unidentified_reason(facts(heartbeat_pid=999))
        self.assertIn("disagree", reason)

    def test_one_pid_holding_both_locks_is_impossible(self) -> None:
        """The two locks are different files guarding different roles so
        that force-quitting one half leaves the other running. One pid
        holding both is a state this app cannot produce, and the message
        that must never come out of it is "leave the other one alone"."""
        reason = cs.unidentified_reason(facts(display_pid=4242))
        self.assertIn("both", reason)

    def test_a_missing_heartbeat_pid_is_not_a_disagreement(self) -> None:
        """v1.1.5 wrote no pid at all. An older holder must read as
        identified, not as an impostor."""
        self.assertIsNone(cs.unidentified_reason(facts(heartbeat_pid=None)))

    def test_a_missing_display_pid_is_not_a_disagreement(self) -> None:
        self.assertIsNone(cs.unidentified_reason(facts(display_pid=None)))


class TruthTableTests(unittest.TestCase):
    """Every reachable combination of the four observations, written out
    rather than derived — a table computed from the same rules the
    classifier uses would pass no matter what either of them said."""

    CASES = (
        # -- identity fails, and nothing else is looked at -------------
        ("no pid at all", dict(lock_pid=None), cs.Verdict.UNIDENTIFIED),
        ("pid is gone", dict(holder_exe=None), cs.Verdict.UNIDENTIFIED),
        ("pid is something else", dict(holder_exe="/bin/sleep"), cs.Verdict.UNIDENTIFIED),
        ("pids disagree", dict(heartbeat_pid=41), cs.Verdict.UNIDENTIFIED),
        ("one pid, both locks", dict(display_pid=4242), cs.Verdict.UNIDENTIFIED),
        (
            "unidentified outranks a stale unwritable wedge",
            dict(holder_exe="/bin/sleep", heartbeat_at=STALE, state_writable=False, windows=()),
            cs.Verdict.UNIDENTIFIED,
        ),
        # -- fresh -----------------------------------------------------
        ("fresh, icon on the menu bar", dict(), cs.Verdict.SERVING_VISIBLE),
        (
            "fresh, window server unavailable",
            dict(windows=None),
            cs.Verdict.SERVING_VISIBLE,
        ),
        (
            "fresh, icon and a dialog",
            dict(windows=(ICON_ON_MENU_BAR, AN_ALERT)),
            cs.Verdict.SERVING_VISIBLE,
        ),
        ("fresh, nothing on screen", dict(windows=()), cs.Verdict.SERVING_HIDDEN),
        (
            "fresh, only the icon parked on the picture display",
            dict(windows=(ICON_ON_THE_VIEW,)),
            cs.Verdict.SERVING_HIDDEN,
        ),
        (
            "fresh, a window but no icon",
            dict(windows=(AN_ALERT,)),
            cs.Verdict.SERVING_HIDDEN,
        ),
        (
            "fresh exactly at the threshold is still fresh",
            dict(heartbeat_at=NOW - ms.UI_UNRESPONSIVE_AFTER_S),
            cs.Verdict.SERVING_VISIBLE,
        ),
        (
            "a future stamp is fresh, not stale — clocks step backwards",
            dict(heartbeat_at=NOW + 500.0),
            cs.Verdict.SERVING_VISIBLE,
        ),
        # -- stale, and the state folder is the reason -----------------
        (
            "stale because it cannot write state/",
            dict(heartbeat_at=STALE, state_writable=False),
            cs.Verdict.MUTED,
        ),
        (
            "muted outranks every window reading",
            dict(heartbeat_at=STALE, state_writable=False, windows=()),
            cs.Verdict.MUTED,
        ),
        (
            "never wrote a stamp and cannot write state/",
            dict(heartbeat_at=0.0, state_writable=False),
            cs.Verdict.MUTED,
        ),
        (
            # Caught by mutation: moving the MUTED check below the window
            # readings survived the whole suite, because no case combined
            # an unwritable state/ with a *non-status* window. A process
            # that cannot write its heartbeat AND has a dialog up is two
            # benign readings at once, and muteness is the one that
            # explains the staleness.
            "muted outranks a dialog on screen",
            dict(
                heartbeat_at=STALE,
                state_writable=False,
                windows=(ICON_ON_MENU_BAR, AN_ALERT),
            ),
            cs.Verdict.MUTED,
        ),
        (
            "muted outranks an unreadable window list",
            dict(heartbeat_at=STALE, state_writable=False, windows=None),
            cs.Verdict.MUTED,
        ),
        (
            "muted outranks an icon-only reading",
            dict(
                heartbeat_at=STALE, state_writable=False, windows=(ICON_ON_MENU_BAR,)
            ),
            cs.Verdict.MUTED,
        ),
        # -- stale, writable -------------------------------------------
        (
            "stale with a dialog on screen",
            dict(heartbeat_at=STALE, windows=(ICON_ON_MENU_BAR, AN_ALERT)),
            cs.Verdict.WINDOW_OPEN,
        ),
        (
            "stale with a dialog and no icon",
            dict(heartbeat_at=STALE, windows=(AN_ALERT,)),
            cs.Verdict.WINDOW_OPEN,
        ),
        (
            "stale, icon only",
            dict(heartbeat_at=STALE, windows=(ICON_ON_MENU_BAR,)),
            cs.Verdict.UNRESPONSIVE,
        ),
        (
            "stale, icon only and parked on the picture display",
            dict(heartbeat_at=STALE, windows=(ICON_ON_THE_VIEW,)),
            cs.Verdict.UNRESPONSIVE,
        ),
        (
            "stale, window server unavailable",
            dict(heartbeat_at=STALE, windows=None),
            cs.Verdict.UNRESPONSIVE,
        ),
        (
            "stale, nothing on screen at all",
            dict(heartbeat_at=STALE, windows=()),
            cs.Verdict.NEVER_ARRIVED,
        ),
        (
            "never wrote a stamp and nothing on screen",
            dict(heartbeat_at=0.0, windows=()),
            cs.Verdict.NEVER_ARRIVED,
        ),
        (
            "just past the threshold",
            dict(heartbeat_at=NOW - ms.UI_UNRESPONSIVE_AFTER_S - 0.01, windows=()),
            cs.Verdict.NEVER_ARRIVED,
        ),
        # -- no heartbeat FILE at all, which is not the same as a cold
        # -- stamp. Every build before v1.1.5 looks like this, healthy,
        # -- and this app upgrades by dragging a new bundle over a
        # -- running old one — so this is the modal case on upgrade day,
        # -- not an edge case. FOUND by running the classifier against
        # -- the live machine's own v1.1.4 menu bar.
        (
            "no heartbeat file, icon on the menu bar",
            dict(heartbeat_at=0.0, heartbeat_present=False),
            cs.Verdict.NEVER_REPORTED,
        ),
        (
            "no heartbeat file, icon only on the picture display",
            dict(
                heartbeat_at=0.0, heartbeat_present=False, windows=(ICON_ON_THE_VIEW,)
            ),
            cs.Verdict.NEVER_REPORTED,
        ),
        (
            "no heartbeat file, window server unavailable",
            dict(heartbeat_at=0.0, heartbeat_present=False, windows=None),
            cs.Verdict.NEVER_REPORTED,
        ),
        (
            "no heartbeat file, but a dialog is on screen",
            dict(heartbeat_at=0.0, heartbeat_present=False, windows=(AN_ALERT,)),
            cs.Verdict.WINDOW_OPEN,
        ),
        (
            # A healthy older build still puts an icon in the menu bar,
            # so nothing on screen at all means stuck at either vintage.
            "no heartbeat file and nothing on screen is still stuck",
            dict(heartbeat_at=0.0, heartbeat_present=False, windows=()),
            cs.Verdict.NEVER_ARRIVED,
        ),
        (
            "no heartbeat file does not outrank an unwritable state folder",
            dict(heartbeat_at=0.0, heartbeat_present=False, state_writable=False),
            cs.Verdict.MUTED,
        ),
        (
            # The file exists, so the holder is a build that writes one —
            # it was reporting and stopped. That IS evidence.
            "a file that exists with an unusable stamp is not an old build",
            dict(heartbeat_at=0.0, heartbeat_present=True),
            cs.Verdict.UNRESPONSIVE,
        ),
        (
            "an offscreen window is not a window on screen",
            dict(
                heartbeat_at=STALE,
                windows=(cs.WindowFact(layer=0, on_screen=False, x=0, y=0, width=9, height=9),),
            ),
            cs.Verdict.NEVER_ARRIVED,
        ),
    )

    def test_the_whole_table(self) -> None:
        for label, overrides, expected in self.CASES:
            with self.subTest(label):
                self.assertIs(cs.classify(facts(**overrides)), expected)

    def test_every_verdict_is_reachable(self) -> None:
        """A verdict no case produces is a row nobody has ever seen."""
        produced = {cs.classify(facts(**over)) for _, over, _ in self.CASES}
        self.assertEqual(produced, set(cs.Verdict))

    def test_the_stale_threshold_is_not_the_menu_title_one(self) -> None:
        """`STALE_AFTER_S` is 5.0 with 0.2s of slack and is tuned for a
        label. A stamp between the two thresholds must still read as
        serving here."""
        self.assertGreater(ms.UI_UNRESPONSIVE_AFTER_S, ms.STALE_AFTER_S)
        between = NOW - (ms.STALE_AFTER_S + 1.0)
        self.assertTrue(ms.is_stale(between, NOW))
        self.assertIs(cs.classify(facts(heartbeat_at=between)), cs.Verdict.SERVING_VISIBLE)


class LayerTests(unittest.TestCase):
    """🔴 `layer != 25`, never `layer == 0`.

    MEASURED from AppKit on this machine, 2026-09-10, and confirmed live
    against the running app (`kCGWindowLayer` equals the `NSWindow`
    level: the display agent reports 1000, the status items 25).
    """

    LEVELS = {
        0: "NSNormalWindowLevel — Settings, First Run",
        3: "NSFloatingWindowLevel",
        8: "NSModalPanelWindowLevel — every NSAlert.runModal(), the About box",
        24: "NSMainMenuWindowLevel",
        101: "NSPopUpMenuWindowLevel — an open NSMenu",
        1001: "the calibration overlay and the Identify flash",
        7777: "a level this app has never seen",
    }

    def test_every_non_status_layer_reads_as_a_window_on_screen(self) -> None:
        for layer, what in self.LEVELS.items():
            with self.subTest(f"layer {layer}: {what}"):
                window = cs.WindowFact(layer=layer, on_screen=True, x=1, y=2, width=3, height=4)
                self.assertIs(
                    cs.classify(facts(heartbeat_at=STALE, windows=(window,))),
                    cs.Verdict.WINDOW_OPEN,
                    f"layer {layer} ({what}) fell through to an accusation",
                )

    def test_the_about_box_is_never_accused(self) -> None:
        """The specific regression. M1 measured an About box freezing the
        heartbeat for 51.2s — well past the 20s threshold — so this is
        not a hypothetical combination."""
        about = cs.WindowFact(layer=8, on_screen=True, x=500, y=400, width=360, height=200)
        frozen = facts(heartbeat_at=NOW - 51.2, windows=(ICON_ON_MENU_BAR, about))
        self.assertIs(cs.classify(frozen), cs.Verdict.WINDOW_OPEN)
        body = cs.notice(frozen, cs.classify(frozen)).body
        self.assertNotIn("force quit", body.lower())
        self.assertIn("bring that window forward", body)

    def test_the_status_item_layer_alone_is_not_a_window(self) -> None:
        self.assertIs(
            cs.classify(facts(heartbeat_at=STALE, windows=(ICON_ON_MENU_BAR,))),
            cs.Verdict.UNRESPONSIVE,
        )

    def test_the_constant_is_still_the_measured_one(self) -> None:
        self.assertEqual(cs.STATUS_ITEM_LAYER, 25)


class VisibleIconTests(unittest.TestCase):
    """"On screen" is the window server's word, not a claim about a
    person. MEASURED: the live menu bar owns a layer-25 window on the
    Napster View, `kCGWindowIsOnscreen: True`, underneath a 960x960
    picture."""

    def test_an_icon_buried_under_the_picture_display_is_not_visible(self) -> None:
        self.assertEqual(
            cs.visible_status_items((ICON_ON_THE_VIEW,), (DISPLAY_WINDOW,)), ()
        )

    def test_the_menu_bar_icon_is_visible(self) -> None:
        self.assertEqual(
            cs.visible_status_items((ICON_ON_MENU_BAR,), (DISPLAY_WINDOW,)),
            (ICON_ON_MENU_BAR,),
        )

    def test_the_live_pair_resolves_to_exactly_one_visible_icon(self) -> None:
        """Both status items, both on screen, exactly as measured."""
        self.assertEqual(
            cs.visible_status_items(
                (ICON_ON_THE_VIEW, ICON_ON_MENU_BAR), (DISPLAY_WINDOW,)
            ),
            (ICON_ON_MENU_BAR,),
        )

    def test_without_display_windows_nothing_is_excluded(self) -> None:
        """Could not look — so fail toward the benign reading rather than
        telling a user their icon is hidden when it may not be."""
        self.assertEqual(
            cs.visible_status_items((ICON_ON_THE_VIEW,), None), (ICON_ON_THE_VIEW,)
        )
        self.assertIs(
            cs.classify(facts(windows=(ICON_ON_THE_VIEW,), display_windows=None)),
            cs.Verdict.SERVING_VISIBLE,
        )

    def test_an_offscreen_display_window_excludes_nothing(self) -> None:
        offscreen = cs.WindowFact(
            layer=1000, on_screen=False, x=313, y=-960, width=960, height=960
        )
        self.assertEqual(
            cs.visible_status_items((ICON_ON_THE_VIEW,), (offscreen,)),
            (ICON_ON_THE_VIEW,),
        )

    def test_a_stale_holder_with_a_buried_icon_still_reached_the_menu_bar(self) -> None:
        """`NEVER_ARRIVED` says the status item was never created. An
        icon parked on the picture display *was* created, so the verdict
        must be UNRESPONSIVE — this is `status_items`, not
        `visible_status_items`, on purpose."""
        self.assertIs(
            cs.classify(facts(heartbeat_at=STALE, windows=(ICON_ON_THE_VIEW,))),
            cs.Verdict.UNRESPONSIVE,
        )


class MessageTests(unittest.TestCase):
    def notice_for(self, **overrides) -> cs.Notice:
        f = facts(**overrides)
        return cs.notice(f, cs.classify(f))

    def test_every_verdict_renders(self) -> None:
        """Total: no verdict may fall off the end of the renderer."""
        for verdict in cs.Verdict:
            with self.subTest(verdict.value):
                note = cs.notice(facts(), verdict)
                self.assertTrue(note.headline)
                self.assertTrue(note.body)
                self.assertIs(note.verdict, verdict)

    def test_a_force_quit_instruction_always_names_the_pid(self) -> None:
        """🔴 Both halves of this app are called ImageView in Activity
        Monitor, and the other one is drawing the pictures."""
        for verdict in cs.Verdict:
            body = cs.notice(facts(heartbeat_at=STALE), verdict).body
            if "force quit" in body.lower():
                with self.subTest(verdict.value):
                    self.assertIn("pid 4242", body)

    def test_the_force_quit_instruction_names_the_display_pid_to_spare(self) -> None:
        note = self.notice_for(heartbeat_at=STALE, windows=())
        self.assertIn("pid 4243", note.body)
        self.assertIn("leave", note.body)

    def test_an_unverified_display_pid_is_never_printed(self) -> None:
        """An unverified number in a "leave this one alone" sentence
        points confidently at whatever happens to be running under that
        id."""
        note = self.notice_for(heartbeat_at=STALE, windows=(), display_pid=None)
        self.assertIn("pid 4242", note.body)
        self.assertNotIn("4243", note.body)

    def test_a_holder_that_never_reported_is_not_accused(self) -> None:
        """🔴 The upgrade-day case. A build older than v1.1.5 writes no
        heartbeat file ever, and this app upgrades by dragging a new
        bundle over a running old one — so the first contended launch
        after an upgrade meets exactly this holder. It is healthy."""
        note = self.notice_for(heartbeat_at=0.0, heartbeat_present=False)
        self.assertIs(note.verdict, cs.Verdict.NEVER_REPORTED)
        self.assertIn("most likely an older copy that is perfectly fine", note.body)
        self.assertIn("Click its menu bar icon", note.body)
        self.assertNotIn("not responding", note.body)

    def test_the_never_reported_message_still_names_the_pid_if_it_comes_to_that(
        self,
    ) -> None:
        note = self.notice_for(heartbeat_at=0.0, heartbeat_present=False)
        self.assertIn("force quit pid 4242", note.body)

    def test_a_cold_stamp_is_still_an_accusation(self) -> None:
        """The softening must not swallow the real fault: a file that
        exists and has gone cold is a process that was reporting and
        stopped."""
        note = self.notice_for(heartbeat_at=STALE, heartbeat_present=True)
        self.assertIs(note.verdict, cs.Verdict.UNRESPONSIVE)
        self.assertIn("not responding", note.body)

    def test_unresponsive_and_never_arrived_read_identically(self) -> None:
        """Merged in presentation, distinct in the classifier: the
        difference is everything to a diagnostician and nothing to the
        person at the screen."""
        icon_only = self.notice_for(heartbeat_at=STALE, windows=(ICON_ON_MENU_BAR,))
        nothing = self.notice_for(heartbeat_at=STALE, windows=())
        self.assertIs(icon_only.verdict, cs.Verdict.UNRESPONSIVE)
        self.assertIs(nothing.verdict, cs.Verdict.NEVER_ARRIVED)
        self.assertEqual(icon_only.headline, nothing.headline)

    def test_the_two_serving_messages_give_different_instructions(self) -> None:
        visible = self.notice_for()
        hidden = self.notice_for(windows=(ICON_ON_THE_VIEW,))
        self.assertNotEqual(visible.body, hidden.body)
        self.assertIn("click it", visible.body)
        self.assertIn("Make room", hidden.body)

    def test_a_hidden_icon_on_the_picture_display_says_where_it_went(self) -> None:
        hidden = self.notice_for(windows=(ICON_ON_THE_VIEW,))
        self.assertIn("picture display", hidden.body)

    def test_no_icon_anywhere_does_not_claim_it_is_on_the_display(self) -> None:
        hidden = self.notice_for(windows=())
        self.assertIn("no menu bar icon", hidden.body)
        self.assertNotIn("picture display", hidden.body)

    def test_the_muted_message_says_nothing_needs_restarting(self) -> None:
        """`_write_ui_heartbeat` never latches its failure: it retries on
        every throttled tick and logs the transition back."""
        note = self.notice_for(
            heartbeat_at=STALE, state_writable=False, state_detail="Permission denied"
        )
        self.assertTrue(note.self_heals)
        self.assertIn("nothing needs restarting", note.body)
        self.assertIn("Permission denied", note.body)
        self.assertNotIn("force quit", note.body.lower())

    def test_only_muted_claims_to_self_heal(self) -> None:
        for verdict in cs.Verdict:
            with self.subTest(verdict.value):
                note = cs.notice(facts(), verdict)
                self.assertEqual(note.self_heals, verdict is cs.Verdict.MUTED)

    def test_the_unidentified_message_says_nothing_was_signalled(self) -> None:
        note = self.notice_for(holder_exe="/bin/sleep")
        self.assertIn("/bin/sleep", note.body)
        self.assertIn("Nothing has been signalled", note.body)
        self.assertNotIn("force quit", note.body.lower())

    def test_no_message_carries_markup(self) -> None:
        """These render into an `NSPanel` in Phase 4b, which shows
        asterisks and backticks literally."""
        for verdict in cs.Verdict:
            note = cs.notice(facts(heartbeat_at=STALE), verdict)
            with self.subTest(verdict.value):
                for markup in ("**", "`", "<b>"):
                    self.assertNotIn(markup, note.body)
                    self.assertNotIn(markup, note.headline)

    def test_no_message_names_a_screen_coordinate(self) -> None:
        """🔴 The rule 4b's brief added, enforced where the words live.

        `SERVING_VISIBLE` used to say "Its menu bar icon is on screen
        (34x24 at 1169, 0) — click it" and `WINDOW_OPEN` said the same of
        its window bounds. Honest, and useless: nobody turns a coordinate
        into a place to look.

        The geometry below is deliberately unmistakable, so the assertion
        fails on *any* route back to naming bounds — `where()`,
        an f-string over `WindowFact`, a helpfully reformatted "at x, y" —
        rather than only on the exact expression that was removed.
        """
        marked = cs.WindowFact(
            layer=25, on_screen=True, x=7654, y=8765, width=1357, height=2468
        )
        window = cs.WindowFact(
            layer=8, on_screen=True, x=7654, y=8765, width=1357, height=2468
        )
        for verdict in cs.Verdict:
            for f in (
                facts(windows=(marked,)),
                facts(heartbeat_at=STALE, windows=(window,)),
            ):
                note = cs.notice(f, verdict)
                text = f"{note.headline} {note.body}"
                for number in ("7654", "8765", "1357", "2468"):
                    with self.subTest(f"{verdict.value}/{number}"):
                        self.assertNotIn(number, text)

    def test_no_message_claims_to_be_near_a_window(self) -> None:
        """🔴 The other half of the same rule, and the reason the fix was
        not simply a better sentence: **these words have two consumers.**
        Phase 4b's panel, where "directly above this window" is true, and
        `<role>.stderr.log` plus `tools/status.py`, where there is no
        window and it is a lie.

        A wording true in one is false in the other, so this layer may
        only say what is true in both. The spatial pointer belongs to
        `notice_state.pointer_sentence`, which is owned by the layer that
        actually placed a window and withholds it unless it placed it
        where the sentence says.
        """
        for verdict in cs.Verdict:
            note = cs.notice(facts(heartbeat_at=STALE), verdict)
            text = f"{note.headline} {note.body}".lower()
            for claim in ("this window", "above this", "below this", "just above"):
                with self.subTest(f"{verdict.value}/{claim}"):
                    self.assertNotIn(claim, text)

    def test_the_visible_icon_message_still_tells_you_where_to_look(self) -> None:
        """Dropping the coordinate must not drop the instruction. What is
        left has to be true with no window on screen at all, because the
        log is a consumer of it."""
        note = self.notice_for()
        self.assertIs(note.verdict, cs.Verdict.SERVING_VISIBLE)
        self.assertIn("menu bar icon", note.body)
        self.assertIn("click it", note.body)

    def test_the_could_not_look_branch_still_hedges(self) -> None:
        """With no window list there is no "is on screen" to assert, and
        the two branches converged when the coordinates went. They must
        not collapse into one: "should be at the right-hand end" is a
        hedge, and "is on screen" is a claim."""
        looked = self.notice_for()
        blind = self.notice_for(windows=None)
        self.assertIs(blind.verdict, cs.Verdict.SERVING_VISIBLE)
        self.assertNotEqual(looked.body, blind.body)
        self.assertIn("should be", blind.body)

    def test_no_message_has_doubled_or_trailing_whitespace(self) -> None:
        for verdict in cs.Verdict:
            for capture in (
                cs.Capture(),
                cs.Capture(wanted=True, signalled=True, grew=True, path="/l.log"),
                cs.Capture(wanted=True, refused="it is too old to signal"),
            ):
                note = cs.notice(facts(heartbeat_at=STALE), verdict, capture)
                with self.subTest(verdict.value):
                    self.assertNotIn("  ", note.body)
                    self.assertEqual(note.body, note.body.strip())

    def test_a_never_written_stamp_does_not_print_a_negative_age(self) -> None:
        note = self.notice_for(heartbeat_at=0.0, windows=())
        self.assertIn("has never reported working", note.body)
        self.assertNotIn("-", note.body.split("force quit")[0])

    def test_a_future_stamp_reports_zero_rather_than_a_negative_age(self) -> None:
        self.assertEqual(facts(heartbeat_at=NOW + 99.0).age_s(), 0.0)


class StructuredPayloadTests(unittest.TestCase):
    """The 4a/4b seam: 4b renders these fields and decides nothing, and
    `tools/status.py` prints the same verdict without reimplementing
    it."""

    def test_the_notice_carries_the_evidence(self) -> None:
        f = facts(heartbeat_at=STALE, windows=(ICON_ON_MENU_BAR, AN_ALERT))
        note = cs.notice(f, cs.classify(f))
        self.assertEqual(note.holder_pid, 4242)
        self.assertEqual(note.display_pid, 4243)
        self.assertAlmostEqual(note.heartbeat_age_s, ms.UI_UNRESPONSIVE_AFTER_S + 1.0)
        self.assertEqual(note.window_layers, (8, 25))
        self.assertTrue(note.state_writable)

    def test_layers_seen_is_none_when_the_window_server_could_not_be_asked(self) -> None:
        """Not `()` — "could not look" is not "nothing there"."""
        f = facts(windows=None)
        self.assertIsNone(cs.notice(f, cs.classify(f)).window_layers)

    def test_layers_seen_is_empty_when_nothing_is_on_screen(self) -> None:
        f = facts(windows=())
        self.assertEqual(cs.notice(f, cs.classify(f)).window_layers, ())

    def test_the_stacks_path_appears_only_when_a_dump_actually_landed(self) -> None:
        f = facts(heartbeat_at=STALE, windows=())
        wanted = cs.Capture(wanted=True, signalled=True, grew=True, path="/l.log")
        missed = cs.Capture(wanted=True, signalled=True, path="/l.log")
        self.assertEqual(cs.notice(f, cs.classify(f), wanted).stacks_path, "/l.log")
        self.assertIsNone(cs.notice(f, cs.classify(f), missed).stacks_path)

    def test_the_log_line_names_the_verdict(self) -> None:
        f = facts()
        note = cs.notice(f, cs.classify(f))
        self.assertIn("serving_visible", note.log_line())


class CaptureSentenceTests(unittest.TestCase):
    def test_nothing_wanted_says_nothing(self) -> None:
        self.assertEqual(cs.Capture().sentence(), "")

    def test_a_landed_dump_names_the_file(self) -> None:
        got = cs.Capture(wanted=True, signalled=True, grew=True, path="/l.log").sentence()
        self.assertIn("/l.log", got)
        self.assertIn("captured", got)

    def test_a_signal_that_produced_nothing_says_so(self) -> None:
        got = cs.Capture(wanted=True, signalled=True, path="/l.log").sentence()
        self.assertIn("did not grow", got)

    def test_a_refusal_gives_the_reason(self) -> None:
        got = cs.Capture(wanted=True, refused="it is too old").sentence()
        self.assertIn("too old", got)

    def test_capture_is_only_ever_wanted_for_the_two_wedge_verdicts(self) -> None:
        self.assertEqual(
            cs.CAPTURE_STACKS_FOR, {cs.Verdict.UNRESPONSIVE, cs.Verdict.NEVER_ARRIVED}
        )


class LaunchSourceTests(unittest.TestCase):
    """Only a human launch gets a response — an agent respawn that loses
    the lock at login is routine and must not cost the holder a signal."""

    LABEL = "com.example.imageview.ui"

    def test_the_agents_own_label_is_the_agent(self) -> None:
        self.assertTrue(
            cs.launched_by_agent({"XPC_SERVICE_NAME": self.LABEL}, self.LABEL)
        )

    def test_a_launchservices_app_is_not_the_agent(self) -> None:
        self.assertFalse(
            cs.launched_by_agent(
                {"XPC_SERVICE_NAME": "application.com.example.imageview.1.2"},
                self.LABEL,
            )
        )

    def test_a_launchservices_value_prefixed_with_the_label_is_not_the_agent(self) -> None:
        """Exact match only. A prefix or substring test would catch
        `application.<bundleid>...`, which is the case that must not
        match."""
        self.assertFalse(
            cs.launched_by_agent(
                {"XPC_SERVICE_NAME": f"application.{self.LABEL}.0.1"}, self.LABEL
            )
        )
        self.assertFalse(
            cs.launched_by_agent({"XPC_SERVICE_NAME": self.LABEL + ".0"}, self.LABEL)
        )

    def test_an_absent_variable_is_not_the_agent(self) -> None:
        self.assertFalse(cs.launched_by_agent({}, self.LABEL))

    def test_a_terminal_descended_process_is_not_the_agent(self) -> None:
        """MEASURED on this machine 2026-09-10: a shell session has
        `XPC_SERVICE_NAME=0`, a third value the plan does not mention. An
        "is it set at all" test would have been wrong."""
        self.assertFalse(cs.launched_by_agent({"XPC_SERVICE_NAME": "0"}, self.LABEL))

    def test_the_display_agents_label_is_not_the_ui_agents(self) -> None:
        self.assertFalse(
            cs.launched_by_agent(
                {"XPC_SERVICE_NAME": "com.example.imageview.display"}, self.LABEL
            )
        )


if __name__ == "__main__":
    unittest.main()
