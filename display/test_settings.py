"""Unit tests for settings.py — same failure philosophy as
test_calibration.py: every bad-input path falls back to the same safe
default, never raises.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from display import paths
from display import settings

VALID = {
    "rotation_interval_s": 900,
    "poll_interval_s": 1800,
    "fade_duration_s": 2.0,
    "image_studio_base_url": "http://images.example.test:8883",
    "pool": "starred",
    "cache_max": 300,
}


def _write(tmpdir: Path, data: dict) -> Path:
    path = tmpdir / "settings.json"
    path.write_text(json.dumps(data))
    return path


class LoadSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_valid_file_loads_exactly(self) -> None:
        s = settings.load_settings(_write(self.tmpdir, VALID))
        self.assertEqual(s.rotation_interval_s, 900.0)
        self.assertEqual(s.poll_interval_s, 1800.0)
        self.assertEqual(s.fade_duration_s, 2.0)
        self.assertEqual(s.image_studio_base_url, "http://images.example.test:8883")
        self.assertEqual(s.pool, "starred")
        self.assertEqual(s.cache_max, 300)

    def test_missing_file_falls_back(self) -> None:
        s = settings.load_settings(self.tmpdir / "nope.json")
        self.assertEqual(s.rotation_interval_s, settings.FALLBACK_ROTATION_INTERVAL_S)

    def test_malformed_json_falls_back(self) -> None:
        path = self.tmpdir / "settings.json"
        path.write_text("{not json")
        s = settings.load_settings(path)
        self.assertEqual(s.pool, settings.FALLBACK_POOL)

    def test_wrong_shape_falls_back(self) -> None:
        path = self.tmpdir / "settings.json"
        path.write_text(json.dumps([1, 2, 3]))
        s = settings.load_settings(path)
        self.assertEqual(s.cache_max, settings.FALLBACK_CACHE_MAX)

    def test_zero_rotation_interval_falls_back(self) -> None:
        data = {**VALID, "rotation_interval_s": 0}
        s = settings.load_settings(_write(self.tmpdir, data))
        self.assertEqual(s.rotation_interval_s, settings.FALLBACK_ROTATION_INTERVAL_S)

    def test_negative_poll_interval_falls_back(self) -> None:
        data = {**VALID, "poll_interval_s": -5}
        s = settings.load_settings(_write(self.tmpdir, data))
        self.assertEqual(s.poll_interval_s, settings.FALLBACK_POLL_INTERVAL_S)

    def test_negative_fade_duration_falls_back(self) -> None:
        data = {**VALID, "fade_duration_s": -1.0}
        s = settings.load_settings(_write(self.tmpdir, data))
        self.assertEqual(s.fade_duration_s, settings.FALLBACK_FADE_DURATION_S)

    def test_zero_fade_duration_is_valid(self) -> None:
        """0 is a legitimate value — window.py treats duration<=0 as an
        instant cut, not an error."""
        data = {**VALID, "fade_duration_s": 0}
        s = settings.load_settings(_write(self.tmpdir, data))
        self.assertEqual(s.fade_duration_s, 0.0)

    def test_invalid_pool_falls_back(self) -> None:
        data = {**VALID, "pool": "everything"}
        s = settings.load_settings(_write(self.tmpdir, data))
        self.assertEqual(s.pool, settings.FALLBACK_POOL)

    def test_pool_all_is_valid(self) -> None:
        data = {**VALID, "pool": "all"}
        s = settings.load_settings(_write(self.tmpdir, data))
        self.assertEqual(s.pool, "all")

    def test_non_http_base_url_falls_back(self) -> None:
        data = {**VALID, "image_studio_base_url": "ftp://images.example.test"}
        s = settings.load_settings(_write(self.tmpdir, data))
        self.assertEqual(
            s.image_studio_base_url, settings.FALLBACK_IMAGE_STUDIO_BASE_URL
        )

    def test_zero_cache_max_falls_back(self) -> None:
        data = {**VALID, "cache_max": 0}
        s = settings.load_settings(_write(self.tmpdir, data))
        self.assertEqual(s.cache_max, settings.FALLBACK_CACHE_MAX)

    def test_missing_individual_field_uses_that_fields_default(self) -> None:
        data = {k: v for k, v in VALID.items() if k != "cache_max"}
        s = settings.load_settings(_write(self.tmpdir, data))
        self.assertEqual(s.cache_max, settings.FALLBACK_CACHE_MAX)
        self.assertEqual(s.pool, "starred")  # other fields still load correctly

    def test_never_raises_on_directory_instead_of_file(self) -> None:
        try:
            s = settings.load_settings(self.tmpdir)
        except Exception as exc:  # noqa: BLE001 - this is the assertion
            self.fail(f"load_settings raised {exc!r} instead of falling back")
        self.assertEqual(s.rotation_interval_s, settings.FALLBACK_ROTATION_INTERVAL_S)

    def test_real_repo_settings_file_is_valid(self) -> None:
        real_path = Path(__file__).parent / "config" / "settings.json"
        s = settings.load_settings(real_path)
        self.assertEqual(s.rotation_interval_s, 900.0)
        self.assertEqual(s.pool, "starred")


class SchemaVersionTests(unittest.TestCase):
    """Schema-version handling."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_shipped_shape_without_schema_version_is_accepted(self) -> None:
        """The tracked settings.json has no schema_version at all."""
        path = _write(self.tmpdir, VALID)
        self.assertEqual(settings.load_settings(path).rotation_interval_s, 900.0)

    def test_unknown_major_falls_back(self) -> None:
        path = _write(self.tmpdir, dict(VALID, schema_version=3, rotation_interval_s=60))
        loaded = settings.load_settings(path)
        self.assertEqual(loaded.rotation_interval_s, settings.FALLBACK_ROTATION_INTERVAL_S)

    def test_supported_major_loads_normally(self) -> None:
        path = _write(self.tmpdir, dict(VALID, schema_version=1, rotation_interval_s=60))
        self.assertEqual(settings.load_settings(path).rotation_interval_s, 60.0)


class ResolvedLoadTests(unittest.TestCase):
    """Mirrors test_calibration.ResolvedLoadTests."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self._home_patch = patch("pathlib.Path.home", return_value=self.home)
        self._home_patch.start()

    def tearDown(self) -> None:
        self._home_patch.stop()
        self._tmp.cleanup()

    def test_bundled_seed_used_and_copied_when_user_file_absent(self) -> None:
        resolved = settings.load_settings_resolved()
        self.assertEqual(resolved.source, settings.ConfigSource.BUNDLED_SEED)
        self.assertTrue(paths.settings_path().is_file())
        self.assertEqual(resolved.value.pool, "starred")

    def test_user_file_takes_precedence(self) -> None:
        paths.ensure_dir(paths.config_dir())
        paths.settings_path().write_text(json.dumps(dict(VALID, pool="all")))
        resolved = settings.load_settings_resolved()
        self.assertEqual(resolved.source, settings.ConfigSource.USER)
        self.assertEqual(resolved.value.pool, "all")

    def test_fallback_when_neither_file_is_usable(self) -> None:
        with patch.object(paths, "bundled_settings_path", return_value=self.home / "nope.json"):
            resolved = settings.load_settings_resolved()
        self.assertEqual(resolved.source, settings.ConfigSource.FALLBACK)

    def test_watcher_watches_the_user_path_only(self) -> None:
        watcher = settings.settings_watcher()
        self.assertEqual(watcher.path, paths.settings_path())
        self.assertFalse(watcher.path.is_relative_to(paths.bundled_config_dir()))


if __name__ == "__main__":
    unittest.main()
