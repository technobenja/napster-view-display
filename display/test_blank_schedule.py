"""Scheduled blanking — the arithmetic and the override rule.

The interesting behaviour here is not "does 21:00 fall inside 21:00-07:00"
but the override expiry, which is the mechanism that lets "clears
back to null at the next window boundary" work without anybody writing a
file. Those tests pin exact timestamps rather than using the clock.
"""

from __future__ import annotations

import time
import unittest

from display import blank_schedule as bs
from display.blank_schedule import BlankSchedule


def at(year: int, month: int, day: int, hour: int, minute: int = 0) -> float:
    """A local-time timestamp. Local, because a blanking window is
    expressed in the user's own clock — a UTC fixture here would pass on
    the build machine and mean nothing."""
    return time.mktime((year, month, day, hour, minute, 0, 0, 0, -1))


class ParseMinuteTests(unittest.TestCase):
    def test_accepts_the_forms_a_person_actually_types(self) -> None:
        for text, expected in (
            ("9:00 PM", 21 * 60),
            ("9 PM", 21 * 60),
            ("21:00", 21 * 60),
            ("2100", 21 * 60),
            ("7:30 AM", 7 * 60 + 30),
            ("12:00 AM", 0),
            ("12:00 PM", 12 * 60),
            ("  7:00 am  ", 7 * 60),
        ):
            with self.subTest(text=text):
                self.assertEqual(bs.parse_minute(text), expected)

    def test_returns_none_for_a_half_typed_value_rather_than_guessing(self) -> None:
        """The caller keeps the last good value; substituting a default
        mid-keystroke would fight the user."""
        for text in ("", "9:", ":", "abc", "25:00", "9:70", "13 PM", "0 PM"):
            with self.subTest(text=text):
                self.assertIsNone(bs.parse_minute(text))

    def test_rejects_bools_which_are_ints_in_python(self) -> None:
        self.assertIsNone(bs.parse_minute(True))

    def test_round_trips_through_format(self) -> None:
        for minute in (0, 1, 7 * 60, 12 * 60, 21 * 60, 23 * 60 + 59):
            with self.subTest(minute=minute):
                self.assertEqual(bs.parse_minute(bs.format_minute(minute)), minute)


class InWindowTests(unittest.TestCase):
    def test_an_overnight_window_wraps(self) -> None:
        schedule = BlankSchedule(True, 21 * 60, 7 * 60)
        self.assertTrue(schedule.in_window(at(2026, 7, 19, 22)))
        self.assertTrue(schedule.in_window(at(2026, 7, 20, 3)))
        self.assertFalse(schedule.in_window(at(2026, 7, 20, 8)))
        self.assertFalse(schedule.in_window(at(2026, 7, 19, 20, 59)))

    def test_boundaries_are_start_inclusive_end_exclusive(self) -> None:
        schedule = BlankSchedule(True, 21 * 60, 7 * 60)
        self.assertTrue(schedule.in_window(at(2026, 7, 19, 21, 0)))
        self.assertFalse(schedule.in_window(at(2026, 7, 20, 7, 0)))

    def test_a_daytime_window_does_not_wrap(self) -> None:
        schedule = BlankSchedule(True, 9 * 60, 17 * 60)
        self.assertTrue(schedule.in_window(at(2026, 7, 19, 12)))
        self.assertFalse(schedule.in_window(at(2026, 7, 19, 20)))

    def test_a_zero_length_window_never_blanks(self) -> None:
        """Both readings are defensible from the arithmetic; only one is
        safe. A user who fat-fingers both fields the same gets a working
        View, not a permanently dark one."""
        schedule = BlankSchedule(True, 21 * 60, 21 * 60)
        self.assertFalse(schedule.is_active)
        self.assertFalse(schedule.in_window(at(2026, 7, 19, 21)))

    def test_a_disabled_schedule_is_never_in_window(self) -> None:
        schedule = BlankSchedule(False, 21 * 60, 7 * 60)
        self.assertFalse(schedule.in_window(at(2026, 7, 19, 22)))


class MostRecentBoundaryTests(unittest.TestCase):
    def test_after_midnight_the_boundary_is_last_nights_start(self) -> None:
        """The case that requires looking at yesterday: at 00:30 the most
        recent edge of a 21:00-07:00 window is last night's 21:00, and an
        override written at 22:00 must still be in force."""
        schedule = BlankSchedule(True, 21 * 60, 7 * 60)
        boundary = schedule.most_recent_boundary(at(2026, 7, 20, 0, 30))
        self.assertEqual(boundary, at(2026, 7, 19, 21, 0))

    def test_mid_morning_the_boundary_is_this_mornings_end(self) -> None:
        schedule = BlankSchedule(True, 21 * 60, 7 * 60)
        boundary = schedule.most_recent_boundary(at(2026, 7, 20, 9, 0))
        self.assertEqual(boundary, at(2026, 7, 20, 7, 0))


class EffectiveBlankedTests(unittest.TestCase):
    """Whole state model, in one place."""

    def setUp(self) -> None:
        self.schedule = BlankSchedule(True, 21 * 60, 7 * 60)

    def test_no_schedule_is_exactly_step_ones_behaviour(self) -> None:
        """The compatibility guarantee: a user who never opens the new
        checkbox sees no change whatsoever."""
        self.assertFalse(bs.effective_blanked(None, 0.0, None))
        self.assertTrue(bs.effective_blanked(True, 0.0, None))
        self.assertFalse(bs.effective_blanked(False, 0.0, None))

    def test_a_disabled_schedule_falls_back_to_the_manual_flag(self) -> None:
        off = BlankSchedule(False, 21 * 60, 7 * 60)
        self.assertFalse(bs.effective_blanked(None, 0.0, off, at(2026, 7, 19, 22)))
        self.assertTrue(bs.effective_blanked(True, 0.0, off, at(2026, 7, 19, 12)))

    def test_no_override_follows_the_window(self) -> None:
        self.assertTrue(
            bs.effective_blanked(None, 0.0, self.schedule, at(2026, 7, 19, 22))
        )
        self.assertFalse(
            bs.effective_blanked(None, 0.0, self.schedule, at(2026, 7, 19, 12))
        )

    def test_an_override_holds_until_the_next_boundary(self) -> None:
        """Central case: turned back on at 22:00 stays on for the
        rest of the night."""
        written = at(2026, 7, 19, 22, 0)
        self.assertFalse(
            bs.effective_blanked(False, written, self.schedule, at(2026, 7, 19, 23))
        )
        self.assertFalse(
            bs.effective_blanked(False, written, self.schedule, at(2026, 7, 20, 3))
        )

    def test_the_override_expires_at_the_boundary_and_the_schedule_resumes(self) -> None:
        """"...so the schedule resumes tonight without the user
        remembering anything." Same override, one night later: the 07:00
        edge has passed, so it is no longer in force and 22:00 blanks."""
        written = at(2026, 7, 19, 22, 0)
        self.assertTrue(
            bs.effective_blanked(False, written, self.schedule, at(2026, 7, 20, 22))
        )

    def test_a_manual_blank_outside_the_window_holds_until_the_start_edge(self) -> None:
        written = at(2026, 7, 19, 15, 0)
        self.assertTrue(
            bs.effective_blanked(True, written, self.schedule, at(2026, 7, 19, 16))
        )

    def test_an_undatable_override_is_treated_as_stale(self) -> None:
        """It cannot be placed relative to any boundary, and the standing
        instruction the user configured is the better guess."""
        for stamp in (0.0, -1.0, True):
            with self.subTest(stamp=stamp):
                self.assertTrue(
                    bs.effective_blanked(
                        False, stamp, self.schedule, at(2026, 7, 19, 22)
                    )
                )


class ParseScheduleTests(unittest.TestCase):
    def test_a_malformed_block_disables_rather_than_defaulting_on(self) -> None:
        """A typo in a hand-edited file must not blank someone's View
        overnight with no trace of why."""
        for data in (None, [], {}, {"enabled": True}, {"enabled": True, "start": "nope", "end": "7:00 AM"}):
            with self.subTest(data=data):
                self.assertFalse(bs.parse_schedule(data).enabled)

    def test_a_valid_block_round_trips(self) -> None:
        schedule = BlankSchedule(True, 21 * 60, 7 * 60)
        self.assertEqual(bs.parse_schedule(schedule.to_dict()), schedule)

    def test_a_non_bool_enabled_is_not_truthy(self) -> None:
        parsed = bs.parse_schedule({"enabled": "yes", "start": "9:00 PM", "end": "7:00 AM"})
        self.assertFalse(parsed.enabled)


class DescribeTests(unittest.TestCase):
    """Two strings are named specifically; both must actually appear."""

    def setUp(self) -> None:
        self.schedule = BlankSchedule(True, 21 * 60, 7 * 60)

    def test_following_schedule(self) -> None:
        text = bs.describe(None, 0.0, self.schedule, at(2026, 7, 19, 22))
        self.assertIn("Following schedule", text)

    def test_turned_back_on_until_names_the_end_of_the_window(self) -> None:
        written = at(2026, 7, 19, 22, 0)
        text = bs.describe(False, written, self.schedule, at(2026, 7, 19, 23))
        self.assertEqual(text, "Turned back on until 7:00 AM.")

    def test_with_no_schedule_it_still_says_something(self) -> None:
        """A status line that is sometimes empty is one the eye stops
        checking."""
        self.assertTrue(bs.describe(None, 0.0, None))
        self.assertTrue(bs.describe(True, 0.0, None))


if __name__ == "__main__":
    unittest.main()
