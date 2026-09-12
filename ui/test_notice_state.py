"""The geometry behind the contended-launch notice.

`ui/test_notice_window.py` covers the `NSPanel` itself. Everything here
is a pure function of rectangles and a `Notice`, which is the whole
reason the split exists: placement is the part of Phase 4b that can be
wrong in a way nobody notices — a window on the wrong screen still looks
like a window — and it is the only part a headless run can check.

Two of these tests exist because the plan was wrong about them, and both
say so where they assert it.
"""

from __future__ import annotations

import unittest

from ui import contention_state as cs
from ui import notice_state as ns

#: The live geometry of the machine this was written on, MEASURED
#: 2026-09-10 from `NSScreen.screens()`. Screen 0 is the desk monitor
#: with a Dock on its left edge; screen 1 is the 2.1-inch round picture
#: display, which sits above it in Cocoa coordinates.
DESK = ns.Screen(
    frame=ns.Rect(0.0, 0.0, 1600.0, 1000.0),
    visible_frame=ns.Rect(63.0, 0.0, 1537.0, 975.0),
)
VIEW = ns.Screen(
    frame=ns.Rect(313.0, 1000.0, 960.0, 960.0),
    visible_frame=ns.Rect(313.0, 1000.0, 960.0, 935.0),
)
ZERO_HEIGHT = 1000.0

#: MEASURED in the same call: the holder owns one status item on the menu
#: bar and one on the picture display, and the second reports
#: `kCGWindowIsOnscreen: True` while buried under a 960x960 layer-1000
#: window. These are window-server (Quartz) coordinates.
MENU_BAR_ICON = cs.WindowFact(layer=25, on_screen=True, x=1169.0, y=0.0, width=34.0, height=24.0)
VIEW_ICON = cs.WindowFact(layer=25, on_screen=True, x=842.0, y=-960.0, width=34.0, height=24.0)
PICTURE = cs.WindowFact(
    layer=1000, on_screen=True, x=313.0, y=-960.0, width=960.0, height=960.0
)

PANEL = (ns.PANEL_WIDTH, 220.0)


def notice(verdict=cs.Verdict.UNRESPONSIVE, **overrides) -> cs.Notice:
    base = dict(
        verdict=verdict,
        headline="ImageView is not responding",
        body="ImageView (pid 4321) last reported working 37s ago.",
        holder_pid=4321,
        display_pid=4300,
        heartbeat_age_s=37.0,
        window_layers=(25,),
        state_writable=True,
        stacks_path="/tmp/viewlab/ui.stacks.log",
    )
    base.update(overrides)
    return cs.Notice(**base)


class RectTests(unittest.TestCase):
    def test_the_derived_edges(self) -> None:
        rect = ns.Rect(10.0, 20.0, 100.0, 50.0)
        self.assertEqual(rect.max_x, 110.0)
        self.assertEqual(rect.max_y, 70.0)
        self.assertEqual(rect.center_x, 60.0)
        self.assertEqual(rect.center_y, 45.0)

    def test_containment_is_inclusive_on_every_edge(self) -> None:
        """A status item sitting exactly on a screen's origin is on that
        screen. An exclusive test answers that it is on no screen at all,
        which routes the one case with the simplest geometry to the
        fallback."""
        rect = ns.Rect(0.0, 0.0, 100.0, 100.0)
        for x, y in ((0.0, 0.0), (100.0, 100.0), (0.0, 100.0), (50.0, 50.0)):
            with self.subTest(f"{x},{y}"):
                self.assertTrue(rect.contains_point(x, y))
        self.assertFalse(rect.contains_point(-0.5, 50.0))
        self.assertFalse(rect.contains_point(50.0, 100.5))

    def test_containment_is_of_the_centre(self) -> None:
        outer = ns.Rect(0.0, 0.0, 100.0, 100.0)
        self.assertTrue(outer.contains_center_of(ns.Rect(80.0, 80.0, 40.0, 40.0)))
        self.assertFalse(outer.contains_center_of(ns.Rect(99.0, 0.0, 40.0, 10.0)))


class CoordinateTests(unittest.TestCase):
    """Quartz measures down from the top of the zero-origin screen;
    `NSWindow` measures up from its bottom. Getting this backwards puts
    every off-main-screen window in the wrong place, silently."""

    def test_the_measured_menu_bar_icon(self) -> None:
        rect = ns.rect_of(MENU_BAR_ICON, ZERO_HEIGHT)
        self.assertEqual(rect.x, 1169.0)
        self.assertEqual(rect.y, 976.0)
        self.assertEqual(rect.max_y, 1000.0, "the icon's top edge is the top of the screen")

    def test_the_measured_icon_on_the_picture_display(self) -> None:
        """A negative Quartz y is a screen *above* the zero-origin one."""
        rect = ns.rect_of(VIEW_ICON, ZERO_HEIGHT)
        self.assertEqual(rect.y, 1936.0)
        self.assertTrue(VIEW.frame.contains_center_of(rect))
        self.assertFalse(DESK.frame.contains_center_of(rect))

    def test_the_picture_window_lands_on_the_picture_display(self) -> None:
        rect = ns.rect_of(PICTURE, ZERO_HEIGHT)
        self.assertEqual((rect.x, rect.y, rect.width, rect.height), (313.0, 1000.0, 960.0, 960.0))
        self.assertTrue(VIEW.frame.contains_center_of(rect))


class AnchorTests(unittest.TestCase):
    def test_the_panel_is_centred_under_the_icon(self) -> None:
        icon = ns.Rect(800.0, 976.0, 34.0, 24.0)
        x, _ = ns.anchor_for(icon, DESK.visible_frame, PANEL)
        self.assertAlmostEqual(x + PANEL[0] / 2.0, icon.center_x)

    def test_the_top_edge_sits_below_the_menu_bar(self) -> None:
        """`visibleFrame` already excludes the menu bar, so the gap is
        measured from it rather than from a menu bar height this code
        would otherwise have to know."""
        icon = ns.Rect(800.0, 976.0, 34.0, 24.0)
        _, y = ns.anchor_for(icon, DESK.visible_frame, PANEL)
        self.assertEqual(y + PANEL[1], DESK.visible_frame.max_y - ns.MENU_BAR_GAP)

    def test_it_is_clamped_at_the_right_edge(self) -> None:
        icon = ns.Rect(DESK.visible_frame.max_x - 34.0, 976.0, 34.0, 24.0)
        x, _ = ns.anchor_for(icon, DESK.visible_frame, PANEL)
        self.assertEqual(x + PANEL[0], DESK.visible_frame.max_x - ns.EDGE_MARGIN)

    def test_it_is_clamped_at_the_left_edge(self) -> None:
        icon = ns.Rect(DESK.visible_frame.x, 976.0, 34.0, 24.0)
        x, _ = ns.anchor_for(icon, DESK.visible_frame, PANEL)
        self.assertEqual(x, DESK.visible_frame.x + ns.EDGE_MARGIN)

    def test_the_icon_centre_is_always_over_the_panel(self) -> None:
        """🔴 The property the placement's meaning rests on: the panel is
        under the icon, so the copy never has to name a coordinate.

        Swept across the whole width of the screen, including both
        extremes, because the clamp is the only thing that can break it.
        """
        visible = DESK.visible_frame
        x = visible.x
        while x <= visible.max_x - 34.0:
            icon = ns.Rect(x, 976.0, 34.0, 24.0)
            origin_x, _ = ns.anchor_for(icon, visible, PANEL)
            with self.subTest(f"icon at {x}"):
                self.assertGreaterEqual(icon.center_x, origin_x)
                self.assertLessEqual(icon.center_x, origin_x + PANEL[0])
            x += 17.0

    def test_the_whole_icon_is_over_the_panel_when_it_is_inset(self) -> None:
        """The stronger claim, which holds for every real menu bar item:
        the menu bar has its own trailing padding, so a status item is
        never flush with the screen edge."""
        visible = DESK.visible_frame
        for x in (visible.x + ns.EDGE_MARGIN, visible.max_x - ns.EDGE_MARGIN - 34.0, 900.0):
            icon = ns.Rect(x, 976.0, 34.0, 24.0)
            origin_x, _ = ns.anchor_for(icon, visible, PANEL)
            with self.subTest(f"icon at {x}"):
                self.assertGreaterEqual(icon.x, origin_x)
                self.assertLessEqual(icon.max_x, origin_x + PANEL[0])

    def test_an_icon_flush_with_the_edge_overhangs_by_the_margin(self) -> None:
        """🔴 **The plan is wrong here, and this pins how wrong.** It
        says "with a 34 pt icon and a 360 pt panel the icon stays within
        the panel's horizontal span even when clamped hard to either
        edge". The *centre* does, always — the test above. The icon's
        full span does not: an icon flush with the screen edge overhangs
        the clamped panel by exactly `EDGE_MARGIN`, on both sides. The
        placement still reads correctly, because the icon still sits over
        the panel; the plan's sentence simply claims more than the
        geometry gives."""
        visible = DESK.visible_frame
        right = ns.Rect(visible.max_x - 34.0, 976.0, 34.0, 24.0)
        origin_x, _ = ns.anchor_for(right, visible, PANEL)
        self.assertEqual(right.max_x - (origin_x + PANEL[0]), ns.EDGE_MARGIN)

        left = ns.Rect(visible.x, 976.0, 34.0, 24.0)
        origin_x, _ = ns.anchor_for(left, visible, PANEL)
        self.assertEqual(origin_x - left.x, ns.EDGE_MARGIN)

    def test_a_panel_wider_than_the_screen_is_centred(self) -> None:
        """The clamp range inverts; `min`/`max` would answer something
        arbitrary and off-screen on one side."""
        narrow = ns.Rect(0.0, 0.0, 200.0, 900.0)
        icon = ns.Rect(10.0, 880.0, 34.0, 24.0)
        x, _ = ns.anchor_for(icon, narrow, PANEL)
        self.assertAlmostEqual(x + PANEL[0] / 2.0, narrow.center_x)

    def test_a_panel_taller_than_the_screen_keeps_its_top_on_screen(self) -> None:
        """The headline and the first lines of the body are at the top.
        Losing the bottom of a message is recoverable; losing the top of
        it is not."""
        short = ns.Rect(0.0, 0.0, 1000.0, 150.0)
        icon = ns.Rect(500.0, 130.0, 34.0, 24.0)
        _, y = ns.anchor_for(icon, short, (ns.PANEL_WIDTH, 400.0))
        self.assertEqual(y + 400.0, short.max_y)

    def test_a_panel_that_only_just_fits_keeps_its_bottom_margin(self) -> None:
        """Between "fits with the gap" and "does not fit at all" there is
        a band where the gap is given up and the margin is not."""
        screen = ns.Rect(0.0, 0.0, 1000.0, 300.0)
        _, y = ns.anchor_for(ns.Rect(500.0, 280.0, 34.0, 24.0), screen, (ns.PANEL_WIDTH, 285.0))
        self.assertEqual(y, screen.y + ns.EDGE_MARGIN)

    def test_the_centred_fallback_sits_at_the_same_height(self) -> None:
        """So that "there was an icon" and "there was not" do not read as
        two different features."""
        icon = ns.Rect(800.0, 976.0, 34.0, 24.0)
        _, anchored_y = ns.anchor_for(icon, DESK.visible_frame, PANEL)
        x, y = ns.centred_origin(DESK.visible_frame, PANEL)
        self.assertEqual(y, anchored_y)
        self.assertAlmostEqual(x + PANEL[0] / 2.0, DESK.visible_frame.center_x)


class PointableTests(unittest.TestCase):
    def test_nothing_to_point_at(self) -> None:
        self.assertFalse(ns.icon_is_pointable(None, DESK.frame))

    def test_a_degenerate_rectangle_is_not_a_place(self) -> None:
        for rect in (
            ns.Rect(1169.0, 976.0, 0.0, 24.0),
            ns.Rect(1169.0, 976.0, 34.0, 0.0),
            ns.Rect(1169.0, 976.0, -34.0, 24.0),
        ):
            with self.subTest(str(rect)):
                self.assertFalse(ns.icon_is_pointable(rect, DESK.frame))

    def test_bounds_on_no_screen_at_all(self) -> None:
        """A window server answer this app cannot place. It must fall
        back rather than push the panel off into nothing."""
        self.assertFalse(ns.icon_is_pointable(ns.Rect(-5000.0, -5000.0, 34.0, 24.0), DESK.frame))

    def test_a_real_menu_bar_icon_is_pointable(self) -> None:
        self.assertTrue(ns.icon_is_pointable(ns.rect_of(MENU_BAR_ICON, ZERO_HEIGHT), DESK.frame))

    def test_the_docstring_still_says_it_is_unmeasured(self) -> None:
        """🔴 The caveat is the deliverable here, not decoration. Whether
        a notch-hidden status item still reports an on-screen window — and
        with what bounds — has never been measured, because it needs a Mac
        with a notch and no session has had one. A future reader who finds
        this function looking tidy and confident, with the caveat quietly
        gone, would build on an answer nobody has."""
        text = ns.icon_is_pointable.__doc__ or ""
        self.assertIn("UNMEASURED", text)
        self.assertIn("notch", text)


class ScreenChoiceTests(unittest.TestCase):
    def test_no_screens_at_all(self) -> None:
        self.assertEqual(ns.allowed_screens(()), ())
        self.assertIsNone(ns.placement_for((), (), (), PANEL))

    def test_the_picture_display_is_dropped(self) -> None:
        """🔴 MEASURED: macOS gives a status item a window on **every**
        screen, and one of this app's screens is a 2.1-inch round panel
        with a picture on it. The evidence that a screen is that one is
        the display agent's own window, not a resolution or a display id
        — `display_target` can be configured to treat any screen as the
        View, and a geometry heuristic here would disagree with it the
        moment someone changed the setting."""
        allowed = ns.allowed_screens((DESK, VIEW), (ns.rect_of(PICTURE, ZERO_HEIGHT),))
        self.assertEqual(allowed, (0,))

    def test_nothing_is_dropped_when_the_display_owns_no_windows(self) -> None:
        self.assertEqual(ns.allowed_screens((DESK, VIEW), ()), (0, 1))

    def test_every_screen_excluded_retreats_to_the_first(self) -> None:
        """Documented retreat: showing nothing at all is worse than
        showing it where a person is most likely looking."""
        everywhere = (ns.rect_of(PICTURE, ZERO_HEIGHT), ns.Rect(0.0, 0.0, 1600.0, 1000.0))
        self.assertEqual(ns.allowed_screens((DESK, VIEW), everywhere), (0,))


class PlacementTests(unittest.TestCase):
    SCREENS = (DESK, VIEW)
    AVOID = (ns.rect_of(PICTURE, ZERO_HEIGHT),)

    def placement(self, icons):
        return ns.placement_for(
            [ns.rect_of(icon, ZERO_HEIGHT) for icon in icons], self.AVOID, self.SCREENS, PANEL
        )

    def test_the_measured_live_case(self) -> None:
        """The real geometry, end to end: the icon at Quartz (1169, 0)
        puts a 360x220 panel at Cocoa (1006, 747) on the desk monitor."""
        got = self.placement([MENU_BAR_ICON])
        self.assertEqual(got.screen_index, 0)
        self.assertEqual(got.x, 1006.0)
        self.assertEqual(got.y, 747.0)
        self.assertTrue(got.anchored)

    def test_an_icon_only_on_the_picture_display_never_places_it_there(self) -> None:
        """🔴 The failure this exists to prevent: a notice on a 2.1-inch
        round display, underneath the slideshow, with half of it outside
        the circular mask. 4a's `visible_status_items` drops that icon
        before this is called; this asserts what happens if one reaches
        here anyway."""
        got = self.placement([VIEW_ICON])
        self.assertEqual(got.screen_index, 0)
        self.assertFalse(got.anchored)
        self.assertAlmostEqual(got.x + PANEL[0] / 2.0, DESK.visible_frame.center_x)

    def test_no_icons_centres_on_the_first_allowed_screen(self) -> None:
        got = self.placement([])
        self.assertEqual(got.screen_index, 0)
        self.assertFalse(got.anchored)

    def test_an_icon_on_a_second_ordinary_screen_is_used(self) -> None:
        """A second desk monitor is not the picture display, and the
        holder's icon may legitimately be there."""
        second = ns.Screen(
            frame=ns.Rect(1600.0, 0.0, 1200.0, 800.0),
            visible_frame=ns.Rect(1600.0, 0.0, 1200.0, 775.0),
        )
        icon = ns.Rect(2000.0, 776.0, 34.0, 24.0)
        got = ns.placement_for([icon], (), (DESK, second), PANEL)
        self.assertEqual(got.screen_index, 1)
        self.assertTrue(got.anchored)
        self.assertAlmostEqual(got.x + PANEL[0] / 2.0, icon.center_x)

    def test_an_unpointable_icon_falls_back_rather_than_guessing(self) -> None:
        got = ns.placement_for([ns.Rect(-5000.0, -5000.0, 34.0, 24.0)], (), self.SCREENS, PANEL)
        self.assertFalse(got.anchored)


class PointerTests(unittest.TestCase):
    """🔴 The one spatial claim this app makes, and every way it must
    refuse to make it.

    `contention_state` cannot say "directly above this window" — its words
    also go to a log and to `tools/status.py`, where there is no window.
    This layer can, because it placed one. What it must never do is say it
    when the panel did not actually end up there: that is the same fault
    as printing a coordinate, text asserting a spatial fact the code did
    not establish. One test per branch.
    """

    SCREENS = (DESK, VIEW)
    AVOID = (ns.rect_of(PICTURE, ZERO_HEIGHT),)

    def placement(self, icons, avoid=None, screens=None, panel=PANEL):
        return ns.placement_for(
            [ns.rect_of(icon, ZERO_HEIGHT) for icon in icons],
            self.AVOID if avoid is None else avoid,
            self.SCREENS if screens is None else screens,
            panel,
        )

    def test_it_points_when_the_panel_really_is_under_the_icon(self) -> None:
        got = self.placement([MENU_BAR_ICON])
        self.assertTrue(got.anchored)
        self.assertFalse(got.clamped)
        self.assertEqual(ns.pointer_sentence(got), ns.POINTER_SENTENCE)

    def test_it_withholds_when_clamped_at_the_right_edge(self) -> None:
        """The subtle one. A clamped panel still has the icon somewhere
        over it — proved by `AnchorTests` — but its centre is no longer
        under the icon's centre, and "directly above" is then a stronger
        claim than what was worked out. Silence costs a sentence; a wrong
        pointer sends someone to the wrong end of their menu bar while
        their app will not start."""
        flush = cs.WindowFact(
            layer=25,
            on_screen=True,
            x=DESK.visible_frame.max_x - 34.0,
            y=ZERO_HEIGHT - DESK.frame.max_y,
            width=34.0,
            height=24.0,
        )
        got = self.placement([flush])
        self.assertTrue(got.anchored)
        self.assertTrue(got.clamped)
        self.assertIsNone(ns.pointer_sentence(got))

    def test_it_withholds_when_clamped_at_the_left_edge(self) -> None:
        flush = cs.WindowFact(
            layer=25,
            on_screen=True,
            x=DESK.visible_frame.x,
            y=ZERO_HEIGHT - DESK.frame.max_y,
            width=34.0,
            height=24.0,
        )
        got = self.placement([flush])
        self.assertTrue(got.clamped)
        self.assertIsNone(ns.pointer_sentence(got))

    def test_it_withholds_when_the_panel_is_wider_than_the_screen(self) -> None:
        narrow = (
            ns.Screen(
                frame=ns.Rect(0.0, 0.0, 200.0, 900.0),
                visible_frame=ns.Rect(0.0, 0.0, 200.0, 875.0),
            ),
        )
        icon = cs.WindowFact(layer=25, on_screen=True, x=10.0, y=25.0, width=34.0, height=24.0)
        got = ns.placement_for([ns.rect_of(icon, 900.0)], (), narrow, PANEL)
        self.assertTrue(got.clamped)
        self.assertIsNone(ns.pointer_sentence(got))

    def test_it_withholds_when_there_was_no_icon_at_all(self) -> None:
        got = self.placement([])
        self.assertFalse(got.anchored)
        self.assertIsNone(ns.pointer_sentence(got))

    def test_it_withholds_when_the_only_icon_was_on_the_picture_display(self) -> None:
        """🔴 The panel is on the desk monitor and the icon is buried
        under a slideshow on a 2.1-inch round panel. Pointing at it would
        be worse than saying nothing."""
        got = self.placement([VIEW_ICON])
        self.assertFalse(got.anchored)
        self.assertIsNone(ns.pointer_sentence(got))

    def test_it_withholds_when_the_icon_is_on_no_screen(self) -> None:
        lost = cs.WindowFact(
            layer=25, on_screen=True, x=-5000.0, y=-5000.0, width=34.0, height=24.0
        )
        got = self.placement([lost])
        self.assertFalse(got.anchored)
        self.assertIsNone(ns.pointer_sentence(got))

    def test_it_withholds_when_there_was_no_placement_at_all(self) -> None:
        """No screens: AppKit centred the window itself and this layer
        knows nothing about where it went."""
        self.assertIsNone(ns.placement_for((), (), (), PANEL))
        self.assertIsNone(ns.pointer_sentence(None))

    def test_the_sentence_names_no_coordinate(self) -> None:
        """It is the replacement for a coordinate, not a dressed-up one."""
        self.assertFalse(any(char.isdigit() for char in ns.POINTER_SENTENCE))
        self.assertIn("above this window", ns.POINTER_SENTENCE)

    def test_clamping_is_recorded_only_when_the_clamp_moved_it(self) -> None:
        """`clamped` exists for `pointer_sentence` and for nothing else,
        so it has to mean exactly "the clamp changed the answer" — not
        "the icon was near an edge"."""
        middle = self.placement([MENU_BAR_ICON])
        self.assertFalse(middle.clamped)
        inset = cs.WindowFact(
            layer=25,
            on_screen=True,
            x=DESK.visible_frame.x + ns.EDGE_MARGIN + ns.PANEL_WIDTH / 2.0 - 17.0,
            y=ZERO_HEIGHT - DESK.frame.max_y,
            width=34.0,
            height=24.0,
        )
        self.assertFalse(
            self.placement([inset]).clamped,
            "an icon exactly at the clamp boundary was not moved by the clamp",
        )


class LogLineTests(unittest.TestCase):
    """The line a diagnostician reads afterwards.

    🔴 It is a pure function because it used to be an f-string with three
    conditionals inside `show()` — the one function no test can reach,
    since it blocks on a run loop — and it was wrapped in
    `if placement is not None`. So in the single case where nothing could
    be worked out, **nothing at all was logged**: a diagnostic going quiet
    exactly when it could not tell, inside the phase built to stop that.
    """

    def line(self, **kwargs):
        base = dict(screen_index=0, x=1006.0, y=778.0, anchored=True, clamped=False)
        base.update(kwargs)
        return ns.placement_log_line(ns.Placement(**base), ns.POINTER_SENTENCE)

    def test_the_case_with_nothing_to_say_still_says_it(self) -> None:
        got = ns.placement_log_line(None, None)
        self.assertTrue(got)
        self.assertIn("no screens", got)
        self.assertIn("withheld", got)

    def test_an_anchored_placement(self) -> None:
        got = self.line()
        self.assertIn("screen 0", got)
        self.assertIn("1006, 778", got)
        self.assertIn("under the icon", got)
        self.assertIn("pointer shown", got)
        self.assertNotIn("clamped", got)

    def test_a_centred_placement_says_there_was_nothing_to_point_at(self) -> None:
        got = ns.placement_log_line(
            ns.Placement(screen_index=1, x=0.0, y=0.0, anchored=False), None
        )
        self.assertIn("nothing to point at", got)
        self.assertIn("pointer withheld", got)
        self.assertNotIn("under the icon", got)

    def test_a_clamped_placement_says_so(self) -> None:
        """The reader needs to know the pointer was withheld *because* of
        the clamp, not because no icon was found — those are different
        faults to go looking for."""
        got = ns.placement_log_line(
            ns.Placement(screen_index=0, x=12.0, y=778.0, anchored=True, clamped=True), None
        )
        self.assertIn("under the icon", got)
        self.assertIn("clamped to the screen edge", got)
        self.assertIn("pointer withheld", got)

    def test_shown_and_withheld_are_not_swapped(self) -> None:
        """An inverted word here misleads precisely the person this whole
        phase exists for."""
        placement = ns.Placement(screen_index=0, x=1.0, y=2.0, anchored=True)
        self.assertIn("pointer shown", ns.placement_log_line(placement, ns.POINTER_SENTENCE))
        self.assertIn("pointer withheld", ns.placement_log_line(placement, None))


class AutoDismissTests(unittest.TestCase):
    def test_only_the_benign_verdict_dismisses_itself(self) -> None:
        """🔴 Every other verdict carries an instruction — make room in
        the menu bar, fix that folder, look behind another window, force
        quit *this* pid. A window that deletes an instruction after eight
        seconds is worse than no window: the reader now knows something
        was wrong and cannot read what."""
        for verdict in cs.Verdict:
            with self.subTest(verdict.value):
                got = ns.auto_dismiss_after(notice(verdict))
                if verdict is cs.Verdict.SERVING_VISIBLE:
                    self.assertEqual(got, ns.AUTO_DISMISS_AFTER_S)
                else:
                    self.assertIsNone(got)

    def test_it_closes_after_eight_seconds(self) -> None:
        """🔴 Pinned as a **literal**, not as `AUTO_DISMISS_AFTER_S`.

        `assertEqual(auto_dismiss_after(...), AUTO_DISMISS_AFTER_S)` reads
        the constant it is meant to be pinning, so it passes whatever the
        constant says. MEASURED, in this phase's own mutation matrix: with
        that assertion alone, changing 8 seconds to 60 **survived** — a
        vacuous test that looked like coverage. Eight seconds is long
        enough to read one sentence and short enough that a window nobody
        needed is gone before it is in the way."""
        self.assertEqual(ns.AUTO_DISMISS_AFTER_S, 8.0)
        self.assertEqual(ns.auto_dismiss_after(notice(cs.Verdict.SERVING_VISIBLE)), 8.0)

    def test_the_other_already_running_verdict_stays_up(self) -> None:
        """`NEVER_REPORTED`'s headline is also "ImageView is already
        running", and it is the message most users will ever see — but its
        body asks the reader to click the icon and judge what happens,
        which is an instruction."""
        self.assertIsNone(ns.auto_dismiss_after(notice(cs.Verdict.NEVER_REPORTED)))


class ButtonTests(unittest.TestCase):
    def test_close_is_always_there_and_always_last(self) -> None:
        """The panel may never become key — nothing here activates the
        app — so Cmd-W and Esc are conveniences and this is the
        contract."""
        for verdict in cs.Verdict:
            with self.subTest(verdict.value):
                buttons = ns.buttons_for(notice(verdict))
                self.assertEqual(buttons[-1], ns.CLOSE_BUTTON)

    def test_copy_is_offered_when_there_is_evidence(self) -> None:
        self.assertIn(ns.COPY_BUTTON, ns.buttons_for(notice()))

    def test_copy_is_not_offered_for_words_alone(self) -> None:
        bare = cs.Notice(
            verdict=cs.Verdict.UNIDENTIFIED,
            headline="Something else holds ImageView's lock",
            body="No usable pid.",
        )
        self.assertEqual(ns.buttons_for(bare), (ns.CLOSE_BUTTON,))
        self.assertFalse(ns.has_details(bare))

    def test_there_is_no_action_button(self) -> None:
        """This window states and instructs. The one thing it may do to
        the machine is put text on the clipboard — an About box was
        measured freezing the holder's heartbeat for 51.2 seconds, so
        "the stamp is stale" includes a perfectly healthy app whose owner
        is reading it."""
        for verdict in cs.Verdict:
            with self.subTest(verdict.value):
                self.assertLessEqual(set(ns.buttons_for(notice(verdict))),
                                     {ns.COPY_BUTTON, ns.CLOSE_BUTTON})


class DetailsTests(unittest.TestCase):
    def test_it_carries_the_words_and_the_evidence(self) -> None:
        text = ns.details_text(notice())
        for expected in (
            "unresponsive",
            "ImageView is not responding",
            "holder pid: 4321",
            "picture display pid: 4300",
            "37s since it last reported working",
            "on-screen window layers: 25",
            "state folder writable: yes",
            "/tmp/viewlab/ui.stacks.log",
        ):
            with self.subTest(expected):
                self.assertIn(expected, text)

    def test_could_not_look_and_nothing_there_do_not_print_the_same(self) -> None:
        """The distinction this project has paid for twice — a
        `screencapture` that exits 0 with bare wallpaper, and a keychain
        sweep that read "cannot read" as "no secrets"."""
        self.assertIn("could not be listed", ns.details_text(notice(window_layers=None)))
        self.assertIn("layers: none", ns.details_text(notice(window_layers=())))

    def test_a_holder_that_never_reported(self) -> None:
        self.assertIn("heartbeat: never reported", ns.details_text(notice(heartbeat_age_s=None)))

    def test_an_unverified_display_pid_is_never_printed_as_a_number(self) -> None:
        """🔴 Both halves of this app are called ImageView in Activity
        Monitor. A confident pointer at whatever happens to be running
        under an unverified id is worse than no pointer."""
        text = ns.details_text(notice(display_pid=None))
        self.assertIn("picture display pid: not verified", text)

    def test_an_uncaptured_stack_file_says_so(self) -> None:
        self.assertIn("thread stacks: not captured", ns.details_text(notice(stacks_path=None)))

    def test_it_never_raises_on_any_verdict(self) -> None:
        for verdict in cs.Verdict:
            with self.subTest(verdict.value):
                self.assertTrue(ns.details_text(notice(verdict)))


if __name__ == "__main__":
    unittest.main()
