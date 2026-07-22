"""Unit tests for app.py's Coordinator/Bootstrapper.

app.py is the least-unit-tested file in the project: Coordinator and
Bootstrapper are AppKit.NSObject subclasses normally driven by real
NSTimer target-action selectors, which would otherwise require an actual
NSScreen/window and a window-server session to construct.

Technique: construct via `Coordinator.alloc().init()` — bypassing the
real `initWithScreen_calibration_settings_`, which needs a real NSScreen
— then set `_rotation`, `_cache`, `_window`, `_settings`, `_started_at`,
and `_has_shown_real_image` as plain instance attributes to
unittest.mock.Mock() fakes (or plain values where a Mock doesn't fit).
`Coordinator.alloc().init()` runs the inherited plain NSObject `-init`
(Coordinator never overrides it), which is enough to get a live, usable
Python/ObjC object without touching AppKit's screen/window machinery.
The real timer-selector methods (advanceRotation_, pollImageServer_,
_show_current, _show_image) can then be called directly as plain Python
methods — no AppKit window or window-server session required.
"""

from __future__ import annotations

import json
import random
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from display import app
from display.blank_schedule import BlankSchedule
from display.calibration import Calibration
from display.control import ControlChannel, ControlState, write_control
from display.rotation import Rotation
from display.source_settings import SourceSettings
from display.sources.base import ImageRecord


def _make_coordinator(
    *,
    started_at: float = 0.0,
    has_shown_real_image: bool = False,
    screen_present: bool = True,
    rotation: Mock | None = None,
    cache: Mock | None = None,
    window: Mock | None = None,
    settings: Mock | None = None,
    source: Mock | None = None,
) -> app.Coordinator:
    """Build a Coordinator without going through
    initWithScreen_calibration_settings_ (which needs a real NSScreen),
    per the technique described in the module docstring."""
    coordinator = app.Coordinator.alloc().init()
    if rotation is None:
        # A bare Mock()'s `is_pinned` is a truthy Mock, which would make
        # every rotation tick look paused. Default it to the
        # running state; pause tests set it explicitly.
        rotation = Mock()
        rotation.is_pinned = False
    coordinator._rotation = rotation
    coordinator._cache = cache if cache is not None else Mock()
    coordinator._window = window if window is not None else Mock()
    # A **real** BlankSchedule, for the same reason the Calibration below
    # is real: `ControlState.effective_blanked` branches on
    # `schedule.is_active`, and a bare Mock's is a truthy Mock — so every
    # blank test would silently run the scheduled path against a schedule
    # whose `in_window` is also a Mock. Disabled-by-default is what real
    # construction produces for anyone who has not set one.
    coordinator._settings = (
        settings
        if settings is not None
        else Mock(fade_duration_s=2.0, blank_schedule=BlankSchedule(), shuffle=True)
    )
    # 6.1: the Coordinator holds an ImageSource now, not an Image Server
    # client. `caches` decides whether _cache is an ImageCache or a
    # DirectStore; both present the same four methods, so these tests go on
    # mocking one object either way.
    coordinator._source = source if source is not None else Mock()
    coordinator._started_at = started_at
    coordinator._has_shown_real_image = has_shown_real_image
    # Matches real construction: the window was just ordered front
    # successfully, so the screen starts present.
    coordinator._screen_present = screen_present
    # A **real** Calibration, not a Mock: Step 3 made the drawn circle
    # `file calibration + transient preview` (app._refresh_drawn_
    # calibration), and `calibration.apply_preview` does a
    # `dataclasses.replace` on it. A Mock here would not just fail — it
    # would let a preview test pass while proving nothing about the
    # arithmetic that puts a circle on the glass. These are the shipped
    # numbers, so a preview in these tests is clamped by the same
    # framebuffer the device has.
    coordinator._calibration = Calibration(
        framebuffer_width=960.0,
        framebuffer_height=960.0,
        center_x=483.0,
        center_y=482.0,
        radius_px=472.0,
        safety_margin_pct=0.93,
    )
    # Step 1: display labels and the applied desired state.
    # Plain values, not Mocks — every one of these is compared against or
    # branched on, and a Mock would silently read as truthy.
    coordinator._labels = {}
    coordinator._blanked = False
    coordinator._paused = False
    coordinator._applied_paused_on_id = None
    coordinator._preview_calibration = None
    return coordinator


class AdvanceRotationTests(unittest.TestCase):
    """Item 1 + Fix 2's regression tests."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._status_patch = patch.object(
            app, "STATUS_PATH", Path(self._tmp.name) / "status.json"
        )
        self._status_patch.start()

    def tearDown(self) -> None:
        self._status_patch.stop()
        self._tmp.cleanup()

    def test_advance_rotation_calls_next_image_never_current(self) -> None:
        """Direct regression test for the bug that started this whole
        review: app.py previously had next_image()/current() swapped."""
        coordinator = _make_coordinator(has_shown_real_image=True)
        coordinator._rotation.next_image.return_value = "img-1"
        coordinator._cache.get_path.return_value = Path("/tmp/img-1.png")

        coordinator.advanceRotation_(None)

        coordinator._rotation.next_image.assert_called_once()
        coordinator._rotation.current.assert_not_called()

    def test_advance_rotation_none_with_real_image_shown_leaves_view_alone(self) -> None:
        """Fix 2: rotation.next_image() returning None (pool went empty)
        must not blank a currently-displayed real image."""
        coordinator = _make_coordinator(has_shown_real_image=True)
        coordinator._rotation.next_image.return_value = None
        view = coordinator._window.contentView.return_value

        coordinator.advanceRotation_(None)

        view.transitionToImagePath_duration_.assert_not_called()
        view.setImagePath_.assert_not_called()

    def test_advance_rotation_none_with_nothing_ever_shown_still_shows_fallback(self) -> None:
        """Fix 2's other branch: on a fresh start, nothing has ever been
        shown, so there's nothing to protect — the empty-pool fallback
        fill is still the correct thing to draw."""
        coordinator = _make_coordinator(has_shown_real_image=False)
        coordinator._rotation.next_image.return_value = None
        view = coordinator._window.contentView.return_value

        coordinator.advanceRotation_(None)

        view.transitionToImagePath_duration_.assert_called_once_with(
            None, coordinator._settings.fade_duration_s
        )


class ShowCurrentTests(unittest.TestCase):
    """Item 2."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._status_patch = patch.object(
            app, "STATUS_PATH", Path(self._tmp.name) / "status.json"
        )
        self._status_patch.start()

    def tearDown(self) -> None:
        self._status_patch.stop()
        self._tmp.cleanup()

    def test_show_current_calls_current_never_next_image(self) -> None:
        coordinator = _make_coordinator()
        coordinator._rotation.current.return_value = "img-1"
        coordinator._cache.get_path.return_value = Path("/tmp/img-1.png")

        coordinator._show_current(fade=False)

        coordinator._rotation.current.assert_called_once()
        coordinator._rotation.next_image.assert_not_called()


class ShowImageTests(unittest.TestCase):
    """Fix 6's regression test."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._status_patch = patch.object(
            app, "STATUS_PATH", Path(self._tmp.name) / "status.json"
        )
        self._status_patch.start()

    def tearDown(self) -> None:
        self._status_patch.stop()
        self._tmp.cleanup()

    def test_show_image_with_real_id_marks_it_shown_in_cache(self) -> None:
        coordinator = _make_coordinator()
        coordinator._cache.get_path.return_value = Path("/tmp/img-1.png")

        coordinator._show_image("img-1", fade=False)

        coordinator._cache.mark_shown.assert_called_once_with("img-1")
        self.assertTrue(coordinator._has_shown_real_image)

    def test_show_image_with_none_does_not_mark_shown(self) -> None:
        coordinator = _make_coordinator()

        coordinator._show_image(None, fade=False)

        coordinator._cache.mark_shown.assert_not_called()
        self.assertFalse(coordinator._has_shown_real_image)


class PollImageServerTests(unittest.TestCase):
    """Item 5 + Fix 3's regression test."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._status_patch = patch.object(
            app, "STATUS_PATH", Path(self._tmp.name) / "status.json"
        )
        self._status_patch.start()

    def tearDown(self) -> None:
        self._status_patch.stop()
        self._tmp.cleanup()

    def test_poll_with_records_syncs_cache_and_rotation_with_is_valid(self) -> None:
        # Pin has_shown_real_image=True so Fix 5's cold-start refresh
        # branch doesn't also fire here — that's covered separately below.
        coordinator = _make_coordinator(has_shown_real_image=True)
        records = [object(), object()]
        coordinator._source.list_images.return_value = records
        coordinator._cache.known_ids.return_value = {"img-1"}

        coordinator.pollImageServer_(None)

        coordinator._cache.sync.assert_called_once_with(
            records, coordinator._source
        )
        coordinator._rotation.sync_pool.assert_called_once()
        _args, kwargs = coordinator._rotation.sync_pool.call_args
        self.assertIn("is_valid", kwargs)
        self.assertEqual(kwargs["is_valid"], coordinator._is_image_valid)

    def test_poll_with_no_records_does_not_sync_cache_or_rotation(self) -> None:
        coordinator = _make_coordinator(has_shown_real_image=True)
        coordinator._source.list_images.return_value = []

        coordinator.pollImageServer_(None)

        coordinator._cache.sync.assert_not_called()
        coordinator._rotation.sync_pool.assert_not_called()

    def test_poll_swallows_unexpected_exception(self) -> None:
        """Fix 3: a collaborator raising an arbitrary exception deep in
        the call graph must not propagate out of the NSTimer selector."""
        coordinator = _make_coordinator(has_shown_real_image=True)
        coordinator._source.list_images.side_effect = RuntimeError("boom")

        try:
            coordinator.pollImageServer_(None)
        except Exception as exc:  # noqa: BLE001 - this is the assertion
            self.fail(f"pollImageServer_ raised {exc!r} instead of swallowing it")

    def test_cold_start_first_successful_poll_triggers_display_refresh(self) -> None:
        """Fix 5's regression test: nothing has ever been shown yet;
        after a successful poll makes the rotation non-empty, the display
        should refresh immediately rather than waiting for the next
        scheduled rotation tick."""
        coordinator = _make_coordinator(has_shown_real_image=False)
        coordinator._source.list_images.return_value = [object()]
        coordinator._cache.known_ids.return_value = {"img-1"}
        coordinator._cache.get_path.return_value = Path("/tmp/img-1.png")
        coordinator._rotation.current.return_value = "img-1"
        view = coordinator._window.contentView.return_value

        coordinator.pollImageServer_(None)

        coordinator._rotation.sync_pool.assert_called_once()
        view.transitionToImagePath_duration_.assert_called_once_with(
            Path("/tmp/img-1.png"), coordinator._settings.fade_duration_s
        )
        self.assertTrue(coordinator._has_shown_real_image)

    def test_no_refresh_triggered_when_already_shown_something(self) -> None:
        """Companion to the cold-start test: once a real image has
        already been shown, a later successful poll must not force an
        extra out-of-cycle display refresh."""
        coordinator = _make_coordinator(has_shown_real_image=True)
        coordinator._source.list_images.return_value = [object()]
        coordinator._cache.known_ids.return_value = {"img-1"}
        coordinator._rotation.current.return_value = "img-1"
        view = coordinator._window.contentView.return_value

        coordinator.pollImageServer_(None)

        view.transitionToImagePath_duration_.assert_not_called()
        view.setImagePath_.assert_not_called()


class ScreenParametersChangedTests(unittest.TestCase):
    """Screen-disappearance/reappearance handling for an already-running
    Coordinator. Bootstrapper (see BootstrapperTryResolveTests below)
    only covers the pre-Coordinator "screen not found yet" phase; this
    is the post-startup counterpart, wired to
    NSApplicationDidChangeScreenParametersNotification."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._status_patch = patch.object(
            app, "STATUS_PATH", Path(self._tmp.name) / "status.json"
        )
        self._status_patch.start()

    def tearDown(self) -> None:
        self._status_patch.stop()
        self._tmp.cleanup()

    def test_screen_disappears_hides_window(self) -> None:
        coordinator = _make_coordinator(screen_present=True)
        with patch.object(app, "get_view_screen", return_value=None):
            coordinator.screenParametersChanged_(None)

        coordinator._window.orderOut_.assert_called_once_with(None)
        self.assertFalse(coordinator._screen_present)

    def test_screen_reappears_restores_window(self) -> None:
        coordinator = _make_coordinator(screen_present=False)
        sentinel_screen = Mock(name="sentinel_screen")
        sentinel_screen.frame.return_value = "sentinel-frame"
        with patch.object(app, "get_view_screen", return_value=sentinel_screen):
            coordinator.screenParametersChanged_(None)

        coordinator._window.setFrame_display_.assert_called_once_with(
            "sentinel-frame", True
        )
        coordinator._window.orderFrontRegardless.assert_called_once()
        self.assertTrue(coordinator._screen_present)

    def test_no_change_while_present_is_a_noop(self) -> None:
        """An unrelated screen-parameters notification (e.g. the Dell's
        resolution changing, not the View's presence) must not churn the
        window."""
        coordinator = _make_coordinator(screen_present=True)
        sentinel_screen = Mock(name="sentinel_screen")
        with patch.object(app, "get_view_screen", return_value=sentinel_screen):
            coordinator.screenParametersChanged_(None)

        coordinator._window.orderOut_.assert_not_called()
        coordinator._window.orderFrontRegardless.assert_not_called()
        coordinator._window.setFrame_display_.assert_not_called()
        self.assertTrue(coordinator._screen_present)

    def test_no_change_while_absent_is_a_noop(self) -> None:
        coordinator = _make_coordinator(screen_present=False)
        with patch.object(app, "get_view_screen", return_value=None):
            coordinator.screenParametersChanged_(None)

        coordinator._window.orderOut_.assert_not_called()
        coordinator._window.orderFrontRegardless.assert_not_called()
        coordinator._window.setFrame_display_.assert_not_called()
        self.assertFalse(coordinator._screen_present)

    def test_screen_parameters_changed_swallows_unexpected_exception(self) -> None:
        """Matches pollImageServer_/advanceRotation_/tryResolve_'s
        NSTimer/notification-selector-safety pattern: a collaborator
        raising an arbitrary exception deep in the call graph must not
        propagate out of this notification-center-invoked selector."""
        coordinator = _make_coordinator(screen_present=True)
        with patch.object(
            app, "get_view_screen", side_effect=RuntimeError("boom")
        ):
            try:
                coordinator.screenParametersChanged_(None)
            except Exception as exc:  # noqa: BLE001 - this is the assertion
                self.fail(
                    f"screenParametersChanged_ raised {exc!r} instead of "
                    f"swallowing it"
                )


class CoordinatorInitTests(unittest.TestCase):
    """Regression test for a bug that shipped past all 122 prior tests:
    every other test in this file constructs Coordinator via
    alloc().init(), bypassing initWithScreen_calibration_settings_
    entirely (per the module docstring - it needs a real NSScreen). That
    meant nothing ever exercised the actual init path, and it shipped
    without ever calling window.orderFrontRegardless() - the window
    existed with correct level/frame/alpha but the window server never
    composited it, so the View just showed the desktop wallpaper
    underneath. Caught live, on the physical device, after Step 6 was
    already reviewed and merged. This test exercises the real init path
    (patching build_window instead of bypassing construction) so this
    specific class of "window built but never shown" bug can't recur
    silently again."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._status_patch = patch.object(
            app, "STATUS_PATH", Path(self._tmp.name) / "status.json"
        )
        self._status_patch.start()

    def tearDown(self) -> None:
        self._status_patch.stop()
        self._tmp.cleanup()

    def test_init_orders_window_front(self) -> None:
        fake_window = Mock(name="window")
        fake_screen = Mock(name="screen")
        fake_calibration = Mock(name="calibration")
        fake_settings = Mock(cache_max=300)

        with patch.object(
            app, "build_window", return_value=fake_window
        ) as mock_build_window, patch.object(
            app, "ImageCache"
        ) as mock_cache_cls, patch.object(
            app, "build_source"
        ), patch.object(app, "Rotation"):
            mock_cache_cls.return_value.known_ids.return_value = set()
            coordinator = app.Coordinator.alloc().initWithScreen_calibration_settings_(
                fake_screen, fake_calibration, fake_settings
            )

        mock_build_window.assert_called_once_with(fake_screen, fake_calibration)
        fake_window.orderFrontRegardless.assert_called_once()
        self.assertIs(coordinator._window, fake_window)

    def test_init_registers_screen_parameters_observer(self) -> None:
        """Companion regression test, same motivation as
        test_init_orders_window_front above: every other test in this
        file bypasses real init, so nothing but a real-init test would
        catch the observer registration silently going missing."""
        fake_window = Mock(name="window")
        fake_screen = Mock(name="screen")
        fake_calibration = Mock(name="calibration")
        fake_settings = Mock(cache_max=300)

        # patch.object can't target NSNotificationCenter.defaultCenter
        # directly - it's a PyObjC-bridged selector, not a normal Python
        # attribute, and mock's cleanup delattr fails on it ("Cannot
        # remove selector 'defaultCenter'"). Patching the class
        # reference on the AppKit module instead sidesteps that.
        mock_notification_center_cls = Mock(name="NSNotificationCenter")
        with patch.object(
            app, "build_window", return_value=fake_window
        ), patch.object(
            app, "ImageCache"
        ) as mock_cache_cls, patch.object(
            app, "build_source"
        ), patch.object(app, "Rotation"), patch.object(
            app.AppKit, "NSNotificationCenter", mock_notification_center_cls
        ):
            mock_cache_cls.return_value.known_ids.return_value = set()
            coordinator = app.Coordinator.alloc().initWithScreen_calibration_settings_(
                fake_screen, fake_calibration, fake_settings
            )

        mock_notification_center_cls.defaultCenter.return_value.addObserver_selector_name_object_.assert_called_once_with(
            coordinator,
            "screenParametersChanged:",
            app.AppKit.NSApplicationDidChangeScreenParametersNotification,
            None,
        )
        self.assertTrue(coordinator._screen_present)


class BootstrapperTryResolveTests(unittest.TestCase):
    """Item 8."""

    def _make_bootstrapper(self) -> app.Bootstrapper:
        calibration = Mock(name="calibration")
        settings = Mock(name="settings")
        bootstrapper = app.Bootstrapper.alloc().initWithCalibration_settings_(
            calibration, settings
        )
        return bootstrapper

    def test_no_screen_resolved_does_not_construct_coordinator(self) -> None:
        bootstrapper = self._make_bootstrapper()
        with patch.object(app, "get_view_screen", return_value=None), patch.object(
            app, "Coordinator"
        ) as mock_coordinator_cls:
            bootstrapper.tryResolve_(None)

        mock_coordinator_cls.alloc.assert_not_called()
        self.assertIsNone(bootstrapper._coordinator)

    def test_screen_resolved_constructs_and_starts_coordinator(self) -> None:
        bootstrapper = self._make_bootstrapper()
        sentinel_screen = Mock(name="sentinel_screen")
        sentinel_screen.frame.return_value = "sentinel-frame"

        with patch.object(
            app, "get_view_screen", return_value=sentinel_screen
        ), patch.object(app, "Coordinator") as mock_coordinator_cls:
            mock_instance = mock_coordinator_cls.alloc.return_value.initWithScreen_calibration_settings_.return_value
            bootstrapper.tryResolve_(None)

            mock_coordinator_cls.alloc.return_value.initWithScreen_calibration_settings_.assert_called_once_with(
                sentinel_screen, bootstrapper._calibration, bootstrapper._settings
            )
            mock_instance.start.assert_called_once()
        self.assertIs(bootstrapper._coordinator, mock_instance)

    def test_try_resolve_swallows_unexpected_exception(self) -> None:
        """Fix 3: same NSTimer-safety guarantee, for Bootstrapper's
        startup-phase selector."""
        bootstrapper = self._make_bootstrapper()
        with patch.object(app, "get_view_screen", side_effect=RuntimeError("boom")):
            try:
                bootstrapper.tryResolve_(None)
            except Exception as exc:  # noqa: BLE001 - this is the assertion
                self.fail(
                    f"tryResolve_ raised {exc!r} instead of swallowing it"
                )


class ApplyCalibrationTests(unittest.TestCase):
    """A hot-reloaded calibration reaches the drawn
    circle without a restart."""

    def test_apply_calibration_updates_the_view_and_redraws(self) -> None:
        coordinator = _make_coordinator()
        new_calibration = Mock(name="new_calibration")

        coordinator.apply_calibration(new_calibration)

        view = coordinator._window.contentView.return_value
        self.assertIs(view.calibration, new_calibration)
        view.setNeedsDisplay_.assert_called_once_with(True)
        self.assertIs(coordinator._calibration, new_calibration)

    def test_apply_calibration_does_not_move_the_window_or_rescreen(self) -> None:
        """Deliberate: get_view_screen() keys off framebuffer dimensions,
        so a half-typed width could momentarily match another monitor."""
        coordinator = _make_coordinator()
        with patch.object(app, "get_view_screen") as mock_get_screen:
            coordinator.apply_calibration(Mock(name="new_calibration"))
        mock_get_screen.assert_not_called()
        coordinator._window.setFrame_display_.assert_not_called()


class ControlTimerTests(unittest.TestCase):
    """The dedicated control timer.

    Isolated from the real `~/.viewlab/` by patching Path.home — the
    watchers stat their paths at construction.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self._home_patch = patch("pathlib.Path.home", return_value=self.home)
        self._home_patch.start()
        self._status_patch = patch.object(
            app, "STATUS_PATH", self.home / "status.json"
        )
        self._status_patch.start()

    def tearDown(self) -> None:
        self._status_patch.stop()
        self._home_patch.stop()
        self._tmp.cleanup()

    def _make_bootstrapper(self) -> app.Bootstrapper:
        return app.Bootstrapper.alloc().initWithCalibration_settings_(
            Mock(name="calibration"), Mock(name="settings")
        )

    def test_start_schedules_a_separate_control_timer(self) -> None:
        """A *separate* 0.25s timer, not work bolted onto the
        signal-responsiveness heartbeat."""
        bootstrapper = self._make_bootstrapper()
        with patch.object(app, "get_view_screen", return_value=None), patch.object(
            app.AppKit, "NSTimer"
        ) as mock_timer_cls:
            bootstrapper.start()

        scheduled = [
            call.args
            for call in mock_timer_cls.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_.call_args_list
        ]
        self.assertIn(
            (app.CONTROL_INTERVAL_S, bootstrapper, "controlTick:", None, True),
            scheduled,
        )

    def test_control_timer_is_scheduled_even_when_the_view_is_absent(self) -> None:
        """A calibration edit is exactly how a user fixes a framebuffer
        mismatch that is *why* the View isn't resolving."""
        bootstrapper = self._make_bootstrapper()
        with patch.object(app, "get_view_screen", return_value=None), patch.object(
            app.AppKit, "NSTimer"
        ):
            bootstrapper.start()
        self.assertIsNotNone(bootstrapper._control_timer)

    def test_changed_calibration_is_applied_to_the_coordinator(self) -> None:
        bootstrapper = self._make_bootstrapper()
        coordinator = Mock(name="coordinator")
        bootstrapper._coordinator = coordinator
        new_calibration = Mock(center_x=1.0, center_y=2.0, effective_radius_px=3.0)
        bootstrapper._calibration_watch = Mock(**{"poll.return_value": new_calibration})
        bootstrapper._settings_watch = Mock(**{"poll.return_value": None})

        bootstrapper.controlTick_(None)

        coordinator.apply_calibration.assert_called_once_with(new_calibration)
        self.assertIs(bootstrapper._calibration, new_calibration)

    def test_unchanged_config_leaves_the_coordinator_alone(self) -> None:
        bootstrapper = self._make_bootstrapper()
        coordinator = Mock(name="coordinator")
        bootstrapper._coordinator = coordinator
        bootstrapper._calibration_watch = Mock(**{"poll.return_value": None})
        bootstrapper._settings_watch = Mock(**{"poll.return_value": None})

        bootstrapper.controlTick_(None)

        coordinator.apply_calibration.assert_not_called()

    def test_calibration_change_before_a_coordinator_exists_is_held(self) -> None:
        bootstrapper = self._make_bootstrapper()
        new_calibration = Mock(center_x=1.0, center_y=2.0, effective_radius_px=3.0)
        bootstrapper._calibration_watch = Mock(**{"poll.return_value": new_calibration})
        bootstrapper._settings_watch = Mock(**{"poll.return_value": None})

        bootstrapper.controlTick_(None)

        # No coordinator to push it to, but tryResolve_ must use the new
        # value on its next tick.
        self.assertIs(bootstrapper._calibration, new_calibration)

    def test_control_tick_swallows_unexpected_exception(self) -> None:
        """Acceptance criterion: a malformed config must not stop the
        control timer. An exception escaping this selector kills the run
        loop and takes the display with it."""
        bootstrapper = self._make_bootstrapper()
        bootstrapper._calibration_watch = Mock(**{"poll.side_effect": RuntimeError("boom")})
        bootstrapper._settings_watch = Mock(**{"poll.return_value": None})

        try:
            bootstrapper.controlTick_(None)
        except Exception as exc:  # noqa: BLE001 - this is the assertion
            self.fail(f"controlTick_ raised {exc!r} instead of swallowing it")

    def test_control_tick_keeps_ticking_after_a_failure(self) -> None:
        bootstrapper = self._make_bootstrapper()
        good = Mock(center_x=1.0, center_y=2.0, effective_radius_px=3.0)
        bootstrapper._calibration_watch = Mock(
            **{"poll.side_effect": [RuntimeError("boom"), good]}
        )
        bootstrapper._settings_watch = Mock(**{"poll.return_value": None})

        bootstrapper.controlTick_(None)
        bootstrapper.controlTick_(None)

        self.assertIs(bootstrapper._calibration, good)

    def test_a_malformed_config_file_does_not_kill_the_tick(self) -> None:
        """End-to-end through the real watcher: half-typed JSON in
        `~/.viewlab/calibration.json` leaves the last-good value in place, and the tick completes normally."""
        app.paths.ensure_dir(app.paths.config_dir())
        calibration_file = app.paths.calibration_path()
        calibration_file.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "framebuffer": {"width": 960, "height": 960},
                    "circle": {"center_x": 480.0, "center_y": 480.0, "radius_px": 400.0},
                }
            )
        )
        bootstrapper = self._make_bootstrapper()
        coordinator = Mock(name="coordinator")
        bootstrapper._coordinator = coordinator
        last_good = bootstrapper._calibration

        calibration_file.write_text('{"schema_version": 1, "circle": {')
        bootstrapper.controlTick_(None)

        coordinator.apply_calibration.assert_not_called()
        self.assertIs(bootstrapper._calibration, last_good)

    def test_a_valid_edit_is_applied_end_to_end(self) -> None:
        app.paths.ensure_dir(app.paths.config_dir())
        calibration_file = app.paths.calibration_path()
        base = {
            "schema_version": 1,
            "framebuffer": {"width": 960, "height": 960},
            "circle": {"center_x": 480.0, "center_y": 480.0, "radius_px": 400.0},
            "safety_margin_pct": 0.93,
        }
        calibration_file.write_text(json.dumps(base))
        bootstrapper = self._make_bootstrapper()
        coordinator = Mock(name="coordinator")
        bootstrapper._coordinator = coordinator

        nudged = dict(base, circle={"center_x": 483.0, "center_y": 482.0, "radius_px": 472.0})
        calibration_file.write_text(json.dumps(nudged))
        bootstrapper.controlTick_(None)

        coordinator.apply_calibration.assert_called_once()
        applied = coordinator.apply_calibration.call_args.args[0]
        self.assertEqual(applied.center_x, 483.0)
        self.assertEqual(applied.center_y, 482.0)

    def test_watchers_watch_the_user_config_never_the_bundled_seed(self) -> None:
        """Watching the repo's display/config/ would mean a
        half-saved edit during development is acted on immediately by the
        running display."""
        bootstrapper = self._make_bootstrapper()
        self.assertEqual(bootstrapper._calibration_watch.path, app.paths.calibration_path())
        self.assertEqual(bootstrapper._settings_watch.path, app.paths.settings_path())
        for watcher in (bootstrapper._calibration_watch, bootstrapper._settings_watch):
            self.assertFalse(
                watcher.path.is_relative_to(app.paths.bundled_config_dir())
            )


class SingleInstanceStartupTests(unittest.TestCase):
    """The flock guard in main()."""

    def test_main_exits_zero_when_another_instance_holds_the_lock(self) -> None:
        """exit(0) specifically: KeepAlive{SuccessfulExit: false} does not
        respawn a clean exit, so the loser stays down instead of
        respawn-looping."""
        with patch.object(app.paths, "ensure_all"), patch.object(
            app, "rotate_if_oversized"
        ), patch.object(app.single_instance, "acquire", return_value=None), patch.object(
            app, "load_calibration_resolved"
        ) as mock_load_calibration:
            with self.assertRaises(SystemExit) as caught:
                app.main()

        self.assertEqual(caught.exception.code, 0)
        mock_load_calibration.assert_not_called()

    def test_main_locks_the_documented_path(self) -> None:
        with patch.object(app.paths, "ensure_all"), patch.object(
            app, "rotate_if_oversized"
        ), patch.object(app.single_instance, "acquire", return_value=None) as mock_acquire:
            with self.assertRaises(SystemExit):
                app.main()
        mock_acquire.assert_called_once_with(app.paths.lock_path())


class SignalHeartbeatTests(unittest.TestCase):
    """The heartbeat must stay trivial. Its body has no exception
    wrapper, and the failure mode of a raising heartbeat is specifically
    losing the ability to stop the service."""

    def test_heartbeat_block_is_a_no_op_and_separate_from_the_control_timer(self) -> None:
        captured: list = []

        def fake_block_timer(interval, repeats, block):
            captured.append((interval, block))
            return Mock(name="heartbeat_timer")

        resolved_calibration = Mock(
            value=Mock(center_x=483.0, center_y=482.0, effective_radius_px=438.96),
            source=app.ConfigSource.USER,
            path=Path("/tmp/calibration.json"),
        )
        resolved_settings = Mock(
            value=Mock(
                rotation_interval_s=900.0,
                poll_interval_s=1800.0,
                fade_duration_s=2.0,
                pool="starred",
            ),
            source=app.ConfigSource.USER,
            path=Path("/tmp/settings.json"),
        )

        with patch.object(app.paths, "ensure_all"), patch.object(
            app, "rotate_if_oversized"
        ), patch.object(
            app.single_instance, "acquire", return_value=Mock()
        ), patch.object(
            app, "load_calibration_resolved", return_value=resolved_calibration
        ), patch.object(
            app, "load_settings_resolved", return_value=resolved_settings
        ), patch.object(
            app, "merge_status"
        ), patch.object(
            app, "install_signal_handlers"
        ), patch.object(
            app, "Bootstrapper"
        ), patch.object(
            app.AppKit, "NSApplication"
        ), patch.object(
            app.AppKit, "NSTimer"
        ) as mock_timer_cls:
            mock_timer_cls.scheduledTimerWithTimeInterval_repeats_block_.side_effect = (
                fake_block_timer
            )
            app.main()

        self.assertEqual(len(captured), 1, "exactly one block-based heartbeat timer")
        interval, block = captured[0]
        self.assertEqual(interval, app.SIGNAL_RESPONSIVENESS_INTERVAL_S)
        # The body must do nothing at all — no file stat, no JSON parse.
        self.assertIsNone(block(None))
        self.assertIsNone(block(Mock()))


# ======================================================================
# Step 1 — desired state, blanking, status fields
# ======================================================================


def _real_rotation(ids, tmpdir: str) -> Rotation:
    """A genuine Rotation over a temp state file, not a Mock.

    The control-state tests below are precisely about the interaction
    between app.py's applied state and rotation.py's pin — mocking the
    rotation would assert that app.py calls the methods it calls, which
    is not the property anyone cares about. "Next while paused moves the
    pin and does not resume" is only meaningful against the real one."""
    return Rotation(
        ids, state_path=Path(tmpdir) / "rotation_state.json", rng=random.Random(1)
    )


class ApplySettingsTests(unittest.TestCase):
    """Hot-reload, actually applied (Step 4).

    Step -1 recorded settings changes and told the user to restart, which
    was honest while the only way to edit the file was by hand. window makes that untenable: a settings screen whose changes need a
    restart is a settings screen that looks broken.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._status_patch = patch.object(
            app, "STATUS_PATH", Path(self._tmp.name) / "status.json"
        )
        self._status_patch.start()

    def tearDown(self) -> None:
        self._status_patch.stop()
        self._tmp.cleanup()

    def _settings(self, **overrides):
        base = dict(
            rotation_interval_s=900.0,
            poll_interval_s=1800.0,
            fade_duration_s=2.0,
            cache_max=300,
            shuffle=True,
            blank_schedule=BlankSchedule(),
            source=SourceSettings(kind="folder", folder="/tmp/a"),
        )
        base.update(overrides)
        return Mock(**base)

    def _coordinator(self, settings=None):
        coordinator = _make_coordinator(settings=settings or self._settings())
        coordinator._rotation = Mock()
        coordinator._poll_timer = Mock()
        coordinator._rotation_timer = Mock()
        return coordinator

    def test_an_unchanged_setting_rebuilds_nothing(self) -> None:
        """The watcher fires on any edit to the file. Rebuilding the
        source because the blanking time moved would stall the exact
        interaction that is supposed to feel immediate."""
        settings = self._settings()
        coordinator = self._coordinator(settings)
        old_source = coordinator._source

        coordinator.apply_settings(self._settings())

        self.assertIs(coordinator._source, old_source)
        coordinator._rotation.set_shuffle.assert_not_called()

    def test_a_rotation_interval_change_reschedules_that_timer(self) -> None:
        coordinator = self._coordinator()
        old_timer = coordinator._rotation_timer

        coordinator.apply_settings(self._settings(rotation_interval_s=60.0))

        old_timer.invalidate.assert_called_once()
        self.assertEqual(coordinator._settings.rotation_interval_s, 60.0)

    def test_a_rotation_interval_change_does_not_touch_the_poll_timer(self) -> None:
        coordinator = self._coordinator()
        poll_timer = coordinator._poll_timer

        coordinator.apply_settings(self._settings(rotation_interval_s=60.0))

        poll_timer.invalidate.assert_not_called()

    def test_an_order_change_reorders_without_rebuilding_the_source(self) -> None:
        coordinator = self._coordinator()
        old_source = coordinator._source

        coordinator.apply_settings(self._settings(shuffle=False))

        coordinator._rotation.set_shuffle.assert_called_once_with(False)
        self.assertIs(coordinator._source, old_source)

    def test_a_source_change_rebuilds_and_polls_immediately(self) -> None:
        """Waiting up to poll_interval_s after the user pressed Save is
        indistinguishable from the change not having worked."""
        coordinator = self._coordinator()
        old_source = coordinator._source

        with patch.object(app, "build_source") as build:
            build.return_value = Mock(caches=False, poll_interval_s=10.0)
            with patch.object(
                app.Coordinator, "pollImageServer_"
            ) as poll:
                coordinator.apply_settings(
                    self._settings(
                        source=SourceSettings(kind="folder", folder="/tmp/b")
                    )
                )

        self.assertIsNot(coordinator._source, old_source)
        old_source.close.assert_called_once()
        poll.assert_called_once()

    def test_a_source_that_refuses_to_close_does_not_stop_the_swap(self) -> None:
        """An absurd way to lose a display."""
        coordinator = self._coordinator()
        coordinator._source.close.side_effect = RuntimeError("nope")

        with patch.object(app, "build_source") as build:
            build.return_value = Mock(caches=False, poll_interval_s=10.0)
            with patch.object(app.Coordinator, "pollImageServer_"):
                coordinator.apply_settings(
                    self._settings(
                        source=SourceSettings(kind="folder", folder="/tmp/b")
                    )
                )

        build.assert_called_once()

    def test_refresh_now_polls(self) -> None:
        coordinator = self._coordinator()
        with patch.object(app.Coordinator, "pollImageServer_") as poll:
            coordinator.refresh_now()
        poll.assert_called_once()


class ScheduledBlankTickTests(unittest.TestCase):
    """Window edges fire on the control tick, not on a file
    change — a clock crossing 21:00 changes no file."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._status_patch = patch.object(
            app, "STATUS_PATH", Path(self._tmp.name) / "status.json"
        )
        self._status_patch.start()

    def tearDown(self) -> None:
        self._status_patch.stop()
        self._tmp.cleanup()

    def test_apply_blank_state_is_idempotent(self) -> None:
        """Runs four times a second; it must cost one comparison when
        nothing changed."""
        coordinator = _make_coordinator()
        view = coordinator._window.contentView.return_value
        schedule = BlankSchedule(True, 21 * 60, 7 * 60)
        night = time.mktime((2026, 7, 19, 22, 0, 0, 0, 0, -1))

        for _ in range(5):
            coordinator.apply_blank_state(
                ControlState(blanked=None), schedule
            )

        # The clock is real here, so this asserts the *shape* — at most
        # one render for five identical applications — rather than a
        # particular blank state.
        self.assertLessEqual(view.fadeToBlackWithDuration_.call_count, 1)
        self.assertLessEqual(view.restoreToImagePath_duration_.call_count, 1)


class ApplyControlStateTests(unittest.TestCase):
    """Command semantics, at the layer that owns them."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._status_patch = patch.object(
            app, "STATUS_PATH", Path(self._tmp.name) / "status.json"
        )
        self._status_patch.start()
        self.ids = ["a", "b", "c", "d", "e"]

    def tearDown(self) -> None:
        self._status_patch.stop()
        self._tmp.cleanup()

    def _coordinator(self) -> app.Coordinator:
        coordinator = _make_coordinator(
            has_shown_real_image=True,
            rotation=_real_rotation(self.ids, self._tmp.name),
        )
        coordinator._cache.get_path.return_value = Path("/tmp/x.png")
        return coordinator

    @staticmethod
    def _view(coordinator: app.Coordinator) -> Mock:
        return coordinator._window.contentView.return_value

    # -- blanking ------------------------------------------------

    def test_blanking_calls_the_renderer_pair_not_order_out(self) -> None:
        """`orderOut_` would reveal the desktop wallpaper on the View,
        which is the opposite of blank."""
        coordinator = self._coordinator()
        view = self._view(coordinator)

        coordinator.apply_control_state(ControlState(blanked=True), 0)

        view.fadeToBlackWithDuration_.assert_called_once_with(
            app.BLANK_FADE_DURATION_S
        )
        coordinator._window.orderOut_.assert_not_called()

    def test_restoring_passes_no_path_so_the_same_picture_comes_back(self) -> None:
        """"restoring shows the same picture you left"."""
        coordinator = self._coordinator()
        view = self._view(coordinator)
        coordinator.apply_control_state(ControlState(blanked=True), 0)

        coordinator.apply_control_state(ControlState(blanked=False), 0)

        view.restoreToImagePath_duration_.assert_called_once_with(
            None, app.BLANK_FADE_DURATION_S
        )

    def test_applying_the_same_blanked_state_twice_only_renders_once(self) -> None:
        """Level-triggered means this runs constantly; it must be a
        no-op when nothing changed."""
        coordinator = self._coordinator()
        view = self._view(coordinator)

        for _ in range(5):
            coordinator.apply_control_state(ControlState(blanked=True), 0)

        self.assertEqual(view.fadeToBlackWithDuration_.call_count, 1)

    def test_rotation_does_not_advance_while_blanked(self) -> None:
        """"rotation is paused, not hidden" — the picture
        underneath must not move, or restoring would show something
        else."""
        coordinator = self._coordinator()
        coordinator.apply_control_state(ControlState(blanked=True), 0)
        before = coordinator._rotation.current()

        for _ in range(3):
            coordinator.advanceRotation_(None)

        self.assertEqual(coordinator._rotation.current(), before)

    def test_next_un_blanks(self) -> None:
        """"any action un-blanks, except Pause"."""
        coordinator = self._coordinator()
        view = self._view(coordinator)
        coordinator.apply_control_state(ControlState(blanked=True), 0)

        coordinator.apply_control_state(ControlState(blanked=True), 1)

        self.assertFalse(coordinator._blanked)
        view.restoreToImagePath_duration_.assert_called_once()

    def test_a_new_calibration_preview_un_blanks(self) -> None:
        """`Adjust the circle…` appears alongside Next/Previous."""
        coordinator = self._coordinator()
        coordinator.apply_control_state(ControlState(blanked=True), 0)

        coordinator.apply_control_state(
            ControlState(blanked=True, preview_calibration={"center_x": 480.0}), 0
        )

        self.assertFalse(coordinator._blanked)

    def test_pausing_does_not_un_blank(self) -> None:
        """The single exception in rule, and the reason the rule
        needs an exception: Pause must not be the one control that turns
        the picture back *on*."""
        coordinator = self._coordinator()
        view = self._view(coordinator)
        coordinator.apply_control_state(ControlState(blanked=True), 0)

        coordinator.apply_control_state(ControlState(blanked=True, paused=True), 0)

        self.assertTrue(coordinator._blanked)
        self.assertTrue(coordinator._rotation.is_pinned)
        view.restoreToImagePath_duration_.assert_not_called()

    def test_resuming_does_not_un_blank_either(self) -> None:
        coordinator = self._coordinator()
        coordinator.apply_control_state(ControlState(blanked=True, paused=True), 0)

        coordinator.apply_control_state(ControlState(blanked=True, paused=False), 0)

        self.assertTrue(coordinator._blanked)
        self.assertFalse(coordinator._rotation.is_pinned)

    # -- pause ---------------------------------------------------

    def test_pausing_pins_the_named_picture(self) -> None:
        coordinator = self._coordinator()
        coordinator.apply_control_state(
            ControlState(paused=True, paused_on_id="c"), 0
        )
        self.assertEqual(coordinator._rotation.pinned_id(), "c")

    def test_next_while_paused_moves_the_pin_and_never_resumes(self) -> None:
        """"Next/Previous while paused move that id one
        step — they never clear `paused`"."""
        coordinator = self._coordinator()
        coordinator.apply_control_state(
            ControlState(paused=True, paused_on_id="c"), 0
        )

        coordinator.apply_control_state(
            ControlState(paused=True, paused_on_id="c"), 1
        )

        self.assertTrue(coordinator._paused)
        self.assertTrue(coordinator._rotation.is_pinned)
        self.assertNotEqual(coordinator._rotation.pinned_id(), "c")

    def test_a_stale_paused_on_id_does_not_drag_the_picture_back(self) -> None:
        """The reason `paused_on_id` is honoured only on change: a
        Next-while-paused moves this display's pin immediately, while the
        UI's file still names the old picture until its next write.
        Re-pinning from that stale value every tick would visibly yank
        the picture backwards."""
        coordinator = self._coordinator()
        coordinator.apply_control_state(
            ControlState(paused=True, paused_on_id="c"), 0
        )
        coordinator.apply_control_state(
            ControlState(paused=True, paused_on_id="c"), 1
        )
        moved_to = coordinator._rotation.pinned_id()

        # The UI has not caught up yet and writes the old id again.
        for _ in range(3):
            coordinator.apply_control_state(
                ControlState(paused=True, paused_on_id="c"), 0
            )

        self.assertEqual(coordinator._rotation.pinned_id(), moved_to)

    def test_the_ui_moving_the_pause_deliberately_is_still_honoured(self) -> None:
        """The flip side: when `paused_on_id` genuinely *changes*, it
        must be applied — otherwise the UI could never move the pause."""
        coordinator = self._coordinator()
        coordinator.apply_control_state(
            ControlState(paused=True, paused_on_id="c"), 0
        )
        coordinator.apply_control_state(
            ControlState(paused=True, paused_on_id="e"), 0
        )
        self.assertEqual(coordinator._rotation.pinned_id(), "e")

    def test_resuming_unpins_without_moving_the_picture(self) -> None:
        coordinator = self._coordinator()
        coordinator.apply_control_state(
            ControlState(paused=True, paused_on_id="c"), 0
        )
        coordinator.apply_control_state(ControlState(paused=False), 0)

        self.assertFalse(coordinator._paused)
        self.assertFalse(coordinator._rotation.is_pinned)
        self.assertEqual(coordinator._rotation.current(), "c")

    def test_the_rotation_timer_is_a_noop_while_paused(self) -> None:
        coordinator = self._coordinator()
        coordinator.apply_control_state(
            ControlState(paused=True, paused_on_id="c"), 0
        )

        for _ in range(4):
            coordinator.advanceRotation_(None)

        self.assertEqual(coordinator._rotation.current(), "c")

    # -- steps ----------------------------------------------------------

    def test_a_negative_step_moves_backwards(self) -> None:
        coordinator = self._coordinator()
        coordinator.apply_control_state(ControlState(), 2)
        forward = coordinator._rotation.position()

        coordinator.apply_control_state(ControlState(), -1)

        self.assertEqual(coordinator._rotation.position(), forward - 1)

    def test_a_step_shows_the_new_picture_with_a_fade(self) -> None:
        coordinator = self._coordinator()
        view = self._view(coordinator)

        coordinator.apply_control_state(ControlState(), 1)

        view.transitionToImagePath_duration_.assert_called_once()

    def test_zero_steps_does_not_redraw(self) -> None:
        coordinator = self._coordinator()
        view = self._view(coordinator)

        coordinator.apply_control_state(ControlState(), 0)

        view.transitionToImagePath_duration_.assert_not_called()


class ControlChannelWiringTests(unittest.TestCase):
    """The Bootstrapper end: the control timer now also polls the
    command file and writes the heartbeat."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.command_path = Path(self._tmp.name) / "command.json"
        self._status_patch = patch.object(
            app, "STATUS_PATH", Path(self._tmp.name) / "status.json"
        )
        self._status_patch.start()

    def tearDown(self) -> None:
        self._status_patch.stop()
        self._tmp.cleanup()

    def _bootstrapper(self) -> app.Bootstrapper:
        bootstrapper = app.Bootstrapper.alloc().init()
        bootstrapper._calibration = Mock()
        bootstrapper._settings = Mock()
        bootstrapper._coordinator = None
        bootstrapper._retry_timer = None
        bootstrapper._control_timer = None
        bootstrapper._calibration_watch = Mock(**{"poll.return_value": None})
        bootstrapper._settings_watch = Mock(**{"poll.return_value": None})
        bootstrapper._control = ControlChannel(path=self.command_path)
        bootstrapper._last_heartbeat_at = 0.0
        return bootstrapper

    def test_a_control_tick_applies_desired_state_to_the_coordinator(self) -> None:
        bootstrapper = self._bootstrapper()
        bootstrapper._control.adopt_current()
        bootstrapper._coordinator = Mock()

        write_control(self.command_path, ControlState(blanked=True, advance=1))
        bootstrapper.controlTick_(None)

        bootstrapper._coordinator.apply_control_state.assert_called_once()
        state, steps = bootstrapper._coordinator.apply_control_state.call_args.args
        self.assertTrue(state.effective_blanked())
        self.assertEqual(steps, 1)

    def test_a_corrupt_command_file_does_not_kill_the_tick(self) -> None:
        """Same acceptance criterion the release gate sets for a malformed config: the
        control timer must keep ticking."""
        bootstrapper = self._bootstrapper()
        bootstrapper._control.adopt_current()
        bootstrapper._coordinator = Mock()
        self.command_path.write_text("}{ not json")

        for _ in range(3):
            try:
                bootstrapper.controlTick_(None)
            except Exception as exc:  # noqa: BLE001 - this is the assertion
                self.fail(f"controlTick_ raised {exc!r} instead of swallowing it")

        bootstrapper._coordinator.apply_control_state.assert_not_called()

    def test_a_stale_command_file_never_re_fires_across_ticks(self) -> None:
        """The no-replay property, at the tick level: an unchanged file
        must produce exactly one application, not one every 250ms."""
        bootstrapper = self._bootstrapper()
        bootstrapper._control.adopt_current()
        bootstrapper._coordinator = Mock()
        write_control(self.command_path, ControlState(advance=5))

        for _ in range(10):
            bootstrapper.controlTick_(None)

        self.assertEqual(
            bootstrapper._coordinator.apply_control_state.call_count, 1
        )

    def test_startup_adopts_the_counter_so_nothing_replays(self) -> None:
        """No-replay rule end to end: a command file left over
        from an hour ago must not walk the rotation on launch."""
        write_control(self.command_path, ControlState(advance=42))
        bootstrapper = self._bootstrapper()
        bootstrapper._control.adopt_current()
        bootstrapper._coordinator = Mock()

        bootstrapper.controlTick_(None)

        bootstrapper._coordinator.apply_control_state.assert_not_called()
        self.assertEqual(bootstrapper._control.last_seen_advance, 42)

    def test_desired_state_is_applied_when_the_view_finally_appears(self) -> None:
        """A blanked View that comes back after being unplugged must come
        back blanked, not showing a picture."""
        write_control(self.command_path, ControlState(blanked=True))
        bootstrapper = self._bootstrapper()
        bootstrapper._control.adopt_current()

        # patch.object(app, "Coordinator"), never patch.object(
        # app.Coordinator, "alloc"): patching an attribute on a live
        # PyObjC class does not reliably restore, and a leaked `alloc`
        # takes out every later test in the file.
        with patch.object(app, "get_view_screen", return_value=Mock()), patch.object(
            app, "Coordinator"
        ) as mock_coordinator_cls:
            coordinator = (
                mock_coordinator_cls.alloc.return_value.initWithScreen_calibration_settings_.return_value
            )
            bootstrapper.tryResolve_(None)

        state, steps = coordinator.apply_control_state.call_args.args
        self.assertTrue(state.effective_blanked())
        # A Next pressed while the View was unplugged is not a Next
        # anyone still wants.
        self.assertEqual(steps, 0)

    def test_the_heartbeat_is_written_and_then_throttled(self) -> None:
        """A heartbeat the UI can call stale at >5s; it does
        not need four atomic status writes every second."""
        bootstrapper = self._bootstrapper()
        bootstrapper._control.adopt_current()

        with patch.object(app, "merge_status") as mock_merge:
            bootstrapper.controlTick_(None)
            first = mock_merge.call_count
            for _ in range(20):  # 5 seconds of ticks at 0.25s
                bootstrapper.controlTick_(None)

        self.assertEqual(first, 1)
        self.assertEqual(
            mock_merge.call_count,
            1,
            "the heartbeat must be throttled, not written on every tick",
        )

    def test_the_heartbeat_interval_leaves_margin_under_the_staleness_rule(
        self,
    ) -> None:
        """The display is called dead at >5s. Two heartbeats must be
        missed before that happens, or a single slow tick would show
        `Not showing pictures` on a perfectly healthy display."""
        self.assertLess(app.HEARTBEAT_INTERVAL_S * 2, 5.0)

    def test_the_heartbeat_ticks_even_with_no_view_connected(self) -> None:
        """`View not connected` and `Not showing pictures` are different
        menu bar states, and only a heartbeat that runs
        without a Coordinator can tell them apart."""
        bootstrapper = self._bootstrapper()
        bootstrapper._control.adopt_current()
        self.assertIsNone(bootstrapper._coordinator)

        bootstrapper.controlTick_(None)

        status = json.loads(app.STATUS_PATH.read_text())
        self.assertIn("heartbeat_at", status)
        self.assertFalse(status["view_connected"])


class StatusFieldTests(unittest.TestCase):
    """Additions."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._status_patch = patch.object(
            app, "STATUS_PATH", Path(self._tmp.name) / "status.json"
        )
        self._status_patch.start()

    def tearDown(self) -> None:
        self._status_patch.stop()
        self._tmp.cleanup()

    def _status(self) -> dict:
        return json.loads(app.STATUS_PATH.read_text())

    def test_a_failed_poll_records_a_human_readable_error(self) -> None:
        """"there is no error string; last_poll_ok: false says
        *that* something broke, never *what*"."""
        rotation = MagicMock()
        rotation.__len__.return_value = 0
        rotation.is_pinned = False
        coordinator = _make_coordinator(rotation=rotation)
        coordinator._source.list_images.return_value = []
        coordinator._source.label = "Pictures"

        coordinator.pollImageServer_(None)

        status = self._status()
        self.assertFalse(status["last_poll_ok"])
        self.assertIn("Pictures", status["last_error"])
        self.assertIsInstance(status["last_error_at"], float)

    def test_a_successful_poll_clears_the_error(self) -> None:
        """"cleared on success" — an error from an hour ago must
        not still be sitting in the settings window."""
        coordinator = _make_coordinator()
        coordinator._source.list_images.return_value = []
        coordinator._source.label = "Pictures"
        coordinator.pollImageServer_(None)
        self.assertIsNotNone(self._status()["last_error"])

        coordinator._source.list_images.return_value = [
            ImageRecord(id="img-1", filename="a.png", display_label="Sunset")
        ]
        coordinator._cache.known_ids.return_value = {"img-1"}
        coordinator.pollImageServer_(None)

        status = self._status()
        self.assertIsNone(status["last_error"])
        self.assertIsNone(status["last_error_at"])

    def test_image_count_is_what_can_be_shown_not_what_was_listed(self) -> None:
        """A record whose download was deferred is listed but not yet
        showable, and "47 pictures found" that cannot be displayed makes
        a user distrust every other number on the screen."""
        # MagicMock, not Mock: `len()` looks __len__ up on the type, so
        # assigning it to a plain Mock instance has no effect.
        rotation = MagicMock()
        rotation.__len__.return_value = 3
        rotation.is_pinned = False
        # has_shown_real_image=True skips the cold-start refresh, whose
        # Mock image id would otherwise poison the whole status write.
        coordinator = _make_coordinator(rotation=rotation, has_shown_real_image=True)
        coordinator._source.label = "Pictures"
        coordinator._source.list_images.return_value = [
            ImageRecord(id=f"img-{i}", filename=f"{i}.png") for i in range(20)
        ]
        coordinator._cache.known_ids.return_value = {"img-0"}

        coordinator.pollImageServer_(None)

        status = self._status()
        self.assertEqual(status["last_poll_count"], 20)
        self.assertEqual(status["image_count"], 3)

    def test_display_label_prefers_the_source_supplied_label(self) -> None:
        """Never a bare 36-character UUID in the menu bar."""
        coordinator = _make_coordinator()
        coordinator._source.list_images.return_value = [
            ImageRecord(
                id="a6184aff-a295", filename="a.png", display_label="A quiet harbour"
            )
        ]
        coordinator._cache.known_ids.return_value = {"a6184aff-a295"}
        coordinator.pollImageServer_(None)

        coordinator._show_image("a6184aff-a295", fade=False)

        self.assertEqual(self._status()["display_label"], "A quiet harbour")

    def test_display_label_falls_back_to_picture_n_of_m(self) -> None:
        """Fallback, which the plan notes is "more useful than a
        filename anyway"."""
        rotation = MagicMock()
        rotation.__len__.return_value = 47
        rotation.position.return_value = 12
        coordinator = _make_coordinator(rotation=rotation)
        coordinator._rotation.is_pinned = False

        coordinator._show_image("unlabelled-id", fade=False)

        self.assertEqual(self._status()["display_label"], "Picture 12 of 47")

    def test_a_status_field_that_cannot_be_computed_never_costs_the_frame(
        self,
    ) -> None:
        """The ordering rule this file's Fix 5 test depends on: putting a
        picture on the display outranks describing it."""
        rotation = Mock()
        rotation.__len__ = Mock(side_effect=RuntimeError("boom"))
        rotation.position.side_effect = RuntimeError("boom")
        coordinator = _make_coordinator(rotation=rotation)
        coordinator._rotation.is_pinned = False
        coordinator._cache.get_path.return_value = Path("/tmp/img.png")
        view = coordinator._window.contentView.return_value

        coordinator._show_image("img-1", fade=False)

        view.setImagePath_.assert_called_once_with(Path("/tmp/img.png"))
        self.assertEqual(self._status()["display_label"], "")

    def test_source_label_is_recorded(self) -> None:
        coordinator = _make_coordinator()
        coordinator._source.list_images.return_value = []
        coordinator._source.label = "Pictures"

        coordinator.pollImageServer_(None)

        self.assertEqual(self._status()["source_label"], "Pictures")

    def test_clear_sentinel_nulls_a_field_where_none_would_not(self) -> None:
        """`merge_status` treats None as "no opinion" so a partial update
        cannot blank a field it does not know about — which is why
        clearing needs its own sentinel."""
        app.merge_status(last_error="something broke")
        app.merge_status(last_error=None)
        self.assertEqual(self._status()["last_error"], "something broke")

        app.merge_status(last_error=app.CLEAR)
        self.assertIsNone(self._status()["last_error"])


if __name__ == "__main__":
    unittest.main()
