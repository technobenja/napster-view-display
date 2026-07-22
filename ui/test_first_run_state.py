"""First-run sequencing, tested without AppKit.

Several of these assert exact strings. That copy is specified
verbatim, so the strings *are* the requirement — a test that accepted any
message containing "ring" would not be testing what was specified.
"""

from __future__ import annotations

import unicodedata
import unittest

from display import blank_schedule, source_settings
from display.config_store import read_json_object
from display.blank_schedule import BlankSchedule
from display.source_settings import SourceSettings
from ui import first_run_state as fr
from ui import settings_state as ss


def _ok(count: int = 12) -> ss.TestResult:
    """A Test result that passes the gate."""
    return ss.TestResult(outcome=ss.Outcome.OK, message=f"{count} pictures found", count=count)


def _empty() -> ss.TestResult:
    return ss.TestResult(
        outcome=ss.Outcome.EMPTY_FOLDER, message="No pictures in this folder", count=0
    )


def _unreachable() -> ss.TestResult:
    return ss.TestResult(outcome=ss.Outcome.UNREACHABLE, message="Could not reach it")


def _flow_at_pictures(result: ss.TestResult | None = None) -> fr.FirstRunFlow:
    flow = fr.FirstRunFlow()
    flow.advance()
    assert flow.step is fr.Step.PICTURES
    if result is not None:
        flow.record_test(result)
    return flow


class StepOrderTests(unittest.TestCase):
    """"New order: pick display -> choose pictures -> calibrate.\""""

    def test_the_order_is_display_pictures_confirm(self) -> None:
        self.assertEqual(
            fr.STEP_ORDER, (fr.Step.DISPLAY, fr.Step.PICTURES, fr.Step.CONFIRM)
        )

    def test_calibration_is_not_first(self) -> None:
        """The whole reason for the reorder: calibrating first asks for
        fine-motor alignment against an empty black circle."""
        self.assertIsNot(fr.STEP_ORDER[0], fr.Step.CONFIRM)

    def test_pictures_comes_before_confirm(self) -> None:
        """So the confirm step has real content behind it."""
        self.assertLess(
            fr.STEP_ORDER.index(fr.Step.PICTURES),
            fr.STEP_ORDER.index(fr.Step.CONFIRM),
        )

    def test_a_new_flow_starts_on_the_first_step(self) -> None:
        self.assertIs(fr.FirstRunFlow().step, fr.Step.DISPLAY)

    def test_progress_labels(self) -> None:
        self.assertEqual(fr.progress_label(fr.Step.DISPLAY), "Step 1 of 3")
        self.assertEqual(fr.progress_label(fr.Step.PICTURES), "Step 2 of 3")
        self.assertEqual(fr.progress_label(fr.Step.CONFIRM), "Step 3 of 3")

    def test_every_step_has_a_title_and_a_body(self) -> None:
        for step in fr.STEP_ORDER:
            with self.subTest(step=step):
                self.assertTrue(fr.STEP_TITLES[step].strip())
                self.assertTrue(fr.body_for(step).strip())


class DisplayStepTests(unittest.TestCase):
    """"Display picking never blocks.\""""

    def test_next_is_enabled_with_no_display_chosen(self) -> None:
        flow = fr.FirstRunFlow()
        self.assertTrue(flow.can_advance)
        self.assertEqual(flow.blocked_note, "")

    def test_advancing_without_choosing_works(self) -> None:
        flow = fr.FirstRunFlow()
        self.assertTrue(flow.advance())
        self.assertIs(flow.step, fr.Step.PICTURES)

    def test_choosing_nothing_leaves_the_heuristic_in_place(self) -> None:
        self.assertEqual(fr.FirstRunFlow().chosen_display_id, "")

    def test_the_step_says_what_happens_if_you_do_not_pick(self) -> None:
        self.assertIn("960 x 960", fr.DISPLAY_SKIP_NOTE)

    def test_check_again_and_identify_are_offered(self) -> None:
        self.assertEqual(fr.DISPLAY_CHECK_AGAIN, "Check again")
        self.assertEqual(fr.DISPLAY_IDENTIFY, "Identify")

    def test_the_picker_itself_is_step_fours_and_is_not_reimplemented(self) -> None:
        """The flow reuses `display_options`; it does not filter. A
        display that matches nothing must still be listed."""
        options = ss.display_options(
            [
                {"name": "Built-in", "width": 2560, "height": 1440, "is_main": True},
                {"name": "Dell", "width": 1920, "height": 1080},
            ]
        )
        self.assertEqual(len(options), 2)
        self.assertTrue(ss.nothing_matched_note().strip())


class PicturesGateTests(unittest.TestCase):
    """Validate at pick time; "Save disabled on zero." The same
    rule gates Next."""

    def test_next_is_blocked_before_any_test(self) -> None:
        flow = _flow_at_pictures()
        self.assertFalse(flow.can_advance)
        self.assertEqual(flow.blocked_note, fr.PICTURES_BLOCKED_NOTE)
        self.assertFalse(flow.advance())
        self.assertIs(flow.step, fr.Step.PICTURES)

    def test_a_good_test_opens_the_gate(self) -> None:
        flow = _flow_at_pictures(_ok())
        self.assertTrue(flow.can_advance)
        self.assertTrue(flow.advance())
        self.assertIs(flow.step, fr.Step.CONFIRM)

    def test_an_empty_folder_does_not_open_the_gate(self) -> None:
        """Reached it, and it was empty — blocking, per `save_enabled`."""
        flow = _flow_at_pictures(_empty())
        self.assertFalse(flow.can_advance)
        self.assertFalse(flow.advance())

    def test_an_unreachable_source_does_not_open_the_gate(self) -> None:
        flow = _flow_at_pictures(_unreachable())
        self.assertFalse(flow.can_advance)

    def test_editing_the_source_drops_a_previous_pass(self) -> None:
        """A user who tests a working URL, edits it to a broken one, and
        presses Next must not walk past the gate on a stale pass."""
        flow = _flow_at_pictures(_ok())
        self.assertTrue(flow.can_advance)
        flow.source_edited()
        self.assertFalse(flow.can_advance)
        self.assertFalse(flow.advance())


class BackTests(unittest.TestCase):
    def test_back_is_unavailable_on_the_first_step(self) -> None:
        flow = fr.FirstRunFlow()
        self.assertFalse(flow.can_go_back)
        self.assertFalse(flow.back())
        self.assertIs(flow.step, fr.Step.DISPLAY)

    def test_back_works_from_a_blocked_step(self) -> None:
        """Otherwise a user who mistyped a URL is trapped on step 2."""
        flow = _flow_at_pictures()
        self.assertFalse(flow.can_advance)
        self.assertTrue(flow.back())
        self.assertIs(flow.step, fr.Step.DISPLAY)

    def test_going_back_keeps_what_was_entered(self) -> None:
        flow = _flow_at_pictures(_ok())
        flow.choose_display("1552-8-0")
        flow.back()
        self.assertEqual(flow.chosen_display_id, "1552-8-0")
        self.assertIsNotNone(flow.test_result)


class ReachabilityTests(unittest.TestCase):
    def test_the_current_and_earlier_steps_are_always_reachable(self) -> None:
        flow = _flow_at_pictures()
        self.assertTrue(flow.reachable(fr.Step.DISPLAY))
        self.assertTrue(flow.reachable(fr.Step.PICTURES))

    def test_confirm_is_unreachable_until_pictures_passes(self) -> None:
        flow = _flow_at_pictures()
        self.assertFalse(flow.reachable(fr.Step.CONFIRM))
        flow.record_test(_ok())
        self.assertTrue(flow.reachable(fr.Step.CONFIRM))

    def test_reachability_does_not_move_the_flow(self) -> None:
        flow = _flow_at_pictures(_ok())
        flow.reachable(fr.Step.CONFIRM)
        self.assertIs(flow.step, fr.Step.PICTURES)

    def test_confirm_is_reachable_from_the_display_step_once_tested(self) -> None:
        flow = fr.FirstRunFlow()
        flow.record_test(_ok())
        self.assertTrue(flow.reachable(fr.Step.CONFIRM))


class ConfirmCopyTests(unittest.TestCase):
    """This copy is specified verbatim."""

    def test_the_note_is_the_plans_words(self) -> None:
        self.assertEqual(
            fr.CONFIRM_NOTE,
            "These numbers came from one owner's device. Yours may differ "
            "slightly. You can adjust this any time from the menu.",
        )

    def test_the_dark_ring_is_explained_where_it_is_visible(self) -> None:
        """Unexplained, the gap reads as miscalibration and the
        user spends the safety margin correcting it."""
        self.assertEqual(
            fr.DARK_RING_NOTE,
            "Pictures sit slightly inside the edge of the glass so nothing "
            "gets clipped. That thin dark ring is deliberate.",
        )

    def test_the_followup_points_at_the_menu(self) -> None:
        self.assertEqual(
            fr.DARK_RING_FOLLOWUP,
            "If pictures ever look cut off at the edge, use Adjust the "
            "circle from the menu.",
        )

    def test_both_ring_lines_are_on_the_confirm_step(self) -> None:
        notes = fr.confirm_notes()
        self.assertIn(fr.CONFIRM_NOTE, notes)
        self.assertIn(fr.DARK_RING_NOTE, notes)
        self.assertIn(fr.DARK_RING_FOLLOWUP, notes)

    def test_the_buttons_are_confirm_and_adjust_not_skip(self) -> None:
        self.assertEqual(fr.CONFIRM_PRIMARY, "Looks good")
        self.assertEqual(fr.CONFIRM_SECONDARY, "Adjust the circle")
        self.assertNotIn("skip", fr.CONFIRM_PRIMARY.lower())
        self.assertNotIn("skip", fr.CONFIRM_SECONDARY.lower())

    def test_the_secondary_names_the_thing_the_followup_names(self) -> None:
        """The followup tells the user to use "Adjust the circle" from
        the menu; if the button were renamed and the line were not, the
        instruction would point at nothing."""
        self.assertIn(fr.CONFIRM_SECONDARY, fr.DARK_RING_FOLLOWUP)


class NoEmojiTests(unittest.TestCase):
    """No emoji or icons in the first-run flow."""

    def test_all_copy_is_plain_text(self) -> None:
        strings = [
            fr.WINDOW_TITLE,
            fr.MENU_ITEM,
            fr.DISPLAY_BODY,
            fr.DISPLAY_CHECK_AGAIN,
            fr.DISPLAY_IDENTIFY,
            fr.DISPLAY_SKIP_NOTE,
            fr.PICTURES_BODY,
            fr.PICTURES_BLOCKED_NOTE,
            fr.CONFIRM_BODY,
            fr.CONFIRM_PRIMARY,
            fr.CONFIRM_SECONDARY,
            fr.BACK_BUTTON,
            fr.NEXT_BUTTON,
            *fr.confirm_notes(),
            *fr.STEP_TITLES.values(),
        ]
        for text in strings:
            with self.subTest(text=text):
                for char in text:
                    # Unicode category "So" (Symbol, other) is where
                    # emoji and pictographs live. Testing that rather
                    # than "is it ASCII" deliberately: em dashes,
                    # ellipses and curly quotes are typography this
                    # project already uses in menu and settings copy,
                    # and a rule that banned them would be a rule about
                    # character encoding rather than about icons.
                    self.assertNotEqual(
                        unicodedata.category(char),
                        "So",
                        f"symbol character {char!r} in {text!r}",
                    )


class FinishTests(unittest.TestCase):
    def test_the_flow_is_incomplete_without_a_usable_source(self) -> None:
        flow = fr.FirstRunFlow()
        self.assertFalse(flow.is_complete)
        self.assertFalse(flow.finish(fr.Finish.CONFIRMED))
        self.assertIsNone(flow.finished)

    def test_confirming_finishes(self) -> None:
        flow = _flow_at_pictures(_ok())
        flow.advance()
        self.assertTrue(flow.finish(fr.Finish.CONFIRMED))
        self.assertIs(flow.finished, fr.Finish.CONFIRMED)
        self.assertFalse(flow.wants_calibration)

    def test_adjusting_also_finishes_and_asks_for_calibration(self) -> None:
        """`ADJUST` must not behave like cancel — throwing away a
        completed source because the user wanted to nudge a circle would
        drop them back into `Setup needed`."""
        flow = _flow_at_pictures(_ok())
        flow.advance()
        self.assertTrue(flow.finish(fr.Finish.ADJUST))
        self.assertTrue(flow.wants_calibration)
        self.assertTrue(flow.is_complete)

    def test_a_display_choice_is_not_required_to_finish(self) -> None:
        flow = _flow_at_pictures(_ok())
        self.assertTrue(flow.is_complete)


class SourceTests(unittest.TestCase):
    def test_the_form_round_trips_through_step_fours_validator(self) -> None:
        flow = fr.FirstRunFlow()
        flow.form.kind = source_settings.KIND_IMAGE_SERVER
        flow.form.base_url = "http://example.test:8883"
        flow.form.pool = "starred"
        source = flow.to_source()
        self.assertIsNotNone(source)
        self.assertEqual(source.kind, source_settings.KIND_IMAGE_SERVER)

    def test_an_incomplete_form_yields_no_source(self) -> None:
        flow = fr.FirstRunFlow()
        flow.form.kind = source_settings.KIND_JSON_URL
        flow.form.list_url = ""
        self.assertIsNone(flow.to_source())


class TriggerTests(unittest.TestCase):
    """The flow triggers on `setup_needed`."""

    def test_a_fresh_config_triggers_the_flow(self) -> None:
        self.assertTrue(fr.should_start({}))

    def test_a_missing_config_triggers_the_flow(self) -> None:
        self.assertTrue(fr.should_start(None))

    def test_a_configured_source_does_not_trigger(self) -> None:
        self.assertFalse(fr.should_start({"source": {"kind": "folder"}}))

    def test_it_agrees_with_the_menu_exactly(self) -> None:
        """A menu reading `Setup needed` while the flow declines to open
        is a dead end with no way out."""
        from ui import menubar_state

        for data in ({}, None, {"source": {}}, {"image_studio_base_url": "http://x"}):
            with self.subTest(data=data):
                self.assertEqual(fr.should_start(data), menubar_state.setup_needed(data))

    def test_the_menu_item_has_no_emoji(self) -> None:
        self.assertEqual(fr.MENU_ITEM, "Finish setup…")

    def test_the_bundled_seed_does_not_suppress_the_flow(self) -> None:
        """The inversion of a trap that used to be live.

        resolution order copies `display/config/settings.json`
        into `~/.viewlab/` the first time anything reads settings. While
        that file carried the legacy flat `image_studio_base_url` key,
        `setup_needed` counted it as a real source, so merely *reading*
        settings on a fresh machine was enough to stop the first-run
        flow from ever opening — a stranger landed on a config pointing
        at a host that does not exist for them, with no flow offering to
        fix it and no error explaining it.

        Step 6 stripped the flat `image_studio_base_url` and `pool` keys
        from the seed, so the assertion now runs the other way: the seed
        must describe *no* source, and a fresh install must open the
        flow. Keeping the test (rather than deleting it) is what stops
        a future edit from putting a default source back into the seed.
        """
        from display import paths

        seed = read_json_object(paths.bundled_settings_path(), "settings") or {}
        self.assertNotIn(
            "image_studio_base_url",
            seed,
            "the bundled seed must not name a source; a flat source key "
            "suppresses the first-run flow on a fresh install",
        )
        self.assertNotIn("pool", seed)
        self.assertNotIn("source", seed)
        self.assertTrue(
            fr.should_start(seed),
            "a fresh install reading only the bundled seed must open the "
            "first-run flow",
        )


class SettingsDocumentTests(unittest.TestCase):
    def _source(self) -> SourceSettings:
        return source_settings.validate_source(
            {"kind": source_settings.KIND_IMAGE_SERVER, "base_url": "http://x:8883", "pool": "starred"}
        )

    def test_writing_the_source_clears_the_trigger(self) -> None:
        """No separate "first run done" marker: one fact, one place."""
        document = fr.settings_document({}, self._source())
        self.assertFalse(fr.should_start(document))

    def test_unknown_keys_survive(self) -> None:
        """The config is additive-only."""
        document = fr.settings_document(
            {"something_a_newer_build_knows": 5}, self._source()
        )
        self.assertEqual(document["something_a_newer_build_knows"], 5)

    def test_a_hand_edited_interval_is_not_reverted(self) -> None:
        """The flow never mentions timing, so it must not change it."""
        document = fr.settings_document({"rotation_interval_s": 60.0}, self._source())
        self.assertEqual(document["rotation_interval_s"], 60.0)

    def test_a_hand_edited_shuffle_is_not_reverted(self) -> None:
        document = fr.settings_document({"shuffle": False}, self._source())
        self.assertIs(document["shuffle"], False)

    def test_an_existing_schedule_survives(self) -> None:
        schedule = BlankSchedule(enabled=True, start_minute=100, end_minute=200)
        document = fr.settings_document(
            {"blank_schedule": schedule.to_dict()}, self._source()
        )
        # Asserted by round-tripping through the real parser rather than
        # by reaching into the serialised keys, so this test does not
        # pin `to_dict`'s wire format as a side effect.
        self.assertEqual(
            blank_schedule.parse_schedule(document["blank_schedule"]), schedule
        )

    def test_defaults_are_used_when_nothing_is_there(self) -> None:
        document = fr.settings_document(None, self._source())
        self.assertEqual(document["rotation_interval_s"], 900.0)

    def test_garbage_values_do_not_raise(self) -> None:
        """Defensive reads never raise."""
        for previous in ({"rotation_interval_s": "soon"}, {"shuffle": "yes"},
                         {"rotation_interval_s": -5}, {"blank_schedule": "nightly"},
                         {"rotation_interval_s": True}):
            with self.subTest(previous=previous):
                document = fr.settings_document(previous, self._source())
                self.assertIsInstance(document["rotation_interval_s"], float)
                self.assertGreater(document["rotation_interval_s"], 0)
                self.assertIsInstance(document["shuffle"], bool)


class CalibrationDocumentTests(unittest.TestCase):
    def test_no_existing_calibration_means_do_not_write(self) -> None:
        self.assertIsNone(fr.calibration_document(None, "4-5-6"))
        self.assertIsNone(fr.calibration_document({}, "4-5-6"))

    def test_an_explicit_choice_is_recorded(self) -> None:
        document = fr.calibration_document({"radius_px": 472}, "1552-8-0")
        self.assertEqual(
            document["target_screen"],
            {"resolve_strategy": fr.EXPLICIT_RESOLVE_STRATEGY, "display_id": "1552-8-0"},
        )

    def test_the_display_id_is_written_as_a_string(self) -> None:
        """`Calibration.target_display_id` is a str and the parser calls
        `.strip()` on it behind an isinstance check — a number here would
        be silently dropped and the display would fall back to the
        heuristic with no error anywhere."""
        document = fr.calibration_document({"radius_px": 472}, "1552-8-0")
        self.assertIsInstance(document["target_screen"]["display_id"], str)

    def test_no_choice_records_the_heuristic(self) -> None:
        document = fr.calibration_document({"radius_px": 472}, "")
        self.assertEqual(
            document["target_screen"]["resolve_strategy"],
            fr.FALLBACK_RESOLVE_STRATEGY,
        )
        self.assertNotIn("display_id", document["target_screen"])

    def test_the_circle_numbers_are_preserved(self) -> None:
        """The config is additive-only, and these are the numbers that matter."""
        previous = {
            "schema_version": 1,
            "framebuffer": {"width": 960.0, "height": 960.0},
            "circle": {"center_x": 483.0, "center_y": 482.0, "radius_px": 472.0},
            "safety_margin_pct": 0.93,
        }
        document = fr.calibration_document(previous, "1552-8-0")
        for key, value in previous.items():
            self.assertEqual(document[key], value)

    def test_the_input_is_not_mutated(self) -> None:
        previous = {"radius_px": 472}
        fr.calibration_document(previous, "1552-8-0")
        self.assertNotIn("target_screen", previous)

    def test_the_document_survives_a_calibration_round_trip(self) -> None:
        """The real check that the written block is one the display can
        read back: parse it with the loader's own parser."""
        from display import calibration as cal

        document = fr.calibration_document(
            {
                "schema_version": 1,
                "framebuffer": {"width": 960.0, "height": 960.0},
                "circle": {"center_x": 483.0, "center_y": 482.0, "radius_px": 472.0},
                "safety_margin_pct": 0.93,
            },
            "1552-8-0",
        )
        parsed = cal.validate_calibration(document)
        self.assertEqual(parsed.target_display_id, "1552-8-0")
        self.assertEqual(parsed.target_strategy, fr.EXPLICIT_RESOLVE_STRATEGY)

    def test_merging_into_the_bundled_document_yields_a_valid_one(self) -> None:
        """First run is exactly when `~/.viewlab/calibration.json` may not
        exist, so the window merges into the bundled file instead. That is
        only safe if the result is a document the display accepts."""
        from display import calibration as cal
        from display import paths

        bundled = read_json_object(paths.bundled_calibration_path(), "calibration")
        if not bundled:
            self.skipTest("no bundled calibration to merge into")
        document = fr.calibration_document(bundled, "1552-8-0")
        parsed = cal.validate_calibration(document)
        self.assertIsNotNone(parsed, "merged bundled calibration must validate")
        self.assertEqual(parsed.target_display_id, "1552-8-0")
        # The circle numbers must have survived — a target_screen with no
        # circle beside it is the thing this merge exists to avoid.
        self.assertGreater(parsed.radius_px, 0)

    def test_the_strategy_string_matches_display_target(self) -> None:
        """`first_run_state` restates this constant rather than importing
        `display_target` (which imports AppKit). That restating is only
        safe if something checks it, so this is that check."""
        try:
            from display import display_target
        except ImportError:  # pragma: no cover - AppKit absent
            self.skipTest("AppKit not available")
        self.assertEqual(fr.EXPLICIT_RESOLVE_STRATEGY, display_target.EXPLICIT_STRATEGY)


if __name__ == "__main__":
    unittest.main()
