"""Unit tests for config_store.py.

Covers the three things the plan is specific about and that a casual
implementation gets wrong:

* the **exact resolution order**, including the first-run copy into
  `~/.viewlab/` and the fact that a malformed *user* file is never
  overwritten by the seed;
* `schema_version` being **actually read** — an unknown MAJOR refuses the
  file loudly instead of parsing it half-way;
* hot-reload comparing the **`(st_mtime_ns, st_size, st_ino)` triple**,
  and falling back to the **last-good in-memory value** on a malformed
  file rather than to the safe default (mid-calibration a default
  would make the circle jump wildly on every half-typed edit).

    ./.venv/bin/python3 -m unittest test_config_store -v
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from display import config_store
from display.config_store import ConfigSource, WatchedConfig

LABEL = "test.py"


def _validate(data):
    """Stand-in validator: accepts {"value": <int>} and nothing else."""
    value = data.get("value")
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return value


def _fallback() -> int:
    return -1


class ReadJsonObjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_reads_a_json_object(self) -> None:
        path = self.tmpdir / "x.json"
        path.write_text('{"a": 1}')
        self.assertEqual(config_store.read_json_object(path, LABEL), {"a": 1})

    def test_missing_file_returns_none_silently(self) -> None:
        buf = io.StringIO()
        with redirect_stderr(buf):
            self.assertIsNone(config_store.read_json_object(self.tmpdir / "nope.json", LABEL))
        self.assertEqual(buf.getvalue(), "", "an absent file is an expected first-run state")

    def test_malformed_json_returns_none_and_logs(self) -> None:
        path = self.tmpdir / "x.json"
        path.write_text("{not json")
        buf = io.StringIO()
        with redirect_stderr(buf):
            self.assertIsNone(config_store.read_json_object(path, LABEL))
        self.assertIn("not valid JSON", buf.getvalue())

    def test_non_object_top_level_returns_none(self) -> None:
        path = self.tmpdir / "x.json"
        path.write_text("[1, 2, 3]")
        with redirect_stderr(io.StringIO()):
            self.assertIsNone(config_store.read_json_object(path, LABEL))

    def test_unreadable_file_returns_none_and_does_not_raise(self) -> None:
        path = self.tmpdir / "x.json"
        path.write_text('{"a": 1}')
        path.chmod(0o000)
        try:
            buf = io.StringIO()
            with redirect_stderr(buf):
                self.assertIsNone(config_store.read_json_object(path, LABEL))
            self.assertIn("cannot read", buf.getvalue())
        finally:
            path.chmod(0o600)

    def test_directory_in_place_of_file_returns_none(self) -> None:
        path = self.tmpdir / "x.json"
        path.mkdir()
        with redirect_stderr(io.StringIO()):
            self.assertIsNone(config_store.read_json_object(path, LABEL))


class SchemaVersionTests(unittest.TestCase):
    def test_missing_key_is_treated_as_the_supported_major(self) -> None:
        """The shipped settings.json has no schema_version at all;
        refusing it would break every existing install for no benefit."""
        self.assertEqual(config_store.schema_major({}), config_store.SUPPORTED_SCHEMA_MAJOR)

    def test_int_form(self) -> None:
        self.assertEqual(config_store.schema_major({"schema_version": 2}), 2)

    def test_float_and_string_forms_take_the_major_component(self) -> None:
        self.assertEqual(config_store.schema_major({"schema_version": 1.4}), 1)
        self.assertEqual(config_store.schema_major({"schema_version": "1"}), 1)
        self.assertEqual(config_store.schema_major({"schema_version": "2.7"}), 2)

    def test_uninterpretable_values_return_none(self) -> None:
        for value in ("v1", "", None, [1], {"major": 1}, True):
            with self.subTest(value=value):
                self.assertIsNone(config_store.schema_major({"schema_version": value}))

    def test_supported_major_passes(self) -> None:
        self.assertTrue(
            config_store.schema_is_supported({"schema_version": 1}, Path("x"), LABEL)
        )

    def test_unknown_major_is_refused_and_logged_loudly(self) -> None:
        buf = io.StringIO()
        with redirect_stderr(buf):
            ok = config_store.schema_is_supported({"schema_version": 2}, Path("x"), LABEL)
        self.assertFalse(ok)
        self.assertIn("REFUSING IT", buf.getvalue())

    def test_uninterpretable_version_is_refused(self) -> None:
        with redirect_stderr(io.StringIO()):
            self.assertFalse(
                config_store.schema_is_supported({"schema_version": "v1"}, Path("x"), LABEL)
            )


class LoadValidTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_valid_file_loads(self) -> None:
        path = self.tmpdir / "x.json"
        path.write_text(json.dumps({"schema_version": 1, "value": 7}))
        self.assertEqual(config_store.load_valid(path, _validate, LABEL), 7)

    def test_wrong_schema_major_rejected_even_if_otherwise_valid(self) -> None:
        path = self.tmpdir / "x.json"
        path.write_text(json.dumps({"schema_version": 9, "value": 7}))
        with redirect_stderr(io.StringIO()):
            self.assertIsNone(config_store.load_valid(path, _validate, LABEL))

    def test_failing_validation_returns_none(self) -> None:
        path = self.tmpdir / "x.json"
        path.write_text(json.dumps({"value": "not an int"}))
        self.assertIsNone(config_store.load_valid(path, _validate, LABEL))

    def test_a_raising_validator_is_contained(self) -> None:
        path = self.tmpdir / "x.json"
        path.write_text(json.dumps({"value": 1}))

        def boom(data):
            raise RuntimeError("validator bug")

        buf = io.StringIO()
        with redirect_stderr(buf):
            self.assertIsNone(config_store.load_valid(path, boom, LABEL))
        self.assertIn("raised unexpectedly", buf.getvalue())


class ResolutionOrderTests(unittest.TestCase):
    """Resolution order, in order."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.user_path = self.tmpdir / "home" / ".viewlab" / "x.json"
        self.bundled_path = self.tmpdir / "bundle" / "config" / "x.json"
        self.bundled_path.parent.mkdir(parents=True)
        self.bundled_path.write_text(json.dumps({"schema_version": 1, "value": 100}))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _resolve(self):
        return config_store.resolve_config(
            user_path=self.user_path,
            bundled_path=self.bundled_path,
            validate=_validate,
            fallback=_fallback,
            label=LABEL,
        )

    def test_step_1_user_file_wins_when_present_and_valid(self) -> None:
        self.user_path.parent.mkdir(parents=True)
        self.user_path.write_text(json.dumps({"schema_version": 1, "value": 42}))
        resolved = self._resolve()
        self.assertEqual(resolved.value, 42)
        self.assertEqual(resolved.source, ConfigSource.USER)
        self.assertEqual(resolved.path, self.user_path)

    def test_step_2_bundled_seed_used_when_user_file_absent(self) -> None:
        with redirect_stderr(io.StringIO()):
            resolved = self._resolve()
        self.assertEqual(resolved.value, 100)
        self.assertEqual(resolved.source, ConfigSource.BUNDLED_SEED)
        self.assertEqual(resolved.path, self.bundled_path)

    def test_step_2_copies_the_seed_into_the_user_path(self) -> None:
        with redirect_stderr(io.StringIO()):
            self._resolve()
        self.assertTrue(self.user_path.is_file(), "seed must be copied on first read")
        self.assertEqual(
            json.loads(self.user_path.read_text()), {"schema_version": 1, "value": 100}
        )

    def test_the_copy_makes_the_next_resolve_a_user_read(self) -> None:
        with redirect_stderr(io.StringIO()):
            self._resolve()
            second = self._resolve()
        self.assertEqual(second.source, ConfigSource.USER)

    def test_step_3_fallback_when_neither_file_is_usable(self) -> None:
        self.bundled_path.write_text("{ garbage")
        buf = io.StringIO()
        with redirect_stderr(buf):
            resolved = self._resolve()
        self.assertEqual(resolved.value, -1)
        self.assertEqual(resolved.source, ConfigSource.FALLBACK)
        self.assertIsNone(resolved.path)
        self.assertIn("conservative built-in fallback", buf.getvalue())

    def test_unknown_schema_major_in_user_file_falls_through_to_the_seed(self) -> None:
        """Unknown major → bundled fallback, logged loudly."""
        self.user_path.parent.mkdir(parents=True)
        self.user_path.write_text(json.dumps({"schema_version": 7, "value": 42}))
        buf = io.StringIO()
        with redirect_stderr(buf):
            resolved = self._resolve()
        self.assertEqual(resolved.source, ConfigSource.BUNDLED_SEED)
        self.assertEqual(resolved.value, 100)
        self.assertIn("REFUSING IT", buf.getvalue())

    def test_a_malformed_user_file_is_never_overwritten_by_the_seed(self) -> None:
        """calibration.json is hand-measured (will not even delete it
        without prompting) — a parse error must not destroy the user's
        work."""
        self.user_path.parent.mkdir(parents=True)
        self.user_path.write_text("{ half-typed")
        with redirect_stderr(io.StringIO()):
            resolved = self._resolve()
        self.assertEqual(resolved.source, ConfigSource.BUNDLED_SEED)
        self.assertEqual(self.user_path.read_text(), "{ half-typed")

    def test_seed_copy_failure_still_yields_the_bundled_value(self) -> None:
        """A read-only home must not stop the app from running."""
        with patch.object(config_store, "atomic_write_json", side_effect=OSError("read-only")):
            buf = io.StringIO()
            with redirect_stderr(buf):
                resolved = self._resolve()
        self.assertEqual(resolved.value, 100)
        self.assertEqual(resolved.source, ConfigSource.BUNDLED_SEED)
        self.assertIn("could not seed", buf.getvalue())

    def test_unreadable_user_file_falls_through_without_raising(self) -> None:
        self.user_path.parent.mkdir(parents=True)
        self.user_path.write_text(json.dumps({"value": 42}))
        self.user_path.chmod(0o000)
        try:
            with redirect_stderr(io.StringIO()):
                resolved = self._resolve()
            self.assertEqual(resolved.source, ConfigSource.BUNDLED_SEED)
        finally:
            self.user_path.chmod(0o600)


class StampTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_missing_file_stamps_as_none(self) -> None:
        self.assertIsNone(config_store.stamp(self.tmpdir / "nope"))

    def test_stamp_carries_all_three_fields(self) -> None:
        path = self.tmpdir / "x.json"
        path.write_text("abc")
        st = config_store.stamp(path)
        self.assertEqual(st.size, 3)
        self.assertEqual(st.inode, path.stat().st_ino)
        self.assertEqual(st.mtime_ns, path.stat().st_mtime_ns)


class WatchedConfigTests(unittest.TestCase):
    """Hot-reload change detection."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.path = self.tmpdir / "x.json"
        self._write({"schema_version": 1, "value": 1})

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, data) -> None:
        self.path.write_text(json.dumps(data))

    def _watcher(self) -> WatchedConfig:
        return WatchedConfig(self.path, _validate, LABEL)

    def test_startup_state_is_adopted_as_already_seen(self) -> None:
        """The first tick after startup must not re-apply what startup
        just loaded."""
        self.assertIsNone(self._watcher().poll())

    def test_unchanged_file_returns_none(self) -> None:
        watcher = self._watcher()
        self.assertIsNone(watcher.poll())
        self.assertIsNone(watcher.poll())

    def test_changed_and_valid_file_returns_the_new_value(self) -> None:
        watcher = self._watcher()
        self._write({"schema_version": 1, "value": 2})
        with redirect_stderr(io.StringIO()):
            self.assertEqual(watcher.poll(), 2)

    def test_change_is_reported_once_not_every_tick(self) -> None:
        watcher = self._watcher()
        self._write({"schema_version": 1, "value": 2})
        with redirect_stderr(io.StringIO()):
            self.assertEqual(watcher.poll(), 2)
            self.assertIsNone(watcher.poll())

    def test_same_mtime_and_size_but_new_inode_is_detected(self) -> None:
        """The atomic write-then-rename case — this is why st_ino is in
        the triple. mtime alone can be identical after a rename that
        preserves it, and the size is unchanged by construction here."""
        watcher = self._watcher()
        original = config_store.stamp(self.path)
        replacement = self.tmpdir / "replacement.json"
        replacement.write_text(json.dumps({"schema_version": 1, "value": 9}))
        # Force identical mtime and identical size, differing only by inode.
        os.utime(replacement, ns=(original.mtime_ns, original.mtime_ns))
        self.assertEqual(replacement.stat().st_size, original.size)
        os.replace(replacement, self.path)
        new = config_store.stamp(self.path)
        self.assertEqual(new.mtime_ns, original.mtime_ns)
        self.assertEqual(new.size, original.size)
        self.assertNotEqual(new.inode, original.inode)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(watcher.poll(), 9)

    def test_same_mtime_and_inode_but_new_size_is_detected(self) -> None:
        """An in-place edit that keeps the mtime — the case mtime-only
        comparison misses."""
        watcher = self._watcher()
        original = config_store.stamp(self.path)
        self._write({"schema_version": 1, "value": 12345})
        os.utime(self.path, ns=(original.mtime_ns, original.mtime_ns))
        after = config_store.stamp(self.path)
        self.assertEqual(after.mtime_ns, original.mtime_ns)
        self.assertNotEqual(after.size, original.size)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(watcher.poll(), 12345)

    def test_malformed_file_returns_none_so_caller_keeps_last_good(self) -> None:
        """Core requirement: never the safe default."""
        watcher = self._watcher()
        self.path.write_text('{"value": 2')  # half-typed
        buf = io.StringIO()
        with redirect_stderr(buf):
            self.assertIsNone(watcher.poll())
        self.assertIn("keeping the last-good configuration", buf.getvalue())
        self.assertIn("NOT", buf.getvalue())

    def test_malformed_file_complains_once_per_edit_not_every_tick(self) -> None:
        watcher = self._watcher()
        self.path.write_text('{"value": 2')
        with redirect_stderr(io.StringIO()):
            watcher.poll()
        buf = io.StringIO()
        with redirect_stderr(buf):
            self.assertIsNone(watcher.poll())
        self.assertEqual(buf.getvalue(), "")

    def test_recovery_after_a_malformed_edit(self) -> None:
        watcher = self._watcher()
        self.path.write_text("{ broken")
        with redirect_stderr(io.StringIO()):
            self.assertIsNone(watcher.poll())
            self._write({"schema_version": 1, "value": 3})
            self.assertEqual(watcher.poll(), 3)

    def test_wrong_schema_major_keeps_last_good(self) -> None:
        watcher = self._watcher()
        self._write({"schema_version": 4, "value": 2})
        buf = io.StringIO()
        with redirect_stderr(buf):
            self.assertIsNone(watcher.poll())
        self.assertIn("REFUSING IT", buf.getvalue())

    def test_deleted_file_keeps_last_good_and_does_not_reseed(self) -> None:
        watcher = self._watcher()
        self.path.unlink()
        buf = io.StringIO()
        with redirect_stderr(buf):
            self.assertIsNone(watcher.poll())
        self.assertIn("disappeared", buf.getvalue())
        self.assertFalse(self.path.exists(), "poll() must not recreate the file")

    def test_deletion_is_reported_once_not_every_tick(self) -> None:
        watcher = self._watcher()
        self.path.unlink()
        with redirect_stderr(io.StringIO()):
            watcher.poll()
        buf = io.StringIO()
        with redirect_stderr(buf):
            watcher.poll()
        self.assertEqual(buf.getvalue(), "")

    def test_file_restored_after_deletion_is_picked_up(self) -> None:
        watcher = self._watcher()
        self.path.unlink()
        with redirect_stderr(io.StringIO()):
            watcher.poll()
            self._write({"schema_version": 1, "value": 5})
            self.assertEqual(watcher.poll(), 5)

    def test_unreadable_file_keeps_last_good(self) -> None:
        watcher = self._watcher()
        self._write({"schema_version": 1, "value": 2})
        self.path.chmod(0o000)
        try:
            buf = io.StringIO()
            with redirect_stderr(buf):
                self.assertIsNone(watcher.poll())
            self.assertIn("keeping the last-good configuration", buf.getvalue())
        finally:
            self.path.chmod(0o600)

    def test_file_replaced_by_a_directory_keeps_last_good(self) -> None:
        """stat() succeeds and reports a change, but the read fails —
        the one path where the stamp and the content disagree."""
        watcher = self._watcher()
        self.path.unlink()
        self.path.mkdir()
        buf = io.StringIO()
        with redirect_stderr(buf):
            self.assertIsNone(watcher.poll())
        self.assertIn("keeping the last-good configuration", buf.getvalue())

    def test_watches_the_path_it_was_given(self) -> None:
        self.assertEqual(self._watcher().path, self.path)


if __name__ == "__main__":
    unittest.main()
