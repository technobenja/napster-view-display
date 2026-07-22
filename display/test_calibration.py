"""Unit tests for calibration.py — "range checks, not just does
it parse", every failure path falls back to the same safe default, never
raises. Pure stdlib (unittest), no AppKit — runs over SSH or locally.

    ./.venv/bin/python3 -m unittest test_calibration -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from display import calibration
from display import paths


def _write(tmpdir: Path, data: dict) -> Path:
    path = tmpdir / "calibration.json"
    path.write_text(json.dumps(data))
    return path


VALID = {
    "schema_version": 1,
    "framebuffer": {"width": 960, "height": 960},
    "target_screen": {"resolve_strategy": "match_by_resolution_excluding_main"},
    "circle": {"center_x": 483.0, "center_y": 482.0, "radius_px": 472.0},
    "safety_margin_pct": 0.93,
    "calibration_source": {
        "method": "test-pattern-photo",
        "photo_file": "findings/photos/calibration-2026-07-17-03.JPG",
        "measured_by": "owner",
        "date": "2026-07-17",
    },
}


class LoadCalibrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_valid_file_loads_exactly(self) -> None:
        path = _write(self.tmpdir, VALID)
        cal = calibration.load_calibration(path)
        self.assertEqual(cal.framebuffer_width, 960.0)
        self.assertEqual(cal.framebuffer_height, 960.0)
        self.assertEqual(cal.center_x, 483.0)
        self.assertEqual(cal.center_y, 482.0)
        self.assertEqual(cal.radius_px, 472.0)
        self.assertEqual(cal.safety_margin_pct, 0.93)

    def test_effective_radius_applies_safety_margin(self) -> None:
        path = _write(self.tmpdir, VALID)
        cal = calibration.load_calibration(path)
        self.assertAlmostEqual(cal.effective_radius_px, 472.0 * 0.93)

    def test_missing_file_falls_back(self) -> None:
        cal = calibration.load_calibration(self.tmpdir / "does-not-exist.json")
        self.assertEqual(cal.center_x, 480.0)
        self.assertEqual(cal.center_y, 480.0)
        self.assertEqual(cal.radius_px, 960.0 / 2 * 0.9)

    def test_malformed_json_falls_back(self) -> None:
        path = self.tmpdir / "calibration.json"
        path.write_text("{not valid json")
        cal = calibration.load_calibration(path)
        self.assertEqual(cal.radius_px, 960.0 / 2 * 0.9)

    def test_missing_required_key_falls_back(self) -> None:
        data = json.loads(json.dumps(VALID))
        del data["circle"]["radius_px"]
        path = _write(self.tmpdir, data)
        cal = calibration.load_calibration(path)
        self.assertEqual(cal.radius_px, 960.0 / 2 * 0.9)

    def test_negative_radius_falls_back(self) -> None:
        data = json.loads(json.dumps(VALID))
        data["circle"]["radius_px"] = -10.0
        path = _write(self.tmpdir, data)
        cal = calibration.load_calibration(path)
        self.assertEqual(cal.radius_px, 960.0 / 2 * 0.9)

    def test_zero_radius_falls_back(self) -> None:
        data = json.loads(json.dumps(VALID))
        data["circle"]["radius_px"] = 0.0
        path = _write(self.tmpdir, data)
        cal = calibration.load_calibration(path)
        self.assertEqual(cal.radius_px, 960.0 / 2 * 0.9)

    def test_radius_exceeding_nearest_edge_falls_back(self) -> None:
        data = json.loads(json.dumps(VALID))
        # center_x=483 -> nearest horizontal edge is 960-483=477; 478 must fail.
        data["circle"]["radius_px"] = 478.0
        path = _write(self.tmpdir, data)
        cal = calibration.load_calibration(path)
        self.assertEqual(cal.radius_px, 960.0 / 2 * 0.9)

    def test_radius_exactly_at_nearest_edge_is_valid(self) -> None:
        data = json.loads(json.dumps(VALID))
        data["circle"]["radius_px"] = 477.0  # == 960 - 483, the binding edge
        path = _write(self.tmpdir, data)
        cal = calibration.load_calibration(path)
        self.assertEqual(cal.radius_px, 477.0)

    def test_center_outside_framebuffer_falls_back(self) -> None:
        data = json.loads(json.dumps(VALID))
        data["circle"]["center_x"] = 1000.0
        path = _write(self.tmpdir, data)
        cal = calibration.load_calibration(path)
        self.assertEqual(cal.center_x, 480.0)

    def test_negative_center_falls_back(self) -> None:
        data = json.loads(json.dumps(VALID))
        data["circle"]["center_y"] = -5.0
        path = _write(self.tmpdir, data)
        cal = calibration.load_calibration(path)
        self.assertEqual(cal.center_y, 480.0)

    def test_safety_margin_zero_falls_back(self) -> None:
        data = json.loads(json.dumps(VALID))
        data["safety_margin_pct"] = 0.0
        path = _write(self.tmpdir, data)
        cal = calibration.load_calibration(path)
        self.assertEqual(cal.safety_margin_pct, calibration.FALLBACK_SAFETY_MARGIN_PCT)

    def test_safety_margin_above_one_falls_back(self) -> None:
        data = json.loads(json.dumps(VALID))
        data["safety_margin_pct"] = 1.5
        path = _write(self.tmpdir, data)
        cal = calibration.load_calibration(path)
        self.assertEqual(cal.safety_margin_pct, calibration.FALLBACK_SAFETY_MARGIN_PCT)

    def test_missing_safety_margin_defaults(self) -> None:
        data = json.loads(json.dumps(VALID))
        del data["safety_margin_pct"]
        path = _write(self.tmpdir, data)
        cal = calibration.load_calibration(path)
        self.assertEqual(cal.safety_margin_pct, calibration.FALLBACK_SAFETY_MARGIN_PCT)

    def test_never_raises_on_directory_instead_of_file(self) -> None:
        # path.read_text() on a directory raises OSError (IsADirectoryError)
        # — confirm load_calibration absorbs it rather than propagating.
        try:
            cal = calibration.load_calibration(self.tmpdir)
        except Exception as exc:  # noqa: BLE001 - this is the assertion
            self.fail(f"load_calibration raised {exc!r} instead of falling back")
        self.assertEqual(cal.radius_px, 960.0 / 2 * 0.9)

    def test_real_repo_calibration_file_is_valid(self) -> None:
        """The actual tracked config/calibration.json must itself pass
        validation — catches a hand-edit mistake before it ships."""
        real_path = Path(__file__).parent / "config" / "calibration.json"
        cal = calibration.load_calibration(real_path)
        # A valid file's radius will never equal the fallback's 90%-of-960
        # sentinel value by coincidence (432.0) - the fallback path would
        # only produce that exact number.
        self.assertNotEqual(cal.radius_px, 960.0 / 2 * 0.9)


class TargetScreenTests(unittest.TestCase):
    """`target_screen` block, read for the first time in Step 4.

    It has been an unused slot in the schema since Step -1, so the
    behaviour that matters most is that a file *without* it still
    behaves exactly as it did before.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _load(self, target) -> calibration.Calibration:
        data = dict(VALID)
        if target is _ABSENT:
            data.pop("target_screen", None)
        else:
            data["target_screen"] = target
        return calibration.load_calibration(_write(self.tmpdir, data))

    def test_a_file_without_the_block_uses_the_heuristic(self) -> None:
        loaded = self._load(_ABSENT)
        self.assertEqual(
            loaded.target_strategy, "match_by_resolution_excluding_main"
        )
        self.assertEqual(loaded.target_display_id, "")

    def test_an_explicit_choice_is_read(self) -> None:
        loaded = self._load(
            {"resolve_strategy": "explicit", "display_id": "6201-28674-1"}
        )
        self.assertEqual(loaded.target_strategy, "explicit")
        self.assertEqual(loaded.target_display_id, "6201-28674-1")

    def test_a_malformed_block_cannot_invalidate_a_good_circle(self) -> None:
        """The worst a bad target can do is fall back to the heuristic —
        which is what every calibration file did before this block was
        read at all."""
        for target in ([], "explicit", {"display_id": 7}, {"resolve_strategy": 3}, None):
            with self.subTest(target=target):
                loaded = self._load(target)
                # The circle is intact...
                self.assertEqual(loaded.center_x, 483.0)
                self.assertEqual(loaded.radius_px, 472.0)
                # ...and the target degraded rather than being trusted.
                self.assertEqual(loaded.target_display_id, "")

    def test_an_explicit_strategy_with_no_id_is_not_usable(self) -> None:
        loaded = self._load({"resolve_strategy": "explicit", "display_id": "  "})
        self.assertEqual(loaded.target_display_id, "")


#: Distinguishes "the key was absent" from "the key was None", which get
#: different treatment above.
_ABSENT = object()


class SchemaVersionTests(unittest.TestCase):
    """Schema_version is no longer decorative."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_supported_major_loads_normally(self) -> None:
        path = _write(self.tmpdir, dict(VALID, schema_version=1))
        self.assertEqual(calibration.load_calibration(path).center_x, 483.0)

    def test_additive_minor_version_still_loads(self) -> None:
        """v1 is additive-only: a newer minor must remain readable."""
        path = _write(self.tmpdir, dict(VALID, schema_version="1.4", new_optional_key=True))
        self.assertEqual(calibration.load_calibration(path).center_x, 483.0)

    def test_unknown_major_falls_back_instead_of_half_parsing(self) -> None:
        path = _write(self.tmpdir, dict(VALID, schema_version=2))
        cal = calibration.load_calibration(path)
        self.assertEqual(cal.center_x, 480.0)
        self.assertEqual(cal.radius_px, 960.0 / 2 * 0.9)

    def test_missing_schema_version_is_accepted_as_v1(self) -> None:
        data = {k: v for k, v in VALID.items() if k != "schema_version"}
        path = _write(self.tmpdir, data)
        self.assertEqual(calibration.load_calibration(path).center_x, 483.0)

    def test_uninterpretable_schema_version_falls_back(self) -> None:
        path = _write(self.tmpdir, dict(VALID, schema_version="version one"))
        self.assertEqual(calibration.load_calibration(path).radius_px, 960.0 / 2 * 0.9)


class ResolvedLoadTests(unittest.TestCase):
    """`~/.viewlab/` takes precedence, the bundled seed is
    used when it is absent, and the seed is copied on first read."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self._home_patch = patch("pathlib.Path.home", return_value=self.home)
        self._home_patch.start()

    def tearDown(self) -> None:
        self._home_patch.stop()
        self._tmp.cleanup()

    def test_bundled_seed_used_and_copied_when_user_file_absent(self) -> None:
        resolved = calibration.load_calibration_resolved()
        self.assertEqual(resolved.source, calibration.ConfigSource.BUNDLED_SEED)
        self.assertTrue(paths.calibration_path().is_file())

    def test_user_file_takes_precedence(self) -> None:
        paths.ensure_dir(paths.config_dir())
        paths.calibration_path().write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "framebuffer": {"width": 800, "height": 800},
                    "circle": {"center_x": 400.0, "center_y": 400.0, "radius_px": 300.0},
                }
            )
        )
        resolved = calibration.load_calibration_resolved()
        self.assertEqual(resolved.source, calibration.ConfigSource.USER)
        self.assertEqual(resolved.value.radius_px, 300.0)

    def test_seeded_copy_reproduces_the_shipped_numbers(self) -> None:
        """The check: center=(483.0, 482.0) effective_radius=438.96
        must be unchanged by the relocation."""
        seeded = calibration.load_calibration_resolved().value
        self.assertEqual(seeded.center_x, 483.0)
        self.assertEqual(seeded.center_y, 482.0)
        self.assertAlmostEqual(seeded.effective_radius_px, 438.96, places=2)

    def test_fallback_when_neither_file_is_usable(self) -> None:
        with patch.object(paths, "bundled_calibration_path", return_value=self.home / "nope.json"):
            resolved = calibration.load_calibration_resolved()
        self.assertEqual(resolved.source, calibration.ConfigSource.FALLBACK)
        self.assertIsNone(resolved.path)

    def test_watcher_watches_the_user_path_only(self) -> None:
        watcher = calibration.calibration_watcher()
        self.assertEqual(watcher.path, paths.calibration_path())
        self.assertFalse(watcher.path.is_relative_to(paths.bundled_config_dir()))


class ApplyPreviewTests(unittest.TestCase):
    """Transient live nudge, as applied by the display.

    This is the mechanism the whole calibration window depends on: the
    UI never writes `calibration.json` while nudging, so if these
    overlays are wrong the user is aligning a ring against a picture that
    is not where the ring says it is.
    """

    BASE = calibration.Calibration(
        framebuffer_width=960.0,
        framebuffer_height=960.0,
        center_x=483.0,
        center_y=482.0,
        radius_px=472.0,
        safety_margin_pct=0.93,
    )

    def test_no_preview_returns_the_base_unchanged(self) -> None:
        """"Clear the preview" and "there was never a preview" have to be
        the same code path, or Cancel has a special case."""
        for preview in (None, {}):
            self.assertIs(calibration.apply_preview(self.BASE, preview), self.BASE)

    def test_a_partial_preview_overlays_only_what_it_carries(self) -> None:
        applied = calibration.apply_preview(self.BASE, {"radius_px": 460.0})
        self.assertEqual(applied.radius_px, 460.0)
        self.assertEqual(applied.center_x, 483.0)
        self.assertEqual(applied.safety_margin_pct, 0.93)

    def test_the_effective_radius_follows_the_preview(self) -> None:
        """What actually moves on the glass."""
        applied = calibration.apply_preview(self.BASE, {"radius_px": 400.0})
        self.assertAlmostEqual(applied.effective_radius_px, 372.0, places=6)

    def test_an_out_of_range_combination_is_refused_whole(self) -> None:
        """The fields are individually legal; together they run the
        circle off the canvas. Nothing upstream checks the combination —
        `control` allow-lists keys and rejects NaN, and that is all."""
        applied = calibration.apply_preview(
            self.BASE, {"center_x": 100.0, "radius_px": 472.0}
        )
        self.assertIs(applied, self.BASE, "half-applied is worse than not applied")

    def test_framebuffer_dimensions_are_not_overridable(self) -> None:
        """`get_view_screen()` keys off them, so a preview that could
        change them could move the overlay to another monitor mid-nudge."""
        applied = calibration.apply_preview(
            self.BASE, {"framebuffer_width": 100.0, "width": 100.0}
        )
        self.assertEqual(applied.framebuffer_width, 960.0)
        self.assertEqual(applied.framebuffer_height, 960.0)

    def test_hostile_values_never_raise_and_never_apply(self) -> None:
        for preview in (
            {"radius_px": "big"},
            {"radius_px": None},
            {"radius_px": True},
            {"center_x": float("nan")},
            {"center_y": float("inf")},
        ):
            self.assertEqual(
                calibration.apply_preview(self.BASE, preview), self.BASE, preview
            )

    def test_a_preview_the_ui_can_produce_is_always_accepted(self) -> None:
        """Pins `apply_preview` against the window's own clamping: every
        value the calibration window can put on the wire has to survive
        this function, or a legal nudge would silently not move."""
        from ui import calibrate_state as cs

        bounds = cs.Bounds(width=960.0, height=960.0)
        for raw in (
            cs.CircleValues(0.0, 0.0, 9000.0),
            cs.CircleValues(9000.0, 480.0, 1.0),
            cs.CircleValues(480.0, 959.0, 480.0),
            cs.CircleValues(483.0, 482.0, 472.0),
        ):
            session = cs.CalibrationSession(
                saved=cs.clamp_values(raw, bounds),
                defaults=cs.CircleValues(483.0, 482.0, 472.0),
                bounds=bounds,
                safety_margin_pct=0.93,
            )
            applied = calibration.apply_preview(self.BASE, session.preview_payload())
            self.assertIsNot(applied, self.BASE, raw)
            self.assertAlmostEqual(applied.radius_px, session.values.radius_px)


if __name__ == "__main__":
    unittest.main()
