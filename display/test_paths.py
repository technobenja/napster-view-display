"""Unit tests for paths.py.

The property that matters here is *negative*: no writable path may
resolve inside the source tree (which becomes the read-only, code-signed
`Contents/Resources/` of ImageView.app). Several tests below assert that
directly rather than asserting a specific literal path, so they keep
holding if a root moves.

Isolation: `Path.home()` is patched per-test, which works because every
function in paths.py resolves it at call time rather than at import.

    ./.venv/bin/python3 -m unittest test_paths -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from display import paths


class RootLocationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self._home_patch = patch("pathlib.Path.home", return_value=self.home)
        self._home_patch.start()

    def tearDown(self) -> None:
        self._home_patch.stop()
        self._tmp.cleanup()

    def test_config_dir_is_dot_viewlab_in_home(self) -> None:
        self.assertEqual(paths.config_dir(), self.home / ".viewlab")

    def test_state_dir_is_under_config_dir(self) -> None:
        self.assertEqual(paths.state_dir(), self.home / ".viewlab" / "state")

    def test_cache_dir_is_under_library_caches_keyed_by_bundle_id(self) -> None:
        self.assertEqual(
            paths.cache_dir(), self.home / "Library" / "Caches" / paths.BUNDLE_ID
        )

    def test_log_dir_is_under_library_logs_by_app_name(self) -> None:
        self.assertEqual(paths.log_dir(), self.home / "Library" / "Logs" / "ImageView")

    def test_config_files_live_in_config_dir(self) -> None:
        self.assertEqual(paths.calibration_path(), self.home / ".viewlab" / "calibration.json")
        self.assertEqual(paths.settings_path(), self.home / ".viewlab" / "settings.json")

    def test_state_files_live_in_state_dir(self) -> None:
        state = self.home / ".viewlab" / "state"
        self.assertEqual(paths.rotation_state_path(), state / "rotation_state.json")
        self.assertEqual(paths.status_path(), state / "status.json")
        self.assertEqual(paths.command_path(), state / "command.json")

    def test_lock_file_is_in_config_dir_not_state_dir(self) -> None:
        """Flock is taken at startup, before any state has
        necessarily been written — so it must not depend on state_dir()
        existing."""
        self.assertEqual(paths.lock_path(), self.home / ".viewlab" / "display.lock")

    def test_log_files_live_in_log_dir(self) -> None:
        log_dir = self.home / "Library" / "Logs" / "ImageView"
        self.assertEqual(paths.stdout_log_path(), log_dir / "display.stdout.log")
        self.assertEqual(paths.stderr_log_path(), log_dir / "display.stderr.log")

    def test_home_is_resolved_at_call_time_not_import_time(self) -> None:
        """The whole isolation strategy — and the packaging requirement
        that nothing be computed against a home directory that may not be
        readable at import — depends on this."""
        first = paths.config_dir()
        with tempfile.TemporaryDirectory() as other:
            with patch("pathlib.Path.home", return_value=Path(other)):
                self.assertNotEqual(paths.config_dir(), first)


class BundleSafetyTests(unittest.TestCase):
    """Nothing writable may live inside the app bundle."""

    def test_no_writable_path_is_inside_the_source_tree(self) -> None:
        source_tree = Path(paths.__file__).resolve().parent
        writable = [
            paths.config_dir(),
            paths.state_dir(),
            paths.cache_dir(),
            paths.log_dir(),
            paths.calibration_path(),
            paths.settings_path(),
            paths.rotation_state_path(),
            paths.status_path(),
            paths.command_path(),
            paths.lock_path(),
            paths.stdout_log_path(),
            paths.stderr_log_path(),
        ]
        for candidate in writable:
            with self.subTest(path=candidate):
                self.assertFalse(
                    candidate.resolve().is_relative_to(source_tree),
                    f"{candidate} resolves inside the bundle; writing there "
                    f"would invalidate the ad-hoc code signature.",
                )

    def test_bundled_config_dir_is_inside_the_source_tree(self) -> None:
        """The read-only seed is the one path that *should* be in the
        bundle."""
        source_tree = Path(paths.__file__).resolve().parent
        self.assertTrue(paths.bundled_config_dir().is_relative_to(source_tree))
        self.assertTrue(paths.bundled_calibration_path().is_file())
        self.assertTrue(paths.bundled_settings_path().is_file())

    def test_bundle_id_is_not_com_remy(self) -> None:
        """This is ruled out explicitly, and `com\\.remy\\.` is added to the
        HARD leak-scan grep: the shipped identifier lands in every stranger's
        ~/Library/LaunchAgents, launchctl list, Info.plist, and code
        signature."""
        self.assertNotIn("com.remy", paths.BUNDLE_ID)
        self.assertNotIn("remy", paths.BUNDLE_ID.lower())


class NoBundleRelativeWritablePathsTest(unittest.TestCase):
    """Structural tripwire in the style of test_no_generate_calls.py.

    finding was that *every* writable path in the app was
    bundle-relative, and the reason it survived so long is that nothing
    failed — it works perfectly from a git checkout and only breaks once
    packaged and signed. A committed test is the only thing that stops it
    being reintroduced by the next module that needs somewhere to write.
    """

    DISPLAY_DIR = Path(__file__).resolve().parent
    # Directory names that, joined onto the source tree, would be
    # writable state inside the bundle.
    FORBIDDEN_SUFFIXES = ('"state"', '"cache"', '"logs"', "'state'", "'cache'", "'logs'")

    def test_no_module_joins_a_writable_dir_onto_its_own_location(self) -> None:
        offenders = []
        for path in sorted(self.DISPLAY_DIR.rglob("*.py")):
            if path.name.startswith("test_"):
                continue
            if ".venv" in path.parts or "__pycache__" in path.parts:
                continue
            for line in path.read_text(errors="ignore").splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if "Path(__file__).parent" not in stripped:
                    continue
                if any(suffix in stripped for suffix in self.FORBIDDEN_SUFFIXES):
                    offenders.append(f"{path.name}: {stripped}")
        self.assertEqual(
            offenders,
            [],
            "Writable paths must come from paths.py, not from the module's "
            "own location: inside ImageView.app that is read-only, and "
            "writing there invalidates the code signature. "
            f"Offenders: {offenders}",
        )


class EnsureDirTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_creates_directory_and_parents_on_demand(self) -> None:
        target = self.tmpdir / "a" / "b" / "c"
        self.assertTrue(paths.ensure_dir(target))
        self.assertTrue(target.is_dir())

    def test_existing_directory_is_fine(self) -> None:
        self.assertTrue(paths.ensure_dir(self.tmpdir))
        self.assertTrue(paths.ensure_dir(self.tmpdir))

    def test_failure_returns_false_and_does_not_raise(self) -> None:
        """A read-only home must degrade to "runs, persists nothing",
        never to a crash at startup."""
        blocker = self.tmpdir / "not-a-dir"
        blocker.write_text("i am a file")
        self.assertFalse(paths.ensure_dir(blocker / "child"))

    def test_permission_denied_returns_false_and_does_not_raise(self) -> None:
        locked = self.tmpdir / "locked"
        locked.mkdir()
        locked.chmod(0o500)
        try:
            self.assertFalse(paths.ensure_dir(locked / "child"))
        finally:
            locked.chmod(0o700)

    def test_ensure_all_creates_every_root(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            with patch("pathlib.Path.home", return_value=Path(home)):
                self.assertTrue(paths.ensure_all())
                self.assertTrue(paths.config_dir().is_dir())
                self.assertTrue(paths.state_dir().is_dir())
                self.assertTrue(paths.cache_dir().is_dir())
                self.assertTrue(paths.log_dir().is_dir())

    def test_ensure_all_reports_false_when_a_root_cannot_be_created(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            home_path = Path(home)
            # Occupy ~/.viewlab with a plain file so mkdir() fails there
            # but the other three roots still succeed.
            (home_path / ".viewlab").write_text("blocked")
            with patch("pathlib.Path.home", return_value=home_path):
                self.assertFalse(paths.ensure_all())
                self.assertTrue(paths.cache_dir().is_dir())


class UiLockTests(unittest.TestCase):
    """Step 5: the menu bar needs its own guard, and specifically not
    the display's — sharing one lock would mean starting the UI killed
    the display."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self._home_patch = patch("pathlib.Path.home", return_value=self.home)
        self._home_patch.start()

    def tearDown(self) -> None:
        self._home_patch.stop()
        self._tmp.cleanup()

    def test_ui_lock_is_a_distinct_file_from_the_display_lock(self) -> None:
        self.assertNotEqual(paths.ui_lock_path(), paths.lock_path())

    def test_ui_lock_is_in_config_dir(self) -> None:
        self.assertEqual(paths.ui_lock_path(), self.home / ".viewlab" / "ui.lock")


class AgentLabelTests(unittest.TestCase):
    def test_labels_derive_from_the_bundle_id(self) -> None:
        self.assertEqual(paths.UI_AGENT_LABEL, f"{paths.BUNDLE_ID}.ui")
        self.assertEqual(paths.DISPLAY_AGENT_LABEL, f"{paths.BUNDLE_ID}.display")

    def test_shipped_labels_are_distinct(self) -> None:
        self.assertNotEqual(paths.UI_AGENT_LABEL, paths.DISPLAY_AGENT_LABEL)

    def test_shipped_labels_carry_no_personal_identity(self) -> None:
        """`com\\.remy\\.` is on the HARD grep list; these land in
        every user's ~/Library/LaunchAgents/."""
        for label in (paths.UI_AGENT_LABEL, paths.DISPLAY_AGENT_LABEL):
            self.assertNotIn("remy", label.lower())
            self.assertFalse(label.startswith("com."))

    def test_the_shipped_display_label_is_project_scoped(self) -> None:
        """Any pre-existing source-tree label is kept out of scope. If
        the shipped label ever collided with one, installing the app
        would fight the running service.

        Asserted as a property (the shipped label is project-scoped under
        `dev.viewlab.`) rather than by naming the old label literally —
        spelling it out here would reintroduce the very identity string
        the release gate exists to keep out of published files.
        """
        self.assertTrue(paths.DISPLAY_AGENT_LABEL.startswith("dev.viewlab."))


class MenubarTemplateTests(unittest.TestCase):
    """The icon has to load from inside the bundle. Getting this wrong
    is invisible on the build machine — the source tree is right there,
    so it loads, and ships broken to everyone else."""

    def test_source_layout_points_at_the_packaging_directory(self) -> None:
        with patch.object(paths.sys, "frozen", False, create=True):
            path = paths.menubar_template_path()
        self.assertEqual(path.parent.name, "packaging")
        self.assertEqual(path.name, "menubar-template.pdf")

    def test_the_source_asset_actually_exists(self) -> None:
        """It is generated by packaging/make_menubar_template.py and
        committed; if it goes missing the menu bar comes up bare."""
        with patch.object(paths.sys, "frozen", False, create=True):
            self.assertTrue(paths.menubar_template_path().is_file())

    def test_resources_dir_is_none_when_not_frozen(self) -> None:
        with patch.object(paths.sys, "frozen", False, create=True):
            self.assertIsNone(paths.bundled_resources_dir())

    def test_frozen_layout_is_resources_sibling_of_macos(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # .resolve() because paths.bundled_resources_dir() resolves
            # sys.executable, and /var is a symlink to /private/var on
            # macOS — comparing unresolved would fail for a reason that
            # has nothing to do with the behaviour under test.
            contents = (Path(tmp) / "ImageView.app" / "Contents").resolve()
            (contents / "MacOS").mkdir(parents=True)
            executable = contents / "MacOS" / "ImageView"
            executable.write_text("")
            with patch.object(paths.sys, "frozen", True, create=True):
                with patch.object(paths.sys, "executable", str(executable)):
                    self.assertEqual(
                        paths.bundled_resources_dir(), contents / "Resources"
                    )

    def test_frozen_template_is_flat_in_resources(self) -> None:
        """py2app flattens `resources` entries to the top of
        Contents/Resources/ — assuming a `packaging/` subdirectory would
        load nothing."""
        with tempfile.TemporaryDirectory() as tmp:
            # .resolve() because paths.bundled_resources_dir() resolves
            # sys.executable, and /var is a symlink to /private/var on
            # macOS — comparing unresolved would fail for a reason that
            # has nothing to do with the behaviour under test.
            contents = (Path(tmp) / "ImageView.app" / "Contents").resolve()
            (contents / "MacOS").mkdir(parents=True)
            (contents / "Resources").mkdir(parents=True)
            executable = contents / "MacOS" / "ImageView"
            executable.write_text("")
            asset = contents / "Resources" / "menubar-template.pdf"
            asset.write_bytes(b"%PDF-1.4")
            with patch.object(paths.sys, "frozen", True, create=True):
                with patch.object(paths.sys, "executable", str(executable)):
                    self.assertEqual(paths.menubar_template_path(), asset)

    def test_frozen_but_missing_resource_falls_back_to_source(self) -> None:
        """Same rule as bundled_config_dir(): return a real path the
        caller can report rather than a fabricated one."""
        with tempfile.TemporaryDirectory() as tmp:
            # .resolve() because paths.bundled_resources_dir() resolves
            # sys.executable, and /var is a symlink to /private/var on
            # macOS — comparing unresolved would fail for a reason that
            # has nothing to do with the behaviour under test.
            contents = (Path(tmp) / "ImageView.app" / "Contents").resolve()
            (contents / "MacOS").mkdir(parents=True)
            executable = contents / "MacOS" / "ImageView"
            executable.write_text("")
            with patch.object(paths.sys, "frozen", True, create=True):
                with patch.object(paths.sys, "executable", str(executable)):
                    self.assertEqual(
                        paths.menubar_template_path().parent.name, "packaging"
                    )


if __name__ == "__main__":
    unittest.main()
