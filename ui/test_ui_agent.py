"""Unit tests for ui_agent.py — the menu bar's LaunchAgent.

Everything here runs against `build_plist()` and `write_plist()`, which
is the whole reason those are separated from `install()`: the plist's
*contents* are the part that has to be right, and asserting on them
must not require loading a real service into the user's GUI domain.

Nothing in this file shells out to launchctl. `bootout`'s
"not loaded is success" rule is the one launchctl behaviour with a test,
and it is tested by substituting the subprocess call.

    ../display/.venv/bin/python3 -m unittest test_ui_agent -v
"""

from __future__ import annotations

import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from display import paths
from ui import ui_agent


class LabelValidationTests(unittest.TestCase):
    """The label is validated because it becomes both a filename
    and a plist key, and the plist is executed by launchd at login."""

    def test_the_shipped_label_is_valid(self) -> None:
        self.assertEqual(
            ui_agent.validate_label(paths.UI_AGENT_LABEL), paths.UI_AGENT_LABEL
        )

    def test_the_shipped_label_is_the_bundle_id_plus_ui(self) -> None:
        self.assertEqual(paths.UI_AGENT_LABEL, f"{paths.BUNDLE_ID}.ui")

    def test_the_shipped_label_carries_no_personal_identity(self) -> None:
        """`com\\.remy\\.` is on the HARD grep list, and this label
        lands in every user's ~/Library/LaunchAgents/."""
        self.assertNotIn("remy", paths.UI_AGENT_LABEL.lower())

    def test_path_separators_are_rejected(self) -> None:
        with self.assertRaises(ui_agent.LabelError):
            ui_agent.validate_label("dev.viewlab/../../evil")

    def test_xml_injection_is_rejected(self) -> None:
        with self.assertRaises(ui_agent.LabelError):
            ui_agent.validate_label(
                "a</string><key>ProgramArguments</key><string>x"
            )

    def test_uppercase_is_rejected(self) -> None:
        with self.assertRaises(ui_agent.LabelError):
            ui_agent.validate_label("Dev.ViewLab.ImageView.ui")

    def test_empty_is_rejected(self) -> None:
        with self.assertRaises(ui_agent.LabelError):
            ui_agent.validate_label("")

    def test_plist_path_validates_its_label(self) -> None:
        with self.assertRaises(ui_agent.LabelError):
            ui_agent.plist_path("../../../etc/evil")

    def test_plist_path_lands_in_launchagents(self) -> None:
        path = ui_agent.plist_path(paths.UI_AGENT_LABEL)
        self.assertEqual(path.parent, Path.home() / "Library" / "LaunchAgents")
        self.assertEqual(path.name, f"{paths.UI_AGENT_LABEL}.plist")


class BuildPlistTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = Path("/Applications/ImageView.app")
        self.data = ui_agent.build_plist(paths.UI_AGENT_LABEL, self.app)

    def test_label_matches(self) -> None:
        self.assertEqual(self.data["Label"], paths.UI_AGENT_LABEL)

    def test_program_points_at_the_bundle_executable(self) -> None:
        self.assertEqual(
            self.data["ProgramArguments"],
            ["/Applications/ImageView.app/Contents/MacOS/ImageView"],
        )

    def test_no_arguments_are_passed(self) -> None:
        """The no-arg path is the menu bar; `--display` would make this
        the display agent instead, on a label that says `.ui`."""
        self.assertEqual(len(self.data["ProgramArguments"]), 1)

    def test_open_dash_a_is_not_used(self) -> None:
        """The packaging rules it out: it returns immediately, so launchd sees a
        job that instantly exits."""
        self.assertNotIn("open", self.data["ProgramArguments"][0].split("/"))

    def test_run_at_load(self) -> None:
        self.assertIs(self.data["RunAtLoad"], True)

    def test_keepalive_is_conditional_not_true(self) -> None:
        """Plain `KeepAlive: true` would relaunch the app seconds after
        the user chose Quit. That is the single most important assertion
        in this file."""
        self.assertIsInstance(self.data["KeepAlive"], dict)
        self.assertIs(self.data["KeepAlive"]["SuccessfulExit"], False)

    def test_keepalive_does_not_respawn_a_clean_exit(self) -> None:
        """Restated as the property that matters: Quit exits 0, and the
        single-instance guard exits 0, and neither may be respawned."""
        self.assertFalse(self.data["KeepAlive"].get("SuccessfulExit", True))

    def test_throttle_interval_is_set(self) -> None:
        self.assertEqual(
            self.data["ThrottleInterval"], ui_agent.THROTTLE_INTERVAL_S
        )

    def test_logs_go_to_the_app_log_directory(self) -> None:
        for key in ("StandardOutPath", "StandardErrorPath"):
            self.assertTrue(
                self.data[key].startswith(str(paths.log_dir())),
                f"{key} = {self.data[key]}",
            )

    def test_no_working_directory(self) -> None:
        """Nothing in the bundle resolves paths relative to cwd — every
        one comes from paths.py. An unnecessary WorkingDirectory would
        be a path that has to stay valid for no reason."""
        self.assertNotIn("WorkingDirectory", self.data)

    def test_label_is_validated_before_the_dict_is_built(self) -> None:
        with self.assertRaises(ui_agent.LabelError):
            ui_agent.build_plist("NOT A LABEL", self.app)

    def test_it_is_plist_serialisable(self) -> None:
        """A dict that dumps is the whole point of returning a dict."""
        self.assertIsInstance(plistlib.dumps(self.data), bytes)


class WritePlistTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "agent.plist"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_round_trips(self) -> None:
        data = ui_agent.build_plist(paths.UI_AGENT_LABEL)
        self.assertTrue(ui_agent.write_plist(self.path, data))
        with self.path.open("rb") as handle:
            self.assertEqual(plistlib.load(handle), data)

    def test_creates_the_parent_directory(self) -> None:
        nested = Path(self._tmp.name) / "a" / "b" / "agent.plist"
        self.assertTrue(
            ui_agent.write_plist(nested, ui_agent.build_plist(paths.UI_AGENT_LABEL))
        )
        self.assertTrue(nested.is_file())

    def test_leaves_no_temp_files_behind(self) -> None:
        ui_agent.write_plist(self.path, ui_agent.build_plist(paths.UI_AGENT_LABEL))
        leftovers = [p.name for p in self.path.parent.iterdir() if p.name.startswith(".tmp-")]
        self.assertEqual(leftovers, [])

    def test_a_hostile_path_is_escaped_not_injected(self) -> None:
        """The reason for plistlib over string templating. A value
        containing plist markup must come back as that same *string*,
        not as extra keys."""
        hostile = "/tmp/a</string><key>ProgramArguments</key><array><string>/bin/sh"
        data = ui_agent.build_plist(paths.UI_AGENT_LABEL, Path(hostile))
        self.assertTrue(ui_agent.write_plist(self.path, data))
        with self.path.open("rb") as handle:
            loaded = plistlib.load(handle)
        self.assertEqual(loaded["ProgramArguments"], data["ProgramArguments"])
        self.assertEqual(len(loaded["ProgramArguments"]), 1)
        self.assertIn(hostile, loaded["ProgramArguments"][0])

    def test_an_unwritable_path_returns_false_rather_than_raising(self) -> None:
        blocker = Path(self._tmp.name) / "blocker"
        blocker.write_text("i am a file")
        self.assertFalse(
            ui_agent.write_plist(
                blocker / "agent.plist", ui_agent.build_plist(paths.UI_AGENT_LABEL)
            )
        )


class BootoutTests(unittest.TestCase):
    """"bootout exits non-zero when the service is not loaded —
    i.e. for every user except the one who built it. Under `set -e` the install aborts on
    the common path. Treat absent-label as success." """

    def _with_launchctl(self, code: int, err: str = ""):
        return mock.patch.object(ui_agent, "_launchctl", return_value=(code, err))

    def test_success_is_success(self) -> None:
        with self._with_launchctl(0):
            self.assertTrue(ui_agent.bootout(paths.UI_AGENT_LABEL))

    def test_not_loaded_113_is_success(self) -> None:
        with self._with_launchctl(113, "Could not find service"):
            self.assertTrue(ui_agent.bootout(paths.UI_AGENT_LABEL))

    def test_not_loaded_3_is_success(self) -> None:
        with self._with_launchctl(3, "No such process"):
            self.assertTrue(ui_agent.bootout(paths.UI_AGENT_LABEL))

    def test_message_backstop_when_the_code_is_unfamiliar(self) -> None:
        """The numeric codes vary across releases, so the text is
        checked too rather than the numbers being trusted alone."""
        with self._with_launchctl(37, "No such process"):
            self.assertTrue(ui_agent.bootout(paths.UI_AGENT_LABEL))

    def test_a_real_failure_is_still_a_failure(self) -> None:
        with self._with_launchctl(1, "Operation not permitted"):
            self.assertFalse(ui_agent.bootout(paths.UI_AGENT_LABEL))

    def test_it_validates_the_label(self) -> None:
        with self.assertRaises(ui_agent.LabelError):
            ui_agent.bootout("../evil")


class InstallGuardTests(unittest.TestCase):
    def test_install_refuses_when_the_executable_is_missing(self) -> None:
        """A plist pointing at a missing binary is a job that fails at
        login with nothing on screen to explain it, so this is checked
        before anything is written."""
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ui_agent, "_launchctl") as launchctl:
                self.assertFalse(
                    ui_agent.install(paths.UI_AGENT_LABEL, Path(tmp) / "Nope.app")
                )
            launchctl.assert_not_called()


class ExecutablePathTests(unittest.TestCase):
    def test_it_is_the_macos_binary(self) -> None:
        self.assertEqual(
            ui_agent.executable_path(Path("/Applications/ImageView.app")),
            Path("/Applications/ImageView.app/Contents/MacOS/ImageView"),
        )

    def test_it_uses_the_shared_app_name(self) -> None:
        self.assertEqual(
            ui_agent.executable_path(Path("/x/Y.app")).name, paths.APP_NAME
        )


if __name__ == "__main__":
    unittest.main()
