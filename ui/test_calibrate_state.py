"""Tests for `calibrate_state`.

Stdlib `unittest`, no AppKit, no window server: the whole point of the
`calibrate_state` / `calibrate_window` split is that everything here runs
on any machine, including over SSH, where the device work cannot.

The shipped numbers (center 483/482, radius 472, margin 0.93, 960x960)
appear throughout deliberately. They are the values on the real device,
so a test that uses them is testing the arithmetic at the point where it
actually has to be right rather than at a rounder one.
"""

from __future__ import annotations

import unittest

# Several tests below pin this module's arithmetic against the display's
# own — the clamp rules are a deliberate copy of the loader's range
# check, and the two ring radii have to agree with
# `Calibration.effective_radius_px` exactly. That costs
# nothing but a `from display... import`: both are real packages under
# the repo root, so no sys.path shim is needed to reach across.
from ui import calibrate_state as cs

BOUNDS = cs.Bounds(width=960.0, height=960.0)
SHIPPED = cs.CircleValues(center_x=483.0, center_y=482.0, radius_px=472.0)
MARGIN = 0.93


class CircleValuesTests(unittest.TestCase):
    def test_replace_returns_a_new_value(self) -> None:
        moved = SHIPPED.replace("radius_px", 400.0)
        self.assertEqual(moved.radius_px, 400.0)
        self.assertEqual(SHIPPED.radius_px, 472.0, "originals are frozen")

    def test_unknown_field_is_loud(self) -> None:
        """Field names come from this module's own FIELDS, so an unknown
        one is a programming error, not hostile input."""
        with self.assertRaises(KeyError):
            SHIPPED.replace("radius", 1.0)
        with self.assertRaises(KeyError):
            SHIPPED.get("nope")


class ClampTests(unittest.TestCase):
    def test_center_is_clamped_to_keep_the_radius_drawable(self) -> None:
        self.assertEqual(cs.clamp_center(10.0, 472.0, 960.0), 472.0)
        self.assertEqual(cs.clamp_center(950.0, 472.0, 960.0), 488.0)

    def test_center_with_an_impossible_radius_goes_to_the_middle(self) -> None:
        """No satisfying answer exists; centered-and-clipped reads as
        too-big, jammed-in-a-corner reads as a bug."""
        self.assertEqual(cs.clamp_center(10.0, 600.0, 960.0), 480.0)

    def test_radius_is_clamped_to_the_nearest_edge(self) -> None:
        # Center 483 in a 960 framebuffer: nearest edge is 960-483=477.
        self.assertEqual(cs.clamp_radius(600.0, 483.0, 482.0, BOUNDS), 477.0)

    def test_radius_has_a_floor(self) -> None:
        self.assertEqual(
            cs.clamp_radius(-50.0, 480.0, 480.0, BOUNDS), cs.MIN_RADIUS_PX
        )

    def test_clamp_values_clamps_radius_first(self) -> None:
        """Radius has a ceiling of its own, so clamping the center first
        could leave a radius with no legal center."""
        wild = cs.CircleValues(center_x=0.0, center_y=0.0, radius_px=5000.0)
        clamped = cs.clamp_values(wild, BOUNDS)
        self.assertEqual(clamped.radius_px, 480.0)
        self.assertEqual(clamped.center_x, 480.0)
        self.assertEqual(clamped.center_y, 480.0)

    def test_shipped_values_survive_clamping_unchanged(self) -> None:
        """The live calibration must be a fixed point of this function —
        if it were not, opening the window would silently move the
        circle on a device that is already correct."""
        self.assertEqual(cs.clamp_values(SHIPPED, BOUNDS), SHIPPED)

    def test_non_finite_values_do_not_propagate(self) -> None:
        """NaN in an NSRect draws nothing, silently — it never raises,
        so it has to be stopped here."""
        nasty = cs.CircleValues(
            center_x=float("nan"), center_y=float("inf"), radius_px=float("-inf")
        )
        clamped = cs.clamp_values(nasty, BOUNDS)
        for number in (clamped.center_x, clamped.center_y, clamped.radius_px):
            self.assertEqual(number, number)
            self.assertNotIn(number, (float("inf"), float("-inf")))


class SetFieldTests(unittest.TestCase):
    def test_moving_the_center_does_not_shrink_the_radius(self) -> None:
        """The decision in the module docstring, as a test. Radius is the
        number the user worked hardest for; the field being edited is the
        one that gives way."""
        moved = cs.set_field(SHIPPED, "center_x", 900.0, BOUNDS)
        self.assertEqual(moved.radius_px, 472.0)
        self.assertEqual(moved.center_x, 488.0)

    def test_a_non_number_leaves_the_value_alone(self) -> None:
        """Half-typed input: `4.` and `-` are not values yet."""
        for raw in ("", "-", "4.", "abc", None, True):
            self.assertEqual(
                cs.set_field(SHIPPED, "radius_px", raw, BOUNDS), SHIPPED, raw
            )

    def test_radius_gives_way_when_it_is_the_edited_field(self) -> None:
        grown = cs.set_field(SHIPPED, "radius_px", 900.0, BOUNDS)
        self.assertEqual(grown.radius_px, 477.0)
        self.assertEqual(grown.center_x, 483.0)


class NudgeTests(unittest.TestCase):
    def test_one_pixel(self) -> None:
        """Whole argument for fields over sliders: the 1px nudge
        the final pass needs."""
        self.assertEqual(
            cs.nudge(SHIPPED, "radius_px", cs.NUDGE_STEP, BOUNDS).radius_px, 473.0
        )

    def test_shift_nudge(self) -> None:
        self.assertEqual(
            cs.nudge(SHIPPED, "center_x", -cs.NUDGE_STEP_LARGE, BOUNDS).center_x,
            473.0,
        )

    def test_nudging_into_a_wall_stops(self) -> None:
        values = SHIPPED
        for _ in range(50):
            values = cs.nudge(values, "radius_px", 1.0, BOUNDS)
        self.assertEqual(values.radius_px, 477.0)


class RingGeometryTests(unittest.TestCase):
    def test_the_two_rings_are_the_plans_numbers(self) -> None:
        """472 -> 438.96, a 33-pixel gap. This is the arithmetic
        the entire two-ring requirement exists because of."""
        geometry = cs.ring_geometry(SHIPPED, MARGIN)
        self.assertEqual(geometry.outer_radius, 472.0)
        self.assertAlmostEqual(geometry.inner_radius, 438.96, places=2)
        self.assertAlmostEqual(geometry.gap_px, 33.04, places=2)

    def test_inner_ring_matches_the_displays_effective_radius(self) -> None:
        """The dim ring must be exactly where the display draws
        pictures, or gap is a lie in the other direction."""
        from display.calibration import Calibration

        calibration = Calibration(
            framebuffer_width=960.0,
            framebuffer_height=960.0,
            center_x=SHIPPED.center_x,
            center_y=SHIPPED.center_y,
            radius_px=SHIPPED.radius_px,
            safety_margin_pct=MARGIN,
        )
        self.assertAlmostEqual(
            cs.ring_geometry(SHIPPED, MARGIN).inner_radius,
            calibration.effective_radius_px,
            places=9,
        )

    def test_rects_are_centered(self) -> None:
        x, y, w, h = cs.ring_geometry(SHIPPED, MARGIN).outer_rect()
        self.assertEqual((x, y, w, h), (11.0, 10.0, 944.0, 944.0))

    def test_a_bad_margin_collapses_to_one_ring_rather_than_lying(self) -> None:
        for margin in (0.0, -0.5, 1.5, float("nan")):
            geometry = cs.ring_geometry(SHIPPED, margin)
            self.assertEqual(geometry.inner_radius, geometry.outer_radius, margin)


class SessionTests(unittest.TestCase):
    def _session(self) -> cs.CalibrationSession:
        return cs.CalibrationSession(
            saved=SHIPPED,
            defaults=cs.CircleValues(center_x=480.0, center_y=480.0, radius_px=460.0),
            bounds=BOUNDS,
            safety_margin_pct=MARGIN,
        )

    def test_a_fresh_session_is_clean(self) -> None:
        session = self._session()
        self.assertFalse(session.dirty)
        self.assertFalse(session.can_undo)
        self.assertFalse(session.can_redo)

    def test_a_nudge_makes_it_dirty(self) -> None:
        session = self._session()
        self.assertTrue(session.nudge("radius_px", 1.0))
        self.assertTrue(session.dirty)
        self.assertTrue(session.can_undo)

    def test_undoing_back_to_saved_is_not_dirty(self) -> None:
        """`dirty` is `values != saved`, not a flag — a flag would have
        to be cleared here and this is exactly where it would be
        forgotten."""
        session = self._session()
        session.nudge("radius_px", 1.0)
        session.nudge("radius_px", 1.0)
        session.undo()
        session.undo()
        self.assertFalse(session.dirty)

    def test_a_clamped_no_op_does_not_push_an_undo_entry(self) -> None:
        """Holding an arrow at the framebuffer edge must not build a
        stack of identical states that makes Cmd-Z appear broken."""
        session = self._session()
        for _ in range(100):
            session.nudge("radius_px", 1.0)
        depth_at_wall = len(session._undo)
        for _ in range(20):
            self.assertFalse(session.nudge("radius_px", 1.0))
        self.assertEqual(len(session._undo), depth_at_wall)

    def test_redo(self) -> None:
        session = self._session()
        session.nudge("center_x", 5.0)
        session.undo()
        self.assertTrue(session.can_redo)
        session.redo()
        self.assertEqual(session.values.center_x, 488.0)

    def test_a_new_edit_clears_the_redo_stack(self) -> None:
        session = self._session()
        session.nudge("center_x", 5.0)
        session.undo()
        session.nudge("center_y", 5.0)
        self.assertFalse(session.can_redo)

    def test_revert_to_saved_is_itself_undoable(self) -> None:
        """Revert and Reset sit next to each other, and the test expects
        people to hit the wrong one."""
        session = self._session()
        # Downward: 472 + 7 would clamp at the 477 edge for this center,
        # which would make the assertion below about the clamp rather
        # than about undo.
        session.nudge("radius_px", -7.0)
        session.revert_to_saved()
        self.assertFalse(session.dirty)
        session.undo()
        self.assertEqual(session.values.radius_px, 465.0)

    def test_reset_to_defaults_is_distinct_from_revert(self) -> None:
        session = self._session()
        session.nudge("radius_px", 3.0)
        session.reset_to_defaults()
        self.assertEqual(session.values.radius_px, 460.0)
        self.assertTrue(session.dirty, "defaults differ from what is saved")

    def test_mark_saved_keeps_the_undo_stack(self) -> None:
        """Save then Cmd-Z should walk back through the nudges and leave
        the session dirty again — which is true, and offers Save, which
        is what fixes it."""
        session = self._session()
        session.nudge("radius_px", 2.0)
        session.mark_saved()
        self.assertFalse(session.dirty)
        self.assertTrue(session.can_undo)
        session.undo()
        self.assertTrue(session.dirty)
        self.assertEqual(session.values, SHIPPED)

    def test_undo_depth_is_bounded(self) -> None:
        session = self._session()
        for i in range(cs.MAX_UNDO_DEPTH + 50):
            session.set_values(
                cs.CircleValues(center_x=400.0 + (i % 60), center_y=482.0, radius_px=400.0)
            )
        self.assertLessEqual(len(session._undo), cs.MAX_UNDO_DEPTH)

    def test_preview_payload_carries_the_margin(self) -> None:
        """So the display's inner circle is computed from the same margin
        this window drew its dim ring with."""
        session = self._session()
        payload = session.preview_payload()
        self.assertEqual(
            set(payload), {"center_x", "center_y", "radius_px", "safety_margin_pct"}
        )
        self.assertEqual(payload["safety_margin_pct"], MARGIN)

    def test_preview_payload_keys_match_what_the_display_accepts(self) -> None:
        """The control channel allow-lists preview keys. A key this
        window sends that `control` drops would be a nudge that silently
        does nothing on the device."""
        from display.control import PREVIEW_CALIBRATION_KEYS

        self.assertEqual(
            set(self._session().preview_payload()), set(PREVIEW_CALIBRATION_KEYS)
        )

    def test_out_of_range_saved_values_are_clamped_on_construction(self) -> None:
        session = cs.CalibrationSession(
            saved=cs.CircleValues(center_x=-100.0, center_y=5000.0, radius_px=9000.0),
            defaults=SHIPPED,
            bounds=BOUNDS,
            safety_margin_pct=MARGIN,
        )
        self.assertFalse(session.dirty, "clamped values are the saved ones")
        self.assertEqual(session.values.radius_px, 480.0)


class DocumentTests(unittest.TestCase):
    LIVE = {
        "schema_version": 1,
        "framebuffer": {"width": 960, "height": 960},
        "target_screen": {"resolve_strategy": "match_by_resolution_excluding_main"},
        "circle": {"center_x": 483.0, "center_y": 482.0, "radius_px": 472.0},
        "safety_margin_pct": 0.93,
        "calibration_source": {"method": "test-pattern-photo", "measured_by": "owner"},
    }

    def test_reads_the_live_document(self) -> None:
        self.assertEqual(cs.circle_from_document(self.LIVE), SHIPPED)
        self.assertEqual(cs.bounds_from_document(self.LIVE, cs.Bounds(1.0, 1.0)), BOUNDS)

    def test_unusable_documents_fall_back_rather_than_raising(self) -> None:
        fallback = cs.Bounds(7.0, 7.0)
        for data in (None, [], {}, {"circle": 3}, {"circle": {"center_x": "x"}},
                     {"circle": {"center_x": 1.0, "center_y": 1.0, "radius_px": float("nan")}}):
            self.assertIsNone(cs.circle_from_document(data), data)
            self.assertEqual(cs.bounds_from_document(data, fallback), fallback, data)

    def test_a_zero_framebuffer_is_rejected(self) -> None:
        fallback = cs.Bounds(7.0, 7.0)
        self.assertEqual(
            cs.bounds_from_document({"framebuffer": {"width": 0, "height": 960}}, fallback),
            fallback,
        )

    def test_save_preserves_unknown_keys(self) -> None:
        """V1 is additive-only, and this file is shared with an
        independently-versioned second app."""
        previous = dict(self.LIVE, future_key={"written_by": "another-tool"})
        document = cs.calibration_document(
            cs.CircleValues(center_x=484.0, center_y=481.0, radius_px=470.0),
            safety_margin_pct=MARGIN,
            bounds=BOUNDS,
            previous=previous,
        )
        self.assertEqual(document["future_key"], {"written_by": "another-tool"})
        self.assertEqual(
            document["target_screen"],
            {"resolve_strategy": "match_by_resolution_excluding_main"},
        )

    def test_save_drops_stale_provenance(self) -> None:
        """`calibration_source` names a photograph of the *old* numbers;
        the moment this window writes new ones it is a false statement
        about the values beside it."""
        document = cs.calibration_document(
            SHIPPED, safety_margin_pct=MARGIN, bounds=BOUNDS, previous=self.LIVE
        )
        self.assertNotIn("calibration_source", document)

    def test_the_written_document_loads_back_identically(self) -> None:
        """The round trip that matters: what this window saves must be
        what the display reads, with no drift through the schema."""
        from display.calibration import validate_calibration

        values = cs.CircleValues(center_x=484.0, center_y=479.5, radius_px=468.0)
        document = cs.calibration_document(
            values, safety_margin_pct=MARGIN, bounds=BOUNDS, previous=self.LIVE
        )
        loaded = validate_calibration(document)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.center_x, 484.0)
        self.assertEqual(loaded.center_y, 479.5)
        self.assertEqual(loaded.radius_px, 468.0)
        self.assertEqual(loaded.safety_margin_pct, MARGIN)
        self.assertEqual(document["schema_version"], cs.SCHEMA_VERSION)

    def test_anything_the_session_can_produce_is_accepted_by_the_loader(self) -> None:
        """The clamp rules in this module are a copy of the loader's
        range check (see `clamp_radius`'s docstring). This is the pin
        that keeps the copy honest — a window that can produce a value
        the display rejects would save a file that silently reverts the
        circle to the fallback."""
        from display.calibration import validate_calibration

        corners = [
            cs.CircleValues(0.0, 0.0, 9000.0),
            cs.CircleValues(9000.0, 9000.0, 9000.0),
            cs.CircleValues(0.0, 480.0, 1.0),
            cs.CircleValues(480.0, 0.0, -5.0),
            cs.CircleValues(959.0, 1.0, 480.0),
        ]
        for raw in corners:
            values = cs.clamp_values(raw, BOUNDS)
            document = cs.calibration_document(
                values, safety_margin_pct=MARGIN, bounds=BOUNDS
            )
            self.assertIsNotNone(validate_calibration(document), raw)


class FormatTests(unittest.TestCase):
    def test_whole_numbers_lose_the_decimal(self) -> None:
        self.assertEqual(cs.format_value(472.0), "472")
        self.assertEqual(cs.format_value(438.96), "438.96")
        self.assertEqual(cs.format_value(479.5), "479.5")

    def test_junk_formats_as_zero_rather_than_raising(self) -> None:
        self.assertEqual(cs.format_value(float("nan")), "0")


if __name__ == "__main__":
    unittest.main()
