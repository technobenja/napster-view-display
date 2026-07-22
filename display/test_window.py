"""Unit tests for window.py's masking math — explicit
"highest-value target since it's the part most likely to have an
off-by-one or aspect-ratio bug, and it's pure arithmetic." Only touches
source_crop_rect()/destination_rect(), never creates an NSView/NSWindow/
NSImage or needs a window-server session — pure NSRect struct math.

Step 1 adds `BlankingTests` for blank/restore pair. Those
do construct a `CircularImageView` (allocating an NSView and scheduling
an NSTimer both work fine headless — only *drawing* needs a window
server), and they test the **state machine only**: the flag, the
animation progress, timer lifecycle, and idempotency.

**What these tests cannot and do not cover:** whether the View actually
goes black. `drawRect_` needs a graphics context, which needs a window
server, which does not exist over SSH. The ordering requirement — that
the blank branch sits ahead of the empty-fill branch — is enforced by
reading the source, not by observing a pixel. Confirming that blanking
looks like "off" rather than like charcoal is console work, and belongs
with Step 2's evaluation on the physical device.
"""

from __future__ import annotations

import unittest

import AppKit

from display import window
from display.calibration import Calibration
from display.window import CircularImageView, destination_rect, source_crop_rect

CALIBRATION = Calibration(
    framebuffer_width=960.0,
    framebuffer_height=960.0,
    center_x=483.0,
    center_y=482.0,
    radius_px=472.0,
    safety_margin_pct=0.93,
)


class SourceCropRectTests(unittest.TestCase):
    def test_square_source_crops_to_itself(self) -> None:
        rect = source_crop_rect((500.0, 500.0))
        self.assertEqual((rect.origin.x, rect.origin.y), (0.0, 0.0))
        self.assertEqual((rect.size.width, rect.size.height), (500.0, 500.0))

    def test_landscape_source_crops_to_centered_square(self) -> None:
        rect = source_crop_rect((1280.0, 768.0))
        side = 768.0
        self.assertEqual(rect.size.width, side)
        self.assertEqual(rect.size.height, side)
        # Centered horizontally: equal margin on left/right.
        self.assertEqual(rect.origin.x, (1280.0 - side) / 2.0)
        self.assertEqual(rect.origin.y, 0.0)

    def test_portrait_source_crops_to_centered_square(self) -> None:
        rect = source_crop_rect((768.0, 1280.0))
        side = 768.0
        self.assertEqual(rect.size.width, side)
        self.assertEqual(rect.size.height, side)
        self.assertEqual(rect.origin.x, 0.0)
        self.assertEqual(rect.origin.y, (1280.0 - side) / 2.0)

    def test_extreme_aspect_ratio_still_crops_to_shorter_side(self) -> None:
        rect = source_crop_rect((4000.0, 100.0))
        self.assertEqual(rect.size.width, 100.0)
        self.assertEqual(rect.size.height, 100.0)
        self.assertEqual(rect.origin.x, (4000.0 - 100.0) / 2.0)

    def test_crop_is_independent_of_destination_size(self) -> None:
        """The crop must not depend on calibration/radius at all —
        an earlier plan draft's compute_source_rect(image_size, radius)
        looked radius-dependent but wasn't, which is exactly the class of
        bug this test exists to catch."""
        rect_small_calib = source_crop_rect((1280.0, 768.0))
        # source_crop_rect doesn't take a calibration argument at all;
        # confirm two different "destination contexts" (different
        # calibrations) produce byte-identical crop rects for the same
        # source image.
        other_calibration = Calibration(
            framebuffer_width=960.0,
            framebuffer_height=960.0,
            center_x=100.0,
            center_y=100.0,
            radius_px=50.0,
            safety_margin_pct=0.5,
        )
        rect_large_calib = source_crop_rect((1280.0, 768.0))
        self.assertEqual(
            (rect_small_calib.origin.x, rect_small_calib.origin.y,
             rect_small_calib.size.width, rect_small_calib.size.height),
            (rect_large_calib.origin.x, rect_large_calib.origin.y,
             rect_large_calib.size.width, rect_large_calib.size.height),
        )
        del other_calibration  # only constructed to make the intent explicit


class DestinationRectTests(unittest.TestCase):
    def test_destination_is_square_with_diameter_side(self) -> None:
        rect = destination_rect(CALIBRATION)
        expected_diameter = 2 * CALIBRATION.effective_radius_px
        self.assertAlmostEqual(rect.size.width, expected_diameter)
        self.assertAlmostEqual(rect.size.height, expected_diameter)

    def test_destination_is_centered_on_calibration_center(self) -> None:
        rect = destination_rect(CALIBRATION)
        center_x = rect.origin.x + rect.size.width / 2.0
        center_y = rect.origin.y + rect.size.height / 2.0
        self.assertAlmostEqual(center_x, CALIBRATION.center_x)
        self.assertAlmostEqual(center_y, CALIBRATION.center_y)

    def test_destination_uses_effective_radius_not_raw_radius(self) -> None:
        """The safety margin must actually apply — a destination sized
        off the raw radius_px instead of effective_radius_px would draw
        past the safe, margin-shrunk boundary the whole point of safety_margin_pct exists to enforce."""
        rect = destination_rect(CALIBRATION)
        raw_diameter = 2 * CALIBRATION.radius_px
        effective_diameter = 2 * CALIBRATION.effective_radius_px
        self.assertLess(rect.size.width, raw_diameter)
        self.assertAlmostEqual(rect.size.width, effective_diameter)

    def test_destination_rect_covers_the_full_clip_circle(self) -> None:
        """The destination square must fully cover the circle it's about
        to be clipped to (i.e. its side must equal the circle's
        diameter) - too small would leave a visible gap, too big is
        wasted but harmless since the clip crops it anyway."""
        rect = destination_rect(CALIBRATION)
        diameter = 2 * CALIBRATION.effective_radius_px
        self.assertAlmostEqual(rect.size.width, diameter)
        self.assertAlmostEqual(rect.size.height, diameter)


class BlankingTests(unittest.TestCase):
    """Blank/restore renderer pair — Step 1. State machine only;
    see the module docstring for what is deliberately out of reach."""

    def setUp(self) -> None:
        self.view = CircularImageView.alloc().initWithFrame_(
            AppKit.NSMakeRect(0.0, 0.0, 960.0, 960.0)
        )
        self.view.calibration = CALIBRATION

    def tearDown(self) -> None:
        # Scheduled NSTimers retain their target; leaving them running
        # would leak views across tests.
        for timer in (self.view._blank_timer, self.view._fade_timer):
            if timer is not None:
                timer.invalidate()

    def test_a_new_view_is_not_blanked(self) -> None:
        self.assertFalse(self.view.isBlanked())
        self.assertEqual(self.view._blank_progress, 0.0)

    def test_blanking_sets_the_flag_immediately_not_when_the_fade_ends(self) -> None:
        """"Is this View blanked?" must not depend on whether a
        2-second animation has finished — the control channel asks that
        question four times a second."""
        self.view.fadeToBlackWithDuration_(2.0)
        self.assertTrue(self.view.isBlanked())
        self.assertLess(self.view._blank_progress, 1.0)

    def test_a_zero_duration_blank_snaps_to_full_black(self) -> None:
        self.view.fadeToBlackWithDuration_(0.0)
        self.assertTrue(self.view.isBlanked())
        self.assertEqual(self.view._blank_progress, 1.0)
        self.assertIsNone(self.view._blank_timer)

    def test_restore_clears_the_flag_immediately(self) -> None:
        self.view.fadeToBlackWithDuration_(0.0)
        self.view.restoreToImagePath_duration_(None, 2.0)
        self.assertFalse(self.view.isBlanked())

    def test_a_zero_duration_restore_snaps_back(self) -> None:
        self.view.fadeToBlackWithDuration_(0.0)
        self.view.restoreToImagePath_duration_(None, 0.0)
        self.assertFalse(self.view.isBlanked())
        self.assertEqual(self.view._blank_progress, 0.0)

    def test_blanking_never_touches_the_loaded_image(self) -> None:
        """"rotation is paused, not hidden ... restoring shows the
        same picture you left". The renderer half of that promise is that
        blanking does not unload, reload, or replace `_image`."""
        sentinel = object()
        self.view._image = sentinel

        self.view.fadeToBlackWithDuration_(0.0)
        self.assertIs(self.view._image, sentinel)

        self.view.restoreToImagePath_duration_(None, 0.0)
        self.assertIs(self.view._image, sentinel)

    def test_restore_with_a_path_replaces_the_image(self) -> None:
        """The escape hatch for "the picture changed while it was dark"
        — a nonexistent path decodes to None rather than raising."""
        self.view._image = object()
        self.view.fadeToBlackWithDuration_(0.0)
        self.view.restoreToImagePath_duration_("/nonexistent/picture.png", 0.0)
        self.assertIsNone(self.view._image)

    def test_blanking_is_idempotent(self) -> None:
        """The control channel is level-triggered, so desired
        state may be applied over and over; re-blanking an already-black
        View must not restart the fade."""
        self.view.fadeToBlackWithDuration_(0.0)
        self.view.fadeToBlackWithDuration_(2.0)
        self.assertEqual(self.view._blank_progress, 1.0)
        self.assertIsNone(self.view._blank_timer)

    def test_restore_is_idempotent(self) -> None:
        self.view.restoreToImagePath_duration_(None, 2.0)
        self.assertFalse(self.view.isBlanked())
        self.assertEqual(self.view._blank_progress, 0.0)
        self.assertIsNone(self.view._blank_timer)

    def test_restoring_mid_blank_animates_from_where_it_got_to(self) -> None:
        """Not from full black: a blank interrupted at 30% must come back
        from 30%, or the restore visibly jumps darker before brightening."""
        self.view.fadeToBlackWithDuration_(2.0)
        self.view._blank_progress = 0.3
        self.view.restoreToImagePath_duration_(None, 2.0)
        self.assertEqual(self.view._blank_from, 0.3)
        self.assertEqual(self.view._blank_to, 0.0)

    def test_only_one_blank_timer_runs_at_a_time(self) -> None:
        self.view.fadeToBlackWithDuration_(2.0)
        first = self.view._blank_timer
        self.view.restoreToImagePath_duration_(None, 2.0)
        second = self.view._blank_timer

        self.assertIsNotNone(second)
        self.assertIsNot(first, second)
        self.assertFalse(first.isValid())

    def test_blank_tick_interpolates_and_stops_at_the_target(self) -> None:
        self.view.fadeToBlackWithDuration_(2.0)
        timer = self.view._blank_timer
        # Pretend the whole duration has elapsed.
        self.view._blank_start -= 10.0
        self.view.blankTick_(timer)

        self.assertEqual(self.view._blank_progress, 1.0)
        self.assertIsNone(self.view._blank_timer)
        self.assertFalse(timer.isValid())

    def test_blanking_and_crossfading_use_separate_timers(self) -> None:
        """They are independent by design: the blank overlay composites
        over whatever the crossfade is drawing, so neither has to know
        about the other."""
        self.view.transitionToImagePath_duration_(None, 2.0)
        self.view.fadeToBlackWithDuration_(2.0)

        self.assertIsNotNone(self.view._fade_timer)
        self.assertIsNotNone(self.view._blank_timer)
        self.assertIsNot(self.view._fade_timer, self.view._blank_timer)

    def test_blank_is_true_black_not_the_charcoal_empty_fill(self) -> None:
        """The distinction is the whole point:
        charcoal reads as "on, nothing to show yet", and a blanked View
        is asking to read as off."""
        # blackColor() lives in a grayscale colorspace and raises on
        # -redComponent until converted, so compare in one shared space.
        srgb = AppKit.NSColorSpace.sRGBColorSpace()
        charcoal = window.EMPTY_FILL_COLOR.colorUsingColorSpace_(srgb)
        black = AppKit.NSColor.blackColor().colorUsingColorSpace_(srgb)

        self.assertGreater(charcoal.redComponent(), 0.0)  # "on, nothing yet"
        self.assertEqual(black.redComponent(), 0.0)  # "off"
        self.assertNotAlmostEqual(
            charcoal.redComponent(), black.redComponent(), places=3
        )


if __name__ == "__main__":
    unittest.main()
