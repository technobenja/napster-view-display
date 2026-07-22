"""Unit tests for rotation.py — shuffle/persist/resume,
pool-change detection, the bad-image `is_valid` hook. Pure logic, no
AppKit, no PIL, no live files beyond a temp state dir.
"""

from __future__ import annotations

import json
import random
import tempfile
import unittest
from pathlib import Path

from display.rotation import Rotation, _pool_hash


class RotationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self._tmp.name) / "rotation_state.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _rotation(self, ids, **kwargs) -> Rotation:
        return Rotation(ids, state_path=self.state_path, rng=random.Random(1), **kwargs)

    def test_current_returns_a_pool_member(self) -> None:
        ids = ["a", "b", "c"]
        r = self._rotation(ids)
        self.assertIn(r.current(), ids)

    def test_empty_pool_returns_none_without_raising(self) -> None:
        r = self._rotation([])
        self.assertIsNone(r.current())
        self.assertIsNone(r.next_image())

    def test_full_cycle_visits_every_id_exactly_once(self) -> None:
        ids = [f"id{i}" for i in range(10)]
        r = self._rotation(ids)
        seen = {r.current()}
        for _ in range(len(ids) - 1):
            seen.add(r.next_image())
        self.assertEqual(seen, set(ids))

    def test_reshuffle_on_exhaustion_produces_a_new_order(self) -> None:
        ids = [f"id{i}" for i in range(8)]
        r = self._rotation(ids)
        first_cycle = [r.current()]
        for _ in range(len(ids) - 1):
            first_cycle.append(r.next_image())
        # One more call wraps past exhaustion into a fresh shuffle.
        second_cycle = [r.next_image()]
        for _ in range(len(ids) - 1):
            second_cycle.append(r.next_image())

        self.assertEqual(set(first_cycle), set(ids))
        self.assertEqual(set(second_cycle), set(ids))
        # Vanishingly unlikely to coincidentally match for len 8 - confirms
        # a real reshuffle happened rather than looping the same order.
        self.assertNotEqual(first_cycle, second_cycle)

    def test_single_item_pool_never_raises_across_many_cycles(self) -> None:
        r = self._rotation(["only"])
        for _ in range(5):
            self.assertEqual(r.next_image(), "only")

    def test_persists_and_resumes_position(self) -> None:
        ids = [f"id{i}" for i in range(6)]
        r1 = self._rotation(ids)
        r1.next_image()
        r1.next_image()
        position_id = r1.current()

        r2 = Rotation(ids, state_path=self.state_path, rng=random.Random(99))
        self.assertEqual(r2.current(), position_id)

    def test_unchanged_pool_does_not_reshuffle_on_resume(self) -> None:
        ids = [f"id{i}" for i in range(6)]
        r1 = self._rotation(ids)
        order_before = list(r1._order)  # noqa: SLF001 - whitebox check by design

        r2 = Rotation(ids, state_path=self.state_path, rng=random.Random(99))
        self.assertEqual(list(r2._order), order_before)  # noqa: SLF001

    def test_pool_change_triggers_reshuffle_and_position_reset(self) -> None:
        ids_a = ["a1", "a2", "a3"]
        r1 = self._rotation(ids_a)
        r1.next_image()
        r1.next_image()

        ids_b = ["b1", "b2", "b3", "b4"]
        r2 = Rotation(ids_b, state_path=self.state_path, rng=random.Random(5))
        self.assertIn(r2.current(), ids_b)
        self.assertEqual(set(r2._order), set(ids_b))  # noqa: SLF001

    def test_sync_pool_mid_run_reshuffles_on_change(self) -> None:
        r = self._rotation(["a", "b", "c"])
        r.sync_pool(["x", "y", "z", "w"])
        self.assertEqual(set(r._order), {"x", "y", "z", "w"})  # noqa: SLF001
        self.assertEqual(r._position, 0)  # noqa: SLF001

    def test_sync_pool_same_membership_different_order_is_a_no_op(self) -> None:
        r = self._rotation(["a", "b", "c"])
        r.next_image()
        order_before = list(r._order)  # noqa: SLF001
        position_before = r._position  # noqa: SLF001

        r.sync_pool(["c", "b", "a"])  # same set, different iteration order
        self.assertEqual(list(r._order), order_before)  # noqa: SLF001
        self.assertEqual(r._position, position_before)  # noqa: SLF001

    def test_is_valid_filters_bad_ids_at_queue_build_time(self) -> None:
        ids = ["good1", "good2", "bad1", "bad2"]
        r = self._rotation(ids, is_valid=lambda i: i.startswith("good"))
        self.assertEqual(set(r._order), {"good1", "good2"})  # noqa: SLF001
        for _ in range(6):
            self.assertIn(r.next_image(), {"good1", "good2"})

    def test_all_ids_invalid_behaves_like_empty_pool(self) -> None:
        r = self._rotation(["a", "b"], is_valid=lambda i: False)
        self.assertIsNone(r.current())

    def test_corrupt_state_file_falls_back_to_fresh_shuffle(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text("{not valid json")
        r = self._rotation(["a", "b", "c"])
        self.assertIn(r.current(), ["a", "b", "c"])

    def test_state_file_with_wrong_shape_falls_back(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps([1, 2, 3]))  # a list, not a dict
        r = self._rotation(["a", "b", "c"])
        self.assertIn(r.current(), ["a", "b", "c"])

    def test_out_of_range_position_in_state_file_is_clamped(self) -> None:
        ids = ["a", "b", "c"]
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps({"order": ids, "position": 999, "pool_hash": _pool_hash(ids)})
        )
        r = self._rotation(ids)
        self.assertIn(r.current(), ids)  # 999 % 3 is a valid index, not a crash

    def test_state_written_atomically_no_leftover_tmp_files(self) -> None:
        self._rotation(["a", "b", "c"])
        leftovers = list(self.state_path.parent.glob(".tmp-*"))
        self.assertEqual(leftovers, [])

    def test_pool_hash_is_order_independent(self) -> None:
        self.assertEqual(_pool_hash(["a", "b", "c"]), _pool_hash(["c", "a", "b"]))

    def test_pool_hash_differs_on_membership_change(self) -> None:
        self.assertNotEqual(_pool_hash(["a", "b", "c"]), _pool_hash(["a", "b", "d"]))

    def test_len_reflects_current_order_size(self) -> None:
        r = self._rotation(["a", "b", "c", "d"])
        self.assertEqual(len(r), 4)


class ShuffleOptionTests(unittest.TestCase):
    """"Order: shuffle or in order"."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self._tmp.name) / "rotation_state.json"
        self.ids = [f"id{i}" for i in range(10)]

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _rotation(self, **kwargs) -> Rotation:
        return Rotation(
            self.ids, state_path=self.state_path, rng=random.Random(1), **kwargs
        )

    def test_shuffle_defaults_to_true_so_nothing_changes_for_old_callers(self) -> None:
        walked = [self._rotation().current()]
        rotation = self._rotation()
        walked = [rotation.current()] + [rotation.next_image() for _ in range(9)]
        self.assertNotEqual(walked, self.ids)
        self.assertEqual(sorted(walked), sorted(self.ids))

    def test_in_order_walks_the_sources_own_sequence(self) -> None:
        rotation = self._rotation(shuffle=False)
        walked = [rotation.current()] + [rotation.next_image() for _ in range(9)]
        self.assertEqual(walked, self.ids)

    def test_switching_to_in_order_reorders_immediately(self) -> None:
        """The next pool change may be thirty minutes away and the user
        is looking at the settings window now."""
        rotation = self._rotation(shuffle=True)
        rotation.set_shuffle(False)
        walked = [rotation.current()] + [rotation.next_image() for _ in range(9)]
        self.assertEqual(sorted(walked), sorted(self.ids))
        # In source order from wherever the held picture landed.
        start = self.ids.index(walked[0])
        self.assertEqual(walked, self.ids[start:] + self.ids[:start])

    def test_switching_order_never_changes_the_shown_picture(self) -> None:
        """Changing the *order* of the rotation must not behave like a
        Next button."""
        rotation = self._rotation(shuffle=True)
        before = rotation.current()
        rotation.set_shuffle(False)
        self.assertEqual(rotation.current(), before)
        rotation.set_shuffle(True)
        self.assertEqual(rotation.current(), before)

    def test_setting_the_same_value_is_a_no_op(self) -> None:
        """Checked on one rotation rather than two: two instances share
        the persisted state file, so the second would resume where the
        first left off and prove nothing about re-ordering."""
        rotation = self._rotation(shuffle=True)
        rotation.next_image()
        before_position = rotation.position()
        before_current = rotation.current()

        rotation.set_shuffle(True)

        self.assertEqual(rotation.position(), before_position)
        self.assertEqual(rotation.current(), before_current)

    def test_set_shuffle_on_an_empty_pool_does_not_raise(self) -> None:
        rotation = Rotation([], state_path=self.state_path, rng=random.Random(1))
        rotation.set_shuffle(False)
        self.assertIsNone(rotation.current())


class PreviousTests(unittest.TestCase):
    """Previous — Step 1."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self._tmp.name) / "rotation_state.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _rotation(self, ids, **kwargs) -> Rotation:
        return Rotation(ids, state_path=self.state_path, rng=random.Random(1), **kwargs)

    def test_previous_undoes_a_next(self) -> None:
        r = self._rotation(["a", "b", "c", "d"])
        first = r.current()
        r.next_image()
        self.assertEqual(r.previous(), first)

    def test_previous_at_position_zero_is_a_noop_not_a_wrap(self) -> None:
        """Wrapping to the end of the order would show a picture the user
        has *not* seen and call it "back" — the opposite of the control's
        meaning."""
        r = self._rotation(["a", "b", "c", "d"])
        first = r.current()
        self.assertEqual(r.previous(), first)
        self.assertEqual(r.previous(), first)
        self.assertEqual(r.position(), 1)

    def test_previous_does_not_un_reshuffle(self) -> None:
        """"Previous" is only ever defined within the *current*
        permutation. Walking off the end reshuffles in place, and the
        order that preceded it is gone — Previous must land at the start
        of the new order, not resurrect the old one."""
        ids = [f"id{i}" for i in range(4)]
        r = self._rotation(ids)
        for _ in range(len(ids)):  # walk past exhaustion into a reshuffle
            r.next_image()
        self.assertEqual(r.position(), 1)
        after_reshuffle = r.current()
        self.assertEqual(r.previous(), after_reshuffle)

    def test_previous_on_an_empty_pool_returns_none(self) -> None:
        self.assertIsNone(self._rotation([]).previous())

    def test_previous_persists_the_new_position(self) -> None:
        r = self._rotation(["a", "b", "c", "d"])
        r.next_image()
        r.next_image()
        r.previous()
        saved = json.loads(self.state_path.read_text())
        self.assertEqual(saved["position"], 1)

    def test_step_applies_a_signed_count(self) -> None:
        """The control channel's clamped delta, applied in one call."""
        r = self._rotation(["a", "b", "c", "d"])
        r.step(3)
        self.assertEqual(r.position(), 4)
        r.step(-2)
        self.assertEqual(r.position(), 2)
        r.step(0)
        self.assertEqual(r.position(), 2)


class PinTests(unittest.TestCase):
    """Pause, at the rotation layer — Step 1."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self._tmp.name) / "rotation_state.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _rotation(self, ids, **kwargs) -> Rotation:
        return Rotation(ids, state_path=self.state_path, rng=random.Random(1), **kwargs)

    def test_a_pinned_rotation_ignores_the_timer(self) -> None:
        """`next_image()` is the rotation *timer*'s entry point, and the
        pause is enforced here rather than only by stopping the timer —
        so a tick that slips through cannot move a paused picture."""
        r = self._rotation(["a", "b", "c", "d"])
        pinned = r.pin()
        for _ in range(5):
            self.assertEqual(r.next_image(), pinned)
        self.assertEqual(r.current(), pinned)

    def test_pin_moves_to_a_named_id(self) -> None:
        r = self._rotation(["a", "b", "c", "d"])
        self.assertEqual(r.pin("c"), "c")
        self.assertEqual(r.current(), "c")

    def test_pin_on_an_unknown_id_holds_the_current_picture(self) -> None:
        """A `Paused` label that lies is worse than pausing one picture
        away from the requested one."""
        r = self._rotation(["a", "b", "c", "d"])
        current = r.current()
        self.assertEqual(r.pin("not-in-the-pool"), current)
        self.assertTrue(r.is_pinned)

    def test_next_while_paused_moves_the_pin_without_resuming(self) -> None:
        """"Next/Previous while paused move that id
        one step — they never clear `paused`"."""
        r = self._rotation(["a", "b", "c", "d"])
        first = r.pin()
        moved = r.step_forward()

        self.assertNotEqual(moved, first)
        self.assertTrue(r.is_pinned)  # still paused
        self.assertEqual(r.pinned_id(), moved)
        # And the pause now holds the NEW picture, not the old one.
        self.assertEqual(r.next_image(), moved)

    def test_previous_while_paused_moves_the_pin_without_resuming(self) -> None:
        r = self._rotation(["a", "b", "c", "d"])
        r.next_image()
        r.next_image()
        pinned = r.pin()
        back = r.previous()

        self.assertNotEqual(back, pinned)
        self.assertTrue(r.is_pinned)
        self.assertEqual(r.pinned_id(), back)

    def test_unpin_resumes_from_the_pinned_picture_without_moving(self) -> None:
        r = self._rotation(["a", "b", "c", "d"])
        pinned = r.pin()
        r.unpin()

        self.assertFalse(r.is_pinned)
        self.assertEqual(r.current(), pinned)  # Resume does not jump
        self.assertNotEqual(r.next_image(), pinned)  # but rotation moves again

    def test_unpin_is_idempotent(self) -> None:
        r = self._rotation(["a", "b", "c", "d"])
        r.unpin()
        r.unpin()
        self.assertFalse(r.is_pinned)

    def test_pinned_id_is_none_while_running(self) -> None:
        r = self._rotation(["a", "b", "c"])
        self.assertIsNone(r.pinned_id())

    def test_a_pool_change_does_not_move_a_paused_picture(self) -> None:
        """A poll noticing one new file in the folder must not silently
        move a paused View onto a different picture — "restoring
        shows the same picture you left" applies to Pause too, and a
        reshuffle resets position to 0."""
        r = self._rotation(["a", "b", "c", "d"])
        r.next_image()
        pinned = r.pin()

        r.sync_pool(["a", "b", "c", "d", "e"])  # a new picture appears

        self.assertEqual(r.current(), pinned)
        self.assertTrue(r.is_pinned)

    def test_a_paused_picture_leaving_the_pool_does_not_raise(self) -> None:
        r = self._rotation(["a", "b", "c", "d"])
        pinned = r.pin()
        remaining = [i for i in ["a", "b", "c", "d"] if i != pinned]

        r.sync_pool(remaining)

        self.assertTrue(r.is_pinned)
        self.assertIn(r.current(), remaining)

    def test_pinning_an_empty_pool_does_not_raise(self) -> None:
        r = self._rotation([])
        self.assertIsNone(r.pin())
        self.assertIsNone(r.pin("anything"))
        self.assertIsNone(r.next_image())

    def test_the_pin_is_not_persisted_to_the_state_file(self) -> None:
        """Two durable sources of truth for "is it paused" would disagree
        the moment one is hand-edited. The command file is the
        one; app.py re-pins from it on startup."""
        r = self._rotation(["a", "b", "c", "d"])
        r.pin()
        saved = json.loads(self.state_path.read_text())
        self.assertEqual(set(saved), {"order", "position", "pool_hash"})

        resumed = self._rotation(["a", "b", "c", "d"])
        self.assertFalse(resumed.is_pinned)


class PositionTests(unittest.TestCase):
    """`Picture 12 of 47` fallback label needs a 1-based
    position."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self._tmp.name) / "rotation_state.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_position_is_one_based(self) -> None:
        r = Rotation(["a", "b", "c"], state_path=self.state_path, rng=random.Random(1))
        self.assertEqual(r.position(), 1)
        r.next_image()
        self.assertEqual(r.position(), 2)

    def test_position_is_zero_on_an_empty_pool(self) -> None:
        r = Rotation([], state_path=self.state_path, rng=random.Random(1))
        self.assertEqual(r.position(), 0)


if __name__ == "__main__":
    unittest.main()
