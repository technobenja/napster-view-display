"""Unit tests for control.py — desired-state command file.

Pure logic over a temp directory: no AppKit, no window server, no timers.
The cases that carry their weight here are the ones the spec calls out by
name as things a naive `{seq, action}` channel gets wrong — a UI restart
that resets the counter, replay of an hour-old command across a display
restart, and a corrupt file that must be ignored exactly once rather than
re-fired four times a second.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from display import control
from display.control import ControlChannel, ControlState, parse_control, read_control, write_control


class ParseControlTests(unittest.TestCase):
    """Defensive reading ("allowlist keys, ignore unknowns, never
    raise")."""

    def test_the_documented_shape_round_trips(self) -> None:
        state = parse_control(
            {
                "blanked": None,
                "paused": False,
                "paused_on_id": None,
                "advance": 42,
                "preview_calibration": None,
                "written_at": 1752800000.0,
            }
        )
        self.assertIsNone(state.blanked)
        self.assertFalse(state.paused)
        self.assertIsNone(state.paused_on_id)
        self.assertEqual(state.advance, 42)
        self.assertIsNone(state.preview_calibration)
        self.assertEqual(state.written_at, 1752800000.0)

    def test_unknown_keys_are_ignored_not_rejected(self) -> None:
        """A newer UI writing a field this build has never heard of must
        cost that field, not the channel."""
        state = parse_control({"advance": 3, "scheduled_blank_until": "07:00"})
        self.assertEqual(state.advance, 3)

    def test_never_raises_on_hostile_input(self) -> None:
        for payload in (None, [], "a string", 42, {"advance": {"nested": 1}}):
            with self.subTest(payload=payload):
                try:
                    parse_control(payload)
                except Exception as exc:  # noqa: BLE001 - this is the assertion
                    self.fail(f"parse_control({payload!r}) raised {exc!r}")

    def test_one_bad_field_costs_only_that_field(self) -> None:
        state = parse_control({"advance": "not a number", "paused": True})
        self.assertEqual(state.advance, 0)
        self.assertTrue(state.paused)

    def test_blanked_is_tri_state_and_strict_about_bools(self) -> None:
        """The string "false" must not read as True. That mistake leaves
        someone's View dark with nothing anywhere to explain why."""
        self.assertIsNone(parse_control({"blanked": "false"}).blanked)
        self.assertIsNone(parse_control({"blanked": "true"}).blanked)
        self.assertIsNone(parse_control({"blanked": 1}).blanked)
        self.assertIs(parse_control({"blanked": True}).blanked, True)
        self.assertIs(parse_control({"blanked": False}).blanked, False)

    def test_advance_rejects_bools_despite_bool_being_an_int(self) -> None:
        self.assertEqual(parse_control({"advance": True}).advance, 0)

    def test_effective_blanked_treats_null_as_not_blanked(self) -> None:
        """Tri-state: null means "follow the schedule", and no
        schedule ships in Step 1."""
        self.assertFalse(ControlState(blanked=None).effective_blanked())
        self.assertTrue(ControlState(blanked=True).effective_blanked())
        self.assertFalse(ControlState(blanked=False).effective_blanked())

    def test_preview_calibration_allow_lists_keys_and_drops_junk(self) -> None:
        preview = parse_control(
            {
                "preview_calibration": {
                    "center_x": 483,
                    "radius_px": 470.5,
                    "framebuffer_width": 9999,  # not nudgeable
                    "safety_margin_pct": "0.93",  # wrong type
                }
            }
        ).preview_calibration
        self.assertEqual(preview, {"center_x": 483.0, "radius_px": 470.5})

    def test_preview_calibration_drops_nan_and_infinity(self) -> None:
        """These would reach destination_rect() and out into AppKit as a
        garbage frame."""
        preview = parse_control(
            {"preview_calibration": {"center_x": float("nan"), "radius_px": float("inf")}}
        ).preview_calibration
        self.assertIsNone(preview)

    def test_an_empty_preview_is_none_not_an_empty_dict(self) -> None:
        self.assertIsNone(parse_control({"preview_calibration": {}}).preview_calibration)


class ReadWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "command.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_missing_file_reads_as_none_not_as_a_default_state(self) -> None:
        """The distinction is load-bearing: "I could not read it" must
        never be mistaken for "the user wants nothing", or a read hiccup
        would un-blank a blanked View."""
        self.assertIsNone(read_control(self.path))

    def test_corrupt_json_reads_as_none(self) -> None:
        self.path.write_text("{not json at all")
        self.assertIsNone(read_control(self.path))

    def test_write_then_read_round_trips(self) -> None:
        state = ControlState(blanked=True, paused=True, paused_on_id="img-7", advance=9)
        self.assertTrue(write_control(self.path, state))
        self.assertEqual(read_control(self.path), state)

    def test_written_file_carries_exactly_the_documented_keys(self) -> None:
        write_control(self.path, ControlState())
        self.assertEqual(set(json.loads(self.path.read_text())), control.ALLOWED_KEYS)


class AdvanceCounterTests(unittest.TestCase):
    """Incremental half — the part a `{seq, action}` file gets
    wrong."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "command.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, **fields) -> None:
        write_control(self.path, ControlState(**fields))

    def _channel(self) -> ControlChannel:
        channel = ControlChannel(path=self.path)
        channel.adopt_current()
        return channel

    def test_two_fast_clicks_inside_one_window_apply_two_steps(self) -> None:
        """The whole reason the field is a counter rather than an action.
        The display only ever *observes* the endpoint — 2 — but must
        apply both presses."""
        self._write(advance=0)
        channel = self._channel()
        self._write(advance=2)

        update = channel.poll()
        self.assertIsNotNone(update)
        self.assertEqual(update.steps, 2)

    def test_a_single_click_applies_one_step(self) -> None:
        self._write(advance=41)
        channel = self._channel()
        self._write(advance=42)
        self.assertEqual(channel.poll().steps, 1)

    def test_previous_is_a_negative_delta(self) -> None:
        """The command schema has one counter, but both Next and
        Previous to survive fast clicking. A signed delta on the single
        documented field is what satisfies both."""
        self._write(advance=10)
        channel = self._channel()
        self._write(advance=8)
        self.assertEqual(channel.poll().steps, -2)

    def test_a_ui_restart_that_resets_the_counter_does_not_wedge_the_channel(
        self,
    ) -> None:
        """`!=` rule, stated as the failure it prevents. Under
        `new > last_seen`, a reinstalled UI counting from zero again
        would leave Next permanently, silently dead."""
        self._write(advance=500)
        channel = self._channel()

        # The UI is reinstalled and starts over.
        self._write(advance=0)
        reset = channel.poll()
        self.assertIsNotNone(reset)
        self.assertEqual(channel.last_seen_advance, 0)

        # The crucial part: the very next click must work.
        self._write(advance=1)
        self.assertEqual(channel.poll().steps, 1)

    def test_a_counter_reset_is_clamped_rather_than_teleporting(self) -> None:
        """The cost of the `!=` rule is bounded, not unbounded: a jump of
        500 moves at most MAX_ADVANCE_STEPS pictures."""
        self._write(advance=0)
        channel = self._channel()
        self._write(advance=500)
        self.assertEqual(channel.poll().steps, control.MAX_ADVANCE_STEPS)

    def test_a_large_negative_jump_is_clamped_too(self) -> None:
        self._write(advance=500)
        channel = self._channel()
        self._write(advance=0)
        self.assertEqual(channel.poll().steps, -control.MAX_ADVANCE_STEPS)

    def test_an_unchanged_counter_produces_no_steps(self) -> None:
        self._write(advance=7, paused=False)
        channel = self._channel()
        self._write(advance=7, paused=True)  # desired state changed, counter did not
        update = channel.poll()
        self.assertEqual(update.steps, 0)
        self.assertTrue(update.state.paused)


class RefreshCounterTests(unittest.TestCase):
    """`Check for new pictures now`, added in Step 4. Deliberately
    the same shape as `advance` — a flag cannot distinguish "asked again"
    from "still asking"."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "command.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, **fields) -> None:
        write_control(self.path, ControlState(**fields))

    def _channel(self) -> ControlChannel:
        channel = ControlChannel(path=self.path)
        channel.adopt_current()
        return channel

    def test_a_new_value_requests_a_refresh(self) -> None:
        self._write(refresh=0)
        channel = self._channel()
        self._write(refresh=1)
        update = channel.poll()
        self.assertTrue(update.refresh_requested)

    def test_an_unchanged_value_does_not(self) -> None:
        """Level-triggered fields are re-read constantly; this one must
        not re-poll the source every time anything else changes."""
        self._write(refresh=3)
        channel = self._channel()
        self._write(refresh=3, paused=True)
        update = channel.poll()
        self.assertFalse(update.refresh_requested)

    def test_startup_adopts_rather_than_replaying(self) -> None:
        """The display polls its source at startup anyway, so replaying
        an old refresh would be a duplicate poll on every launch."""
        self._write(refresh=9)
        channel = self._channel()
        self.assertEqual(channel.last_seen_refresh, 9)
        self._write(refresh=9, advance=1)
        self.assertFalse(channel.poll().refresh_requested)

    def test_a_ui_counter_reset_is_not_wedged(self) -> None:
        """`!=`, not `>` — the same rule `advance` follows, for the same
        reason: a reinstalled UI counting from zero must not lose this
        button permanently."""
        self._write(refresh=12)
        channel = self._channel()
        self._write(refresh=1)
        self.assertTrue(channel.poll().refresh_requested)

    def test_a_junk_refresh_value_does_not_break_the_channel(self) -> None:
        self.path.write_text(json.dumps({"refresh": "soon", "advance": 2}))
        channel = ControlChannel(path=self.path)
        channel.adopt_current()
        self.assertEqual(channel.state.refresh, 0)


class ScheduledBlankingTests(unittest.TestCase):
    """Seam, wired in Step 4. The arithmetic itself is tested in
    test_blank_schedule.py; what matters here is that `ControlState`
    still behaves exactly as it did when no schedule is supplied."""

    def test_no_schedule_is_unchanged_behaviour(self) -> None:
        self.assertFalse(ControlState(blanked=None).effective_blanked())
        self.assertTrue(ControlState(blanked=True).effective_blanked())
        self.assertFalse(ControlState(blanked=False).effective_blanked())

    def test_an_inactive_schedule_is_also_unchanged(self) -> None:
        from display.blank_schedule import BlankSchedule

        off = BlankSchedule(enabled=False)
        self.assertFalse(ControlState(blanked=None).effective_blanked(off))
        self.assertTrue(ControlState(blanked=True).effective_blanked(off))

    def test_an_active_schedule_decides_when_there_is_no_override(self) -> None:
        import time as _time

        from display.blank_schedule import BlankSchedule

        schedule = BlankSchedule(True, 21 * 60, 7 * 60)
        night = _time.mktime((2026, 7, 19, 22, 0, 0, 0, 0, -1))
        noon = _time.mktime((2026, 7, 19, 12, 0, 0, 0, 0, -1))
        self.assertTrue(ControlState(blanked=None).effective_blanked(schedule, night))
        self.assertFalse(ControlState(blanked=None).effective_blanked(schedule, noon))

    def test_the_refresh_key_round_trips_through_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "command.json"
            write_control(path, ControlState(refresh=4, advance=2))
            self.assertEqual(read_control(path).refresh, 4)


class StartupAdoptionTests(unittest.TestCase):
    """"on startup, adopt current state as already-seen; never
    replay"."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "command.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_an_hour_old_command_is_never_replayed_on_startup(self) -> None:
        """The in-memory-`last_seen` failure mode, tested directly: a
        display that restarts must not walk 42 pictures forward because
        the file still says 42."""
        write_control(self.path, ControlState(advance=42, written_at=1.0))

        channel = ControlChannel(path=self.path)
        channel.adopt_current()

        self.assertEqual(channel.last_seen_advance, 42)
        self.assertIsNone(channel.poll())  # nothing changed since adoption

    def test_startup_adopts_desired_state_even_though_it_drops_the_counter(
        self,
    ) -> None:
        """The asymmetry is the design: desired state IS applied on
        startup (that is how blanking survives a restart); only
        the counter delta is dropped."""
        write_control(
            self.path, ControlState(blanked=True, paused=True, paused_on_id="x", advance=9)
        )
        channel = ControlChannel(path=self.path)
        state = channel.adopt_current()

        self.assertTrue(state.effective_blanked())
        self.assertTrue(state.paused)
        self.assertEqual(state.paused_on_id, "x")
        self.assertEqual(channel.last_seen_advance, 9)

    def test_a_click_after_startup_still_applies(self) -> None:
        write_control(self.path, ControlState(advance=42))
        channel = ControlChannel(path=self.path)
        channel.adopt_current()

        write_control(self.path, ControlState(advance=43))
        self.assertEqual(channel.poll().steps, 1)

    def test_startup_with_no_file_at_all_is_a_clean_default(self) -> None:
        channel = ControlChannel(path=self.path)
        state = channel.adopt_current()
        self.assertEqual(state, ControlState())
        self.assertEqual(channel.last_seen_advance, 0)
        self.assertIsNone(channel.poll())


class CorruptAndStaleFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "command.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_corrupt_file_is_ignored_and_never_re_fires(self) -> None:
        """Parsed and complained about once, then silent — not four times
        a second forever."""
        write_control(self.path, ControlState(advance=1, blanked=True))
        channel = ControlChannel(path=self.path)
        channel.adopt_current()

        self.path.write_text("}{ garbage")
        self.assertIsNone(channel.poll())
        self.assertIsNone(channel.poll())
        self.assertIsNone(channel.poll())

    def test_a_corrupt_file_leaves_the_last_good_desired_state_alone(self) -> None:
        """A blanked View must not un-blank because someone half-saved an
        edit to the command file."""
        write_control(self.path, ControlState(blanked=True))
        channel = ControlChannel(path=self.path)
        channel.adopt_current()
        self.assertTrue(channel.state.effective_blanked())

        self.path.write_text("not json")
        channel.poll()
        self.assertTrue(channel.state.effective_blanked())

    def test_deleting_the_file_does_not_reset_desired_state(self) -> None:
        """Deliberately not a reset to defaults: deleting command.json
        must not silently un-blank a blanked View."""
        write_control(self.path, ControlState(blanked=True, paused=True))
        channel = ControlChannel(path=self.path)
        channel.adopt_current()

        self.path.unlink()
        self.assertIsNone(channel.poll())
        self.assertTrue(channel.state.effective_blanked())
        self.assertTrue(channel.state.paused)

    def test_recovery_after_a_corrupt_file_is_repaired(self) -> None:
        write_control(self.path, ControlState(advance=1))
        channel = ControlChannel(path=self.path)
        channel.adopt_current()

        self.path.write_text("garbage")
        self.assertIsNone(channel.poll())

        write_control(self.path, ControlState(advance=2))
        self.assertEqual(channel.poll().steps, 1)

    def test_an_unchanged_file_is_not_re_parsed(self) -> None:
        """What makes running this four times a second free."""
        write_control(self.path, ControlState(advance=1))
        channel = ControlChannel(path=self.path)
        channel.adopt_current()
        for _ in range(10):
            self.assertIsNone(channel.poll())

    def test_the_channel_never_writes_the_command_file(self) -> None:
        """One-writer rule. The display reads this file; the UI
        owns it. `ControlChannel` has no write method at all — asserted
        structurally so a future convenience method cannot be added
        without this failing."""
        writers = [
            name
            for name in dir(ControlChannel)
            if not name.startswith("__") and "write" in name.lower()
        ]
        self.assertEqual(writers, [])

    def test_a_read_only_command_file_is_still_readable(self) -> None:
        write_control(self.path, ControlState(advance=3))
        self.path.chmod(0o444)
        channel = ControlChannel(path=self.path)
        self.assertEqual(channel.adopt_current().advance, 3)


if __name__ == "__main__":
    unittest.main()
