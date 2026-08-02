"""Tests for the menu bar's pure logic.

stdlib `unittest`, like every other test in this project. Nothing here
imports AppKit, so the whole file runs headless and in a fraction of a
second; the shell in `menubar.py` is verified by running it against a
real menu bar, which is the only thing that can verify it.
"""

from __future__ import annotations

import json
import tempfile
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


if __name__ == "__main__":
    unittest.main()


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
