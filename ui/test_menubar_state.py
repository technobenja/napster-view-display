"""Tests for the menu bar's pure logic.

stdlib `unittest`, like every other test in this project. Nothing here
imports AppKit, so the whole file runs headless and in a fraction of a
second; the shell in `menubar.py` is verified by running it against a
real menu bar, which is the only thing that can verify it.
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from ui import menubar_state as ms


def status(**overrides) -> ms.Status:
    """A healthy `Status`, so each test overrides only the field it is
    actually about. Defaults deliberately invert `Status`'s own (which
    describe the worst case): a test for `Paused` should not have to
    remember to also set `view_connected`."""
    base = dict(
        heartbeat_at=1000.0,
        view_connected=True,
        image_count=10,
        blanked=False,
        paused=False,
        last_shown_id="abc",
        display_label="A picture",
        present=True,
    )
    base.update(overrides)
    return ms.Status(**base)


class TestStaleness(unittest.TestCase):
    def test_fresh_heartbeat_is_not_stale(self):
        self.assertFalse(ms.is_stale(1000.0, 1004.9))

    def test_boundary_is_not_stale(self):
        # "stale >5s", strictly greater.
        self.assertFalse(ms.is_stale(1000.0, 1005.0))

    def test_just_past_the_boundary_is_stale(self):
        self.assertTrue(ms.is_stale(1000.0, 1005.01))

    def test_missing_heartbeat_is_stale(self):
        self.assertTrue(ms.is_stale(0.0, 1000.0))

    def test_future_heartbeat_is_not_stale(self):
        # Clocks step backwards (NTP, a resumed VM). Calling a healthy
        # display dead and offering to start a second one is the worse
        # error.
        self.assertFalse(ms.is_stale(2000.0, 1000.0))


class TestParseStatus(unittest.TestCase):
    def test_non_mapping_is_absent(self):
        for bad in (None, [], "x", 3):
            with self.subTest(bad=bad):
                self.assertFalse(ms.parse_status(bad).present)

    def test_empty_dict_is_present_but_default(self):
        parsed = ms.parse_status({})
        self.assertTrue(parsed.present)
        self.assertEqual(parsed.heartbeat_at, 0.0)
        self.assertFalse(parsed.view_connected)

    def test_bool_is_not_a_heartbeat(self):
        # bool subclasses int; True must not read as 1.0 (i.e. 1970).
        self.assertEqual(ms.parse_status({"heartbeat_at": True}).heartbeat_at, 0.0)

    def test_nan_heartbeat_is_rejected(self):
        parsed = ms.parse_status(json.loads('{"heartbeat_at": NaN}'))
        self.assertEqual(parsed.heartbeat_at, 0.0)

    def test_infinite_heartbeat_is_rejected(self):
        parsed = ms.parse_status(json.loads('{"heartbeat_at": Infinity}'))
        self.assertEqual(parsed.heartbeat_at, 0.0)

    def test_one_bad_field_does_not_cost_the_others(self):
        parsed = ms.parse_status(
            {"heartbeat_at": 5.0, "image_count": "lots", "view_connected": True}
        )
        self.assertEqual(parsed.heartbeat_at, 5.0)
        self.assertTrue(parsed.view_connected)
        self.assertEqual(parsed.image_count, 0)

    def test_blank_last_shown_id_is_none(self):
        self.assertIsNone(ms.parse_status({"last_shown_id": "   "}).last_shown_id)

    def test_float_image_count_is_accepted(self):
        self.assertEqual(ms.parse_status({"image_count": 10.0}).image_count, 10)

    def test_unknown_keys_are_ignored(self):
        parsed = ms.parse_status({"heartbeat_at": 5.0, "invented_by_v2": {"a": 1}})
        self.assertEqual(parsed.heartbeat_at, 5.0)

    def test_a_real_live_status_document_parses(self):
        # Copied from the running display's status.json, so this fails if
        # app.py's writer and this reader ever drift apart.
        live = {
            "updated_at": 1784482483.138536,
            "calibration_source": "user",
            "started_at": 1784482416.028737,
            "last_shown_id": "e1b727dc-4620-41b4-8519-26f58127ee40",
            "last_shown_at": 1784482416.029682,
            "display_label": "Picture 1 of 10",
            "last_poll_ok": True,
            "last_poll_count": 16,
            "image_count": 16,
            "source_label": "Image Server",
            "last_error": None,
            "heartbeat_at": 1784482483.138238,
            "last_handled_advance": 0,
            "view_connected": True,
        }
        parsed = ms.parse_status(live)
        self.assertTrue(parsed.view_connected)
        self.assertEqual(parsed.image_count, 16)
        self.assertEqual(parsed.display_label, "Picture 1 of 10")
        self.assertIsNone(parsed.last_error)


class TestReadStatus(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_missing_file(self):
        self.assertFalse(ms.read_status(self.dir / "nope.json").present)

    def test_corrupt_file_never_raises(self):
        path = self.dir / "status.json"
        path.write_text("{half writ")
        self.assertFalse(ms.read_status(path).present)

    def test_directory_instead_of_file_never_raises(self):
        self.assertFalse(ms.read_status(self.dir).present)

    def test_good_file(self):
        path = self.dir / "status.json"
        path.write_text(json.dumps({"heartbeat_at": 12.0, "view_connected": True}))
        parsed = ms.read_status(path)
        self.assertTrue(parsed.present)
        self.assertEqual(parsed.heartbeat_at, 12.0)


class TestUiHeartbeat(unittest.TestCase):
    """The menu bar's own heartbeat — the mirror of the display's.

    Its whole purpose is to distinguish a menu bar that is *serving* from
    one that is merely alive and holding `ui.lock`, so the tests that
    matter are the ones about what happens when it cannot be read or
    written.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    # -- cadence ------------------------------------------------------
    #
    # Deliberately NOT here. The margin that matters is between
    # STALE_AFTER_S and the gap that actually reaches the disk, and that
    # gap depends on `menubar.POLL_INTERVAL_S` — importing which would put
    # AppKit into this file, which is the one thing it does not do. The
    # cadence assertions live in `ui/test_menubar.py` alongside the poll
    # interval they depend on. Stating it as
    # `STALE_AFTER_S >= 2 * UI_HEARTBEAT_INTERVAL_S`, as this file first
    # did, ignores poll granularity and is satisfied by an interval that
    # goes stale after ONE missed beat.

    # -- writing ------------------------------------------------------

    def test_a_written_heartbeat_reads_back(self):
        path = self.dir / "ui_status.json"
        self.assertTrue(ms.write_ui_heartbeat(path, 1234.5))
        self.assertEqual(ms.read_ui_heartbeat(path), 1234.5)

    def test_the_write_creates_the_state_directory(self):
        """`state_dir()` may not exist on a first run, and a heartbeat
        that cannot be written until something else has run first is a
        heartbeat with a startup blind spot."""
        path = self.dir / "state" / "ui_status.json"
        self.assertTrue(ms.write_ui_heartbeat(path, 7.0))
        self.assertEqual(ms.read_ui_heartbeat(path), 7.0)

    def test_the_write_carries_only_the_heartbeat(self):
        """One field, one writer. A merge would preserve keys this
        process did not write, which for a liveness file is the opposite
        of what is wanted."""
        path = self.dir / "ui_status.json"
        ms.write_ui_heartbeat(path, 5.0)
        self.assertEqual(json.loads(path.read_text()), {"ui_heartbeat_at": 5.0})

    def test_a_later_write_replaces_the_earlier_one(self):
        path = self.dir / "ui_status.json"
        ms.write_ui_heartbeat(path, 1.0)
        ms.write_ui_heartbeat(path, 2.0)
        self.assertEqual(ms.read_ui_heartbeat(path), 2.0)

    def test_an_unwritable_path_returns_false_and_never_raises(self):
        """The safety argument for putting this on a live timer. An
        exception off an NSTimer selector kills the run loop, and the
        failure mode of a raising heartbeat is losing the ability to stop
        the process it was meant to prove healthy."""
        path = self.dir / "ui_status.json"
        path.mkdir()  # a directory where the file should be
        self.assertFalse(ms.write_ui_heartbeat(path, 1.0))

    def test_an_unserializable_moment_returns_false_and_never_raises(self):
        """`OSError` is not the only way `atomic_write_json` can fail —
        `json.dumps` raises `TypeError` on a value it cannot encode.
        `merge_status` learned this the same way."""
        path = self.dir / "ui_status.json"
        self.assertFalse(ms.write_ui_heartbeat(path, object()))
        self.assertFalse(path.exists())

    def test_a_failed_write_leaves_the_previous_heartbeat_intact(self):
        """Atomic write-then-rename: a reader must never catch a
        half-written file and conclude a healthy process is dead."""
        path = self.dir / "ui_status.json"
        ms.write_ui_heartbeat(path, 99.0)
        self.assertFalse(ms.write_ui_heartbeat(path, object()))
        self.assertEqual(ms.read_ui_heartbeat(path), 99.0)

    def test_the_write_replaces_the_file_rather_than_rewriting_it(self):
        """Behavioural proxy for "this write is atomic". A temp file plus
        `os.replace` lands a *new* inode at the path; writing in place
        reuses the old one, and a reader that catches an in-place write
        half done reads 0.0 and reports a healthy process dead.

        Asserting on the inode rather than on `atomic_write_json` being
        called: pattern-presence is not behaviour, and swapping in a
        plain `write_text` is exactly the shortcut this guards."""
        path = self.dir / "ui_status.json"
        ms.write_ui_heartbeat(path, 1.0)
        first = path.stat().st_ino
        ms.write_ui_heartbeat(path, 2.0)
        self.assertNotEqual(path.stat().st_ino, first)

    def test_no_temp_files_are_left_behind(self):
        """Every 2 seconds, forever. A leaked temp file per beat would
        fill `state/` at ~43,000 files a day."""
        path = self.dir / "ui_status.json"
        for moment in range(10):
            ms.write_ui_heartbeat(path, float(moment))
        self.assertEqual([p.name for p in self.dir.iterdir()], ["ui_status.json"])

    def test_the_default_moment_is_now(self):
        before = time.time()
        path = self.dir / "ui_status.json"
        ms.write_ui_heartbeat(path)
        self.assertGreaterEqual(ms.read_ui_heartbeat(path), before)

    # -- reading ------------------------------------------------------

    def test_a_missing_file_is_stale(self):
        beat = ms.read_ui_heartbeat(self.dir / "nope.json")
        self.assertEqual(beat, 0.0)
        self.assertTrue(ms.is_stale(beat, now=0.0))

    def test_a_directory_never_raises(self):
        self.assertEqual(ms.read_ui_heartbeat(self.dir), 0.0)

    def test_a_half_written_file_is_stale_not_an_exception(self):
        path = self.dir / "ui_status.json"
        path.write_text('{"ui_heartbeat_at": 12')
        self.assertEqual(ms.read_ui_heartbeat(path), 0.0)

    def test_a_json_list_is_stale(self):
        path = self.dir / "ui_status.json"
        path.write_text("[1, 2, 3]")
        self.assertEqual(ms.read_ui_heartbeat(path), 0.0)

    def test_a_nan_heartbeat_is_rejected(self):
        """NaN fails every comparison, so a NaN heartbeat would read as
        `not stale` forever — the one answer that hides a wedge
        permanently. Shared with `parse_status` via `_as_float`."""
        path = self.dir / "ui_status.json"
        path.write_text('{"ui_heartbeat_at": NaN}')
        beat = ms.read_ui_heartbeat(path)
        self.assertEqual(beat, 0.0)
        self.assertTrue(ms.is_stale(beat, now=1e9))

    def test_a_boolean_is_not_a_timestamp(self):
        path = self.dir / "ui_status.json"
        path.write_text('{"ui_heartbeat_at": true}')
        self.assertEqual(ms.read_ui_heartbeat(path), 0.0)

    def test_the_display_field_name_is_not_accepted_here(self):
        """`ui_status.json` and `status.json` are different files with
        different owners. Reading the display's field name out of the
        UI's file would make a running display look like a running menu
        bar — exactly the confusion this whole mechanism exists to
        prevent."""
        path = self.dir / "ui_status.json"
        path.write_text(json.dumps({"heartbeat_at": 500.0}))
        self.assertEqual(ms.read_ui_heartbeat(path), 0.0)

    # -- the incident -------------------------------------------------

    def test_a_stopped_process_goes_stale_while_its_last_beat_persists(self):
        """The shape of 2026-09-08: the process is still alive, the file
        it wrote is still on disk and still readable, and the lock is
        still held. Only the *age* of the heartbeat can say that nothing
        is being served."""
        path = self.dir / "ui_status.json"
        ms.write_ui_heartbeat(path, 1000.0)
        beat = ms.read_ui_heartbeat(path)
        self.assertFalse(ms.is_stale(beat, now=1000.0 + ms.STALE_AFTER_S))
        self.assertTrue(ms.is_stale(beat, now=1000.0 + ms.STALE_AFTER_S + 0.01))


class TestStatePrecedence(unittest.TestCase):
    """Order, one assertion per rung plus one per boundary.

    Each "outranks" test sets *both* conditions and asserts the higher
    one wins — the ordering is the thing being tested, so a test that
    only sets one condition would pass under any order.
    """

    NOW = 1000.0

    def evaluate(self, st, needs_setup=False):
        return ms.evaluate_state(st, now=self.NOW, needs_setup=needs_setup)

    def test_healthy_is_normal(self):
        self.assertIs(self.evaluate(status()), ms.State.NORMAL)

    def test_normal_shows_no_text(self):
        self.assertEqual(ms.State.NORMAL.title, "")
        self.assertFalse(ms.State.NORMAL.is_actionable)

    def test_stale_heartbeat_is_not_showing(self):
        self.assertIs(
            self.evaluate(status(heartbeat_at=900.0)), ms.State.NOT_SHOWING
        )

    def test_not_showing_outranks_view_not_connected(self):
        # stated reason: a dead heartbeat means nothing else in
        # status.json can be trusted, so a stale connectivity string
        # would actively mislead.
        st = status(heartbeat_at=900.0, view_connected=False)
        self.assertIs(self.evaluate(st), ms.State.NOT_SHOWING)

    def test_not_showing_outranks_everything_else(self):
        st = status(
            heartbeat_at=0.0,
            view_connected=False,
            image_count=0,
            blanked=True,
            paused=True,
        )
        self.assertIs(self.evaluate(st, needs_setup=True), ms.State.NOT_SHOWING)

    def test_view_not_connected_outranks_setup_needed(self):
        st = status(view_connected=False)
        self.assertIs(
            self.evaluate(st, needs_setup=True), ms.State.VIEW_NOT_CONNECTED
        )

    def test_view_not_connected_outranks_blanked_and_paused(self):
        st = status(view_connected=False, blanked=True, paused=True)
        self.assertIs(self.evaluate(st), ms.State.VIEW_NOT_CONNECTED)

    def test_setup_needed_outranks_no_pictures(self):
        st = status(image_count=0)
        self.assertIs(self.evaluate(st, needs_setup=True), ms.State.SETUP_NEEDED)

    def test_no_pictures_outranks_blanked(self):
        st = status(image_count=0, blanked=True)
        self.assertIs(self.evaluate(st), ms.State.NO_PICTURES)

    def test_blanked_outranks_paused(self):
        st = status(blanked=True, paused=True)
        self.assertIs(self.evaluate(st), ms.State.BLANKED)

    def test_paused_alone(self):
        self.assertIs(self.evaluate(status(paused=True)), ms.State.PAUSED)

    def test_a_failed_poll_stays_silent(self):
        # "A failed poll or a skipped corrupt file stays silent — a
        # 3am network hiccup must not leave text in the menu bar all
        # morning." last_error is carried for the settings window and is
        # deliberately not a title state.
        st = status(last_error="Couldn't reach the source.")
        self.assertIs(self.evaluate(st), ms.State.NORMAL)

    def test_every_actionable_state_has_a_title(self):
        for state in ms.State:
            if state is ms.State.NORMAL:
                continue
            with self.subTest(state=state):
                self.assertTrue(state.title)
                self.assertTrue(state.is_actionable)

    def test_titles_match_the_plan_exactly(self):
        self.assertEqual(
            [s.title for s in ms.State],
            [
                "",
                "Not showing pictures",
                "View not connected",
                "Setup needed",
                "No pictures",
                "Blanked",
                "Paused",
            ],
        )

    def test_absent_status_file_reads_as_not_showing(self):
        self.assertIs(self.evaluate(ms.Status()), ms.State.NOT_SHOWING)


class TestSetupNeeded(unittest.TestCase):
    def test_no_settings_at_all(self):
        self.assertTrue(ms.setup_needed(None))
        self.assertTrue(ms.setup_needed({}))

    def test_explicit_source_block(self):
        self.assertFalse(ms.setup_needed({"source": {"kind": "folder"}}))

    def test_legacy_flat_key_counts_as_configured(self):
        # Step 0's migration reads this; a user who configured the app
        # before the source block is not "not set up".
        self.assertFalse(
            ms.setup_needed({"image_studio_base_url": "http://example.test"})
        )

    def test_unrelated_settings_still_need_setup(self):
        self.assertTrue(ms.setup_needed({"rotation_interval_s": 900}))

    def test_source_of_the_wrong_type(self):
        self.assertTrue(ms.setup_needed({"source": "folder"}))


class TestTruncateLabel(unittest.TestCase):
    def test_short_label_is_untouched(self):
        self.assertEqual(ms.truncate_label("Sunset"), "Sunset")

    def test_exactly_at_the_limit(self):
        text = "x" * ms.LABEL_MAX_CHARS
        self.assertEqual(ms.truncate_label(text), text)

    def test_result_never_exceeds_the_limit(self):
        for length in range(0, 80):
            with self.subTest(length=length):
                out = ms.truncate_label("y" * length)
                self.assertLessEqual(len(out), ms.LABEL_MAX_CHARS)

    def test_truncation_is_ellipsised(self):
        out = ms.truncate_label("z" * 100)
        self.assertTrue(out.endswith(ms.ELLIPSIS))
        self.assertEqual(len(out), ms.LABEL_MAX_CHARS)

    def test_whitespace_is_collapsed(self):
        self.assertEqual(ms.truncate_label("a\n b\tc  d"), "a b c d")

    def test_trailing_space_before_ellipsis_is_dropped(self):
        out = ms.truncate_label("word " + "q" * 40, limit=6)
        self.assertEqual(out, "word" + ms.ELLIPSIS)

    def test_limit_of_one(self):
        self.assertEqual(ms.truncate_label("abcdef", limit=1), ms.ELLIPSIS)

    def test_zero_limit(self):
        self.assertEqual(ms.truncate_label("abcdef", limit=0), "")

    def test_empty_input(self):
        self.assertEqual(ms.truncate_label(""), "")


class TestTitleTracker(unittest.TestCase):
    """The label is driven by *observing* `last_shown_id` change."""

    def test_first_observation_is_not_a_change(self):
        # The UI launching while a picture has been up for an hour must
        # not announce it as new.
        tracker = ms.TitleTracker()
        self.assertFalse(tracker.observe("a", "Alpha", now=0.0))
        self.assertEqual(tracker.active_label(0.1), "")

    def test_a_change_starts_a_hold(self):
        tracker = ms.TitleTracker()
        tracker.observe("a", "Alpha", now=0.0)
        self.assertTrue(tracker.observe("b", "Bravo", now=1.0))
        self.assertEqual(tracker.active_label(1.0), "Bravo")

    def test_the_hold_expires(self):
        tracker = ms.TitleTracker(hold_s=3.0)
        tracker.observe("a", "Alpha", now=0.0)
        tracker.observe("b", "Bravo", now=1.0)
        self.assertEqual(tracker.active_label(3.9), "Bravo")
        self.assertEqual(tracker.active_label(4.1), "")

    def test_repeated_observation_of_the_same_id_does_nothing(self):
        # The UI polls at 2.5Hz; the same picture is observed dozens of
        # times per hold and must not re-arm it.
        tracker = ms.TitleTracker(hold_s=3.0)
        tracker.observe("a", "Alpha", now=0.0)
        tracker.observe("b", "Bravo", now=1.0)
        for tick in range(2, 20):
            self.assertFalse(tracker.observe("b", "Bravo", now=float(tick)))
        self.assertEqual(tracker.active_label(5.0), "")

    def test_three_fast_clicks_collapsed_by_the_display_show_one_label(self):
        # advance counter collapses three clicks into one apply,
        # so the UI observes exactly one id change and shows exactly one
        # name -- stated reason for observing rather than reacting
        # to the click.
        tracker = ms.TitleTracker()
        tracker.observe("a", "Alpha", now=0.0)
        starts = [
            tracker.observe("d", "Delta", now=0.4),
            tracker.observe("d", "Delta", now=0.8),
            tracker.observe("d", "Delta", now=1.2),
        ]
        self.assertEqual(starts, [True, False, False])

    def test_a_wedged_agent_shows_nothing(self):
        # No id change -> no confirmation, which is the whole point.
        tracker = ms.TitleTracker()
        tracker.observe("a", "Alpha", now=0.0)
        for tick in range(1, 30):
            tracker.observe("a", "Alpha", now=float(tick))
        self.assertEqual(tracker.active_label(30.0), "")

    def test_none_id_is_not_a_change(self):
        tracker = ms.TitleTracker()
        tracker.observe("a", "Alpha", now=0.0)
        self.assertFalse(tracker.observe(None, "", now=1.0))

    def test_an_id_change_with_no_label_shows_nothing(self):
        # A blank rectangle in the menu bar for three seconds is worse
        # than no feedback.
        tracker = ms.TitleTracker()
        tracker.observe("a", "Alpha", now=0.0)
        self.assertFalse(tracker.observe("b", "   ", now=1.0))
        self.assertEqual(tracker.active_label(1.0), "")

    def test_long_labels_are_truncated_on_the_way_in(self):
        tracker = ms.TitleTracker()
        tracker.observe("a", "Alpha", now=0.0)
        tracker.observe("b", "B" * 100, now=1.0)
        self.assertEqual(len(tracker.active_label(1.0)), ms.LABEL_MAX_CHARS)

    def test_returning_to_a_previous_id_is_a_change(self):
        # Previous goes back to a picture already seen; that is still a
        # change worth naming.
        tracker = ms.TitleTracker()
        tracker.observe("a", "Alpha", now=0.0)
        tracker.observe("b", "Bravo", now=1.0)
        self.assertTrue(tracker.observe("a", "Alpha", now=2.0))


class TestTitleFor(unittest.TestCase):
    def test_normal_shows_the_transient_label(self):
        self.assertEqual(ms.title_for(ms.State.NORMAL, "Sunset"), "Sunset")

    def test_normal_with_no_label_shows_nothing(self):
        self.assertEqual(ms.title_for(ms.State.NORMAL, ""), "")

    def test_an_actionable_state_beats_the_label(self):
        self.assertEqual(
            ms.title_for(ms.State.VIEW_NOT_CONNECTED, "Sunset"),
            "View not connected",
        )

    def test_paused_beats_the_label(self):
        self.assertEqual(ms.title_for(ms.State.PAUSED, "Sunset"), "Paused")


class TestCommandConstruction(unittest.TestCase):
    """Desired state for blank/pause, a monotonic counter for
    Next/Previous."""

    def test_blank_from_nothing(self):
        out = ms.command_set_blanked(None, True)
        self.assertIs(out["blanked"], True)
        self.assertEqual(out["advance"], 0)

    def test_unblank_writes_false_not_none(self):
        # None means "follow the schedule" (blank_manual). There
        # is no schedule yet, so writing None would mean something
        # different the moment the settings UI lands.
        out = ms.command_set_blanked({"blanked": True}, False)
        self.assertIs(out["blanked"], False)

    def test_blank_preserves_the_counter(self):
        out = ms.command_set_blanked({"advance": 41}, True)
        self.assertEqual(out["advance"], 41)

    def test_blank_preserves_pause(self):
        out = ms.command_set_blanked({"paused": True, "paused_on_id": "x"}, True)
        self.assertTrue(out["paused"])
        self.assertEqual(out["paused_on_id"], "x")

    def test_pause_records_the_pinned_picture(self):
        out = ms.command_set_paused({}, True, "abc")
        self.assertTrue(out["paused"])
        self.assertEqual(out["paused_on_id"], "abc")

    def test_resume_clears_the_pin(self):
        out = ms.command_set_paused(
            {"paused": True, "paused_on_id": "abc"}, False
        )
        self.assertFalse(out["paused"])
        self.assertIsNone(out["paused_on_id"])

    def test_pause_does_not_unblank(self):
        # "Any action un-blanks, except Pause."
        out = ms.command_set_paused({"blanked": True}, True, "abc")
        self.assertIs(out["blanked"], True)

    def test_next_increments(self):
        self.assertEqual(ms.command_advance({"advance": 41}, 1)["advance"], 42)

    def test_previous_decrements(self):
        self.assertEqual(ms.command_advance({"advance": 41}, -1)["advance"], 40)

    def test_previous_below_zero_is_allowed(self):
        # control.py compares `!=`, not `>`, precisely so a negative or
        # reset counter is not a wedged channel.
        self.assertEqual(ms.command_advance({"advance": 0}, -1)["advance"], -1)

    def test_two_fast_clicks_move_the_counter_by_two(self):
        first = ms.command_advance({"advance": 0}, 1)
        second = ms.command_advance(first, 1)
        self.assertEqual(second["advance"], 2)

    def test_next_unblanks(self):
        out = ms.command_advance({"blanked": True, "advance": 0}, 1)
        self.assertIs(out["blanked"], False)

    def test_next_does_not_resume(self):
        # While paused, Next moves the pause to the next picture
        # and never resumes rotation. Only `Resume rotation` does that.
        out = ms.command_advance({"paused": True, "advance": 0}, 1)
        self.assertTrue(out["paused"])

    def test_a_corrupt_counter_does_not_raise(self):
        self.assertEqual(ms.command_advance({"advance": "many"}, 1)["advance"], 1)

    def test_unknown_keys_in_the_existing_file_are_dropped(self):
        # The writer emits exactly control.py's allow-list; anything else
        # in the file was not written by a build that understands it.
        out = ms.command_advance({"advance": 1, "invented": True}, 1)
        self.assertNotIn("invented", out)

    def test_every_builder_emits_the_full_schema(self):
        expected = {
            "blanked",
            "paused",
            "paused_on_id",
            "advance",
            "refresh",
            "preview_calibration",
        }
        for out in (
            ms.command_set_blanked(None, True),
            ms.command_set_paused(None, True, "a"),
            ms.command_advance(None, 1),
            ms.command_refresh(None),
        ):
            with self.subTest(out=out):
                self.assertEqual(set(out), expected)

    def test_refresh_increments_rather_than_setting(self):
        """A counter, not a flag: a flag cannot distinguish "asked again"
        from "still asking"."""
        first = ms.command_refresh(None)
        self.assertEqual(first["refresh"], 1)
        self.assertEqual(ms.command_refresh(first)["refresh"], 2)

    def test_refresh_un_blanks_but_does_not_un_pause(self):
        """Any action un-blanks, except Pause. New pictures
        arriving is not a reason to move off a pinned one."""
        out = ms.command_refresh({"blanked": True, "paused": True, "paused_on_id": "a"})
        self.assertFalse(out["blanked"])
        self.assertTrue(out["paused"])
        self.assertEqual(out["paused_on_id"], "a")

    def test_refresh_survives_a_junk_counter(self):
        for junk in (None, "3", True, [], {}):
            with self.subTest(junk=junk):
                self.assertEqual(ms.command_refresh({"refresh": junk})["refresh"], 1)

    def test_refresh_does_not_disturb_advance(self):
        out = ms.command_refresh({"advance": 7})
        self.assertEqual(out["advance"], 7)

    def test_preview_calibration_is_never_disturbed(self):
        # Step 3 owns this field. A Next pressed mid-calibration must not
        # cancel the live preview.
        preview = {"center_x": 1.0}
        for out in (
            ms.command_set_blanked({"preview_calibration": preview}, True),
            ms.command_set_paused({"preview_calibration": preview}, True, "a"),
            ms.command_advance({"preview_calibration": preview}, 1),
        ):
            with self.subTest(out=out):
                self.assertEqual(out["preview_calibration"], preview)


class TestRoundTripThroughControl(unittest.TestCase):
    """The builders' output has to survive `control.parse_control`, which
    is what the display actually reads. A field this module emits that
    the display's allow-list drops would be a silent no-op."""

    def test_blank_survives_the_display_side_parser(self):
        from display import control

        state = control.parse_control(ms.command_set_blanked(None, True))
        self.assertTrue(state.effective_blanked())

    def test_pause_survives_the_display_side_parser(self):
        from display import control

        state = control.parse_control(ms.command_set_paused(None, True, "abc"))
        self.assertTrue(state.paused)
        self.assertEqual(state.paused_on_id, "abc")

    def test_advance_survives_the_display_side_parser(self):
        from display import control

        state = control.parse_control(ms.command_advance({"advance": 7}, 1))
        self.assertEqual(state.advance, 8)

    def test_no_key_is_outside_the_display_allow_list(self):
        from display import control

        for out in (
            ms.command_set_blanked(None, True),
            ms.command_set_paused(None, True, "a"),
            ms.command_advance(None, 1),
        ):
            with self.subTest(out=out):
                self.assertFalse(set(out) - control.ALLOWED_KEYS)


class UiStatusRecordTests(unittest.TestCase):
    """The four process fields, and the types they must refuse.

    `read_ui_status` parses a document written by *some* version of this
    app — possibly an older one, possibly a newer one, possibly one a
    user edited by hand. The fields drive a message that names a pid to
    force quit and a decision about whether to send a signal whose
    default disposition is terminate, so a hostile value must degrade to
    "the document did not say", never to a plausible-looking number.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "ui_status.json"

    def write(self, document: object) -> ms.UiStatus:
        self.path.write_text(json.dumps(document))
        return ms.read_ui_status(self.path)

    def test_the_full_record_round_trips(self) -> None:
        ms.write_ui_heartbeat(
            self.path,
            1000.0,
            pid=4242,
            exe="/Applications/ImageView.app/Contents/MacOS/ImageView",
            stacks_armed=True,
            stacks_path="/logs/ui.stacks.log",
        )
        got = ms.read_ui_status(self.path)
        self.assertEqual(got.heartbeat_at, 1000.0)
        self.assertEqual(got.pid, 4242)
        self.assertEqual(got.exe, "/Applications/ImageView.app/Contents/MacOS/ImageView")
        self.assertTrue(got.stacks_armed)
        self.assertEqual(got.stacks_path, "/logs/ui.stacks.log")
        self.assertTrue(got.present)

    def test_a_v115_document_reads_with_defaults(self) -> None:
        """Backwards: a stamp-only file is what every v1.1.5 menu bar
        wrote, and it must not read as an impostor or as armed."""
        got = self.write({"ui_heartbeat_at": 7.0})
        self.assertEqual(got.heartbeat_at, 7.0)
        self.assertIsNone(got.pid)
        self.assertIsNone(got.exe)
        self.assertFalse(got.stacks_armed)
        self.assertIsNone(got.stacks_path)
        self.assertTrue(got.present)

    def test_unknown_keys_are_ignored_rather_than_rejected(self) -> None:
        """Forwards: a newer version may add fields this reader has never
        heard of, and the stamp must still be readable."""
        got = self.write({"ui_heartbeat_at": 7.0, "something_new": {"a": 1}})
        self.assertEqual(got.heartbeat_at, 7.0)
        self.assertTrue(got.present)

    def test_a_boolean_pid_is_not_pid_one(self) -> None:
        """🔴 Caught by mutation. `True` is an `int` subclass in Python,
        so a bare `int(...)` turns `{"pid": true}` into **pid 1** — and
        pid 1 is `launchd`. A message naming it, or a signal sent to it,
        is the worst possible outcome of a hostile value in a state
        file."""
        self.assertIsNone(self.write({"ui_heartbeat_at": 1.0, "pid": True}).pid)
        self.assertIsNone(self.write({"ui_heartbeat_at": 1.0, "pid": False}).pid)

    def test_a_string_pid_is_refused_rather_than_raising(self) -> None:
        self.assertIsNone(self.write({"ui_heartbeat_at": 1.0, "pid": "7"}).pid)
        self.assertIsNone(self.write({"ui_heartbeat_at": 1.0, "pid": "seven"}).pid)

    def test_a_nonpositive_pid_is_refused(self) -> None:
        """0 and negatives are `kill(2)`'s process-*group* selectors: 0
        is "every process in my group", -1 is "every process I may
        signal". Neither is a pid, and neither may travel onwards."""
        for value in (0, -1, -4242):
            with self.subTest(value=value):
                self.assertIsNone(self.write({"ui_heartbeat_at": 1.0, "pid": value}).pid)

    def test_a_container_pid_is_refused(self) -> None:
        for value in ([7], {"pid": 7}, None):
            with self.subTest(value=value):
                self.assertIsNone(self.write({"ui_heartbeat_at": 1.0, "pid": value}).pid)

    def test_a_hostile_stacks_armed_is_not_true(self) -> None:
        """The field that decides whether SIGUSR1 is a read or a kill.
        Anything that is not a real `true` must read as False."""
        for value in ("true", 1, "yes", [1], None):
            with self.subTest(value=value):
                got = self.write({"ui_heartbeat_at": 1.0, "stacks_armed": value})
                self.assertFalse(got.stacks_armed)

    def test_a_hostile_exe_or_path_is_refused(self) -> None:
        got = self.write({"ui_heartbeat_at": 1.0, "exe": 7, "stacks_path": ["/a"]})
        self.assertIsNone(got.exe)
        self.assertIsNone(got.stacks_path)

    def test_an_absent_file_is_not_present(self) -> None:
        got = ms.read_ui_status(self.path.parent / "nope.json")
        self.assertFalse(got.present)
        self.assertEqual(got.heartbeat_at, 0.0)

    def test_a_non_object_document_is_not_present(self) -> None:
        self.assertFalse(self.write([1, 2, 3]).present)
        self.assertFalse(self.write("hello").present)

    def test_read_ui_status_never_raises(self) -> None:
        """🔴 The contract, tested rather than read.

        Both readers say **never raises** in bold, and both did: on
        invalid UTF-8 (`UnicodeDecodeError` is a `ValueError`, not an
        `OSError`, so it went straight through the handler written to
        make them total) and on a non-finite JSON number reaching
        `int()`. A table of hostile files is cheap and would have caught
        both. Test the contract, not the docstring.
        """
        cases: list[tuple[str, object]] = [
            ("absent", None),
            ("empty", b""),
            ("invalid utf-8", b'{"ui_heartbeat_at": 1.0, "exe": "\xff\xfe"}'),
            ("a lone continuation byte", b"\x80"),
            ("truncated mid-write", b'{"ui_heartbeat_at": 12'),
            ("not an object", b"[1, 2, 3]"),
            ("not json at all", b"\x00\x01\x02binary"),
            ("NaN pid", b'{"pid": NaN}'),
            ("infinite pid", b'{"pid": 1e400}'),
            ("negative infinite pid", b'{"pid": -1e400}'),
            ("NaN stamp", b'{"ui_heartbeat_at": NaN}'),
            ("infinite stamp", b'{"ui_heartbeat_at": 1e400}'),
            ("boolean everywhere", b'{"pid": true, "stacks_armed": 1, "exe": 7}'),
            ("deeply nested", b'{"pid": ' + b"[" * 200 + b"]" * 200 + b"}"),
            ("a large blob", b'{"exe": "' + b"x" * 200_000 + b'"}'),
        ]
        for label, payload in cases:
            with self.subTest(label):
                target = self.path
                if payload is None:
                    target = self.path.parent / "definitely-absent.json"
                else:
                    target.write_bytes(payload)
                got = ms.read_ui_status(target)
                self.assertIsInstance(got, ms.UiStatus)
                self.assertIsInstance(ms.read_ui_heartbeat(target), float)
                # Whatever came back must be safe to act on: a pid is a
                # positive int or nothing, and nothing else is a pid.
                self.assertTrue(got.pid is None or got.pid > 0, f"{label}: {got.pid}")
                self.assertIsInstance(got.stacks_armed, bool)

    def test_read_ui_status_never_raises_on_an_unreadable_path(self) -> None:
        """A directory where the file should be, and a symlink loop:
        both are `OSError`, which the existing handler covers — pinned so
        that widening the decode fix never narrows this."""
        directory = self.path.parent / "adirectory"
        directory.mkdir()
        self.assertFalse(ms.read_ui_status(directory).present)

        loop = self.path.parent / "loop.json"
        loop.symlink_to(loop)
        self.assertFalse(ms.read_ui_status(loop).present)

    def test_read_status_never_raises_on_invalid_utf8_either(self) -> None:
        """`status.json` is written by the other process and read by the
        menu title on every 0.4s tick. Same helper, same guarantee."""
        self.path.write_bytes(b'{"heartbeat_at": 1.0, "source_label": "\xff"}')
        self.assertTrue(ms.read_status(self.path).present)

    def test_read_ui_heartbeat_is_the_records_own_field(self) -> None:
        """One parser, not two. A second code path answering the same
        question does not fail loudly when it drifts."""
        ms.write_ui_heartbeat(self.path, 55.0, pid=9)
        self.assertEqual(
            ms.read_ui_heartbeat(self.path), ms.read_ui_status(self.path).heartbeat_at
        )


class AboutTextTests(unittest.TestCase):
    """The About box states the version, so it is the one window whose
    whole job is a fact that can drift."""

    def test_it_names_both_authors(self):
        _title, body = ms.about_text("1.1.2")
        for author in ms.AUTHORS:
            self.assertIn(author, body)

    def test_the_title_carries_the_version_it_was_given(self):
        title, _body = ms.about_text("1.1.2")
        self.assertEqual(title, "ImageView 1.1.2")

    def test_no_version_is_hardcoded_anywhere_in_the_text(self):
        """🔴 The point of passing the version in. If a release number
        were baked in here it would be a fourth place the version lives,
        and the one users actually read — while the release gate asserts
        setup.py and the git tag agree precisely so that cannot happen."""
        import re

        title, body = ms.about_text("9.9.9")
        self.assertIn("9.9.9", title)
        self.assertEqual(re.findall(r"\d+\.\d+\.\d+", body), [])

    def test_an_unreadable_version_degrades_rather_than_raising(self):
        """Run from source rather than a bundle, the Info.plist key is
        absent. An About box is never worth a crash."""
        for bad in (None, "", "   ", 17, object()):
            with self.subTest(version=bad):
                title, body = ms.about_text(bad)
                self.assertIn("unknown version", title)
                self.assertTrue(body.strip())

    def test_it_keeps_the_trademark_disclaimer(self):
        """This app drives someone else's hardware and says so everywhere
        else it is described; the About box must not be the one place the
        claim goes missing."""
        _title, body = ms.about_text("1.1.2")
        self.assertIn("Not affiliated", body)
        self.assertIn("trademarks", body)


if __name__ == "__main__":
    unittest.main()
