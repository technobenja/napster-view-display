"""Unit tests for diagnostics.py.

Two things here are behaviour tests rather than assertions about the
shape of the code, deliberately, because both mechanisms fail *silently*
when they are wrong — which is the whole reason they exist:

- The stack dump is proved by **sending the process a real SIGUSR1** and
  reading the stack back out of the file. Asserting that
  `faulthandler.register` had been called would say nothing about whether
  a signal actually produces a dump, or whether it lands in the file
  rather than on a stderr the process has no use for.

  What this cannot prove is that `diagnostics` holds the dump file open.
  The plan expected that to be load-bearing — collect the handle, close
  the fd, dump silently lost — and on CPython 3.13.12 it is not:
  `register(file=...)` keeps its own strong reference. Mutating
  `_dump_file` away leaves every behaviour test green, so the invariant
  is asserted directly below instead, with that limit stated rather than
  papered over.

- The launchd branch is proved by **running `menubar.main()` as a real
  subprocess** with a lock already held and with stderr pointed at a real
  file, then asserting the app's own log was never created. The condition
  it turns on — "is fd 2 already a regular file" — is a property of how
  the process was started and cannot be observed honestly from inside a
  test that did not start it. `test_single_instance.py` reaches for a
  subprocess for the same reason.

- The **ordering** rule — arm, and therefore rotate, only after
  `acquire()` has returned a handle — is proved the same way: a losing
  `main()` run against an oversized `ui.stacks.log` must leave that file
  untouched. Without it the rule lives only as a paragraph of prose and
  a call site, and moving the call above the guard breaks nothing that
  anything checks.

  The other half of that rule — that the *winner* arms at all — is **not
  covered here, and is not covered anywhere**. Reaching it means getting
  past `NSApplication.run()`, which needs a window server this suite does
  not have; deleting the call leaves every test green. Closing it means
  launching the real app and looking for `ui.stacks.log`, which is a
  deployment step rather than a test. The display agent's half of the
  same rule *is* covered, in `test_app.py`'s `StackDumpArmingTests`,
  because `main()` there can be driven past the guard with AppKit mocked.
  Stated plainly rather than implied, for the same reason the
  `_dump_file` limit below is.

    display/.venv/bin/python3 -m unittest display.test_diagnostics -v
"""

from __future__ import annotations

import fcntl
import io
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import IO
from unittest.mock import patch

from display import diagnostics, paths

REPO = Path(__file__).resolve().parent.parent


class _IsolatedHome(unittest.TestCase):
    """A temporary `$HOME` plus a guaranteed restore of `sys.stderr`.

    The restore is not optional: `redirect_stderr_to_log` replaces the
    interpreter's stderr for real, so a test that left it pointing into a
    `TemporaryDirectory` would take the *rest of the suite* down with it
    the moment that directory was cleaned up.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self._home_patch = patch("pathlib.Path.home", return_value=self.home)
        self._home_patch.start()
        self._real_stderr = sys.stderr

    def tearDown(self) -> None:
        # Both releases before the temporary directory goes away: each
        # holds an open handle inside it, and a handle collected
        # afterwards emits a ResourceWarning — onto whatever `sys.stderr`
        # is by then, which in these tests is a log file another
        # assertion is about to read.
        diagnostics.release_stderr()
        diagnostics.disarm_stack_dumps()
        sys.stderr = self._real_stderr
        self._home_patch.stop()
        self._tmp.cleanup()

    def log_dir(self) -> Path:
        return self.home / "Library" / "Logs" / "ImageView"


def _read_when_nonempty(path: Path, timeout_s: float = 2.0) -> str:
    """Read `path`, allowing for the signal handler that writes it having
    been entered but not yet returned. faulthandler writes from a real
    C-level handler, so in practice the bytes are there by the time
    `os.kill` returns; the retry is insurance, not an expectation."""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            text = path.read_text(errors="replace")
        except OSError:
            text = ""
        if text or time.monotonic() > deadline:
            return text
        time.sleep(0.01)


class ArmStackDumpsTests(_IsolatedHome):
    def test_it_returns_the_role_specific_path_in_the_log_directory(self) -> None:
        self.assertEqual(
            diagnostics.arm_stack_dumps(paths.UI_ROLE),
            self.log_dir() / "ui.stacks.log",
        )

    def test_a_usr1_dumps_this_thread_s_stack_into_that_file(self) -> None:
        """The one test that matters. It is also the test that fails if
        the module stops holding the file object alive."""
        path = diagnostics.arm_stack_dumps(paths.DISPLAY_ROLE)
        assert path is not None

        os.kill(os.getpid(), signal.SIGUSR1)

        dump = _read_when_nonempty(path)
        self.assertIn("Current thread", dump)
        # The frame this test is executing in must be named in it —
        # "the file is non-empty" would pass on a truncated write.
        self.assertIn("test_a_usr1_dumps_this_thread_s_stack_into_that_file", dump)

    def test_the_dump_goes_to_the_file_and_not_to_stderr(self) -> None:
        """A Finder-launched UI discards stderr, so a dump written there
        is a dump that does not exist."""
        captured = io.StringIO()
        sys.stderr = captured
        path = diagnostics.arm_stack_dumps(paths.DISPLAY_ROLE)
        assert path is not None

        os.kill(os.getpid(), signal.SIGUSR1)

        self.assertIn("Current thread", _read_when_nonempty(path))
        self.assertNotIn("Current thread", captured.getvalue())

    def test_the_module_holds_the_dump_file_open(self) -> None:
        """State, not behaviour, and deliberately so.

        `faulthandler`'s documented contract is that the file stays open
        until the handler is unregistered, and this module's job is to
        satisfy that rather than to rely on CPython 3.13 happening to
        hold a reference of its own (measured; see `diagnostics.py`).
        There is no behavioural test available for it on this
        interpreter, and a test that cannot fail is worse than one that
        says plainly what it is checking."""
        diagnostics.arm_stack_dumps(paths.UI_ROLE)
        self.assertIsNotNone(diagnostics._dump_file)
        self.assertFalse(diagnostics._dump_file.closed)

    def test_it_rotates_an_oversized_dump_file(self) -> None:
        """Armed only after the single-instance lock is won, so this
        process is the sole writer and rotating is safe here."""
        self.log_dir().mkdir(parents=True)
        stale = self.log_dir() / "ui.stacks.log"
        stale.write_text("x" * (11 * 1024 * 1024))

        diagnostics.arm_stack_dumps(paths.UI_ROLE)

        self.assertTrue((self.log_dir() / "ui.stacks.log.old").is_file())
        self.assertEqual(stale.read_text(), "")

    def test_it_returns_none_when_the_log_directory_cannot_be_created(self) -> None:
        """Never fatal: an unwritable home costs the dumps, not the app."""
        with patch("display.paths.ensure_dir", return_value=False):
            self.assertIsNone(diagnostics.arm_stack_dumps(paths.UI_ROLE))


class OpenLogTests(_IsolatedHome):
    """The shared opener's flags, exercised through a real caller.

    Neither of these is a privilege boundary on the machine this app
    ships to — everything runs as one uid and `~/Library/Logs/` is
    already 0700 — but these files are new, the correct defaults cost
    one line, and an untested default is one nobody notices being
    changed."""

    def test_the_log_is_created_private_to_the_user(self) -> None:
        path = diagnostics.arm_stack_dumps(paths.UI_ROLE)
        assert path is not None
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_it_will_not_open_through_a_symlink(self) -> None:
        """`O_NOFOLLOW`. A log path that has been replaced by a symlink
        is not a path this module should write through — it declines and
        reports the failure, rather than appending to whatever is on the
        other end."""
        elsewhere = self.home / "somewhere-else.log"
        elsewhere.write_text("")
        self.log_dir().mkdir(parents=True)
        (self.log_dir() / "ui.stacks.log").symlink_to(elsewhere)

        captured = io.StringIO()
        sys.stderr = captured

        self.assertIsNone(diagnostics.arm_stack_dumps(paths.UI_ROLE))
        self.assertEqual(elsewhere.read_text(), "")
        self.assertIn("cannot open", captured.getvalue())


class IsRegularFileTests(_IsolatedHome):
    def test_a_real_file_is_a_regular_file(self) -> None:
        target = self.home / "stderr.log"
        with open(target, "w") as handle:
            self.assertTrue(diagnostics._is_regular_file(handle))

    def test_a_pipe_is_not(self) -> None:
        read_fd, write_fd = os.pipe()
        try:
            with os.fdopen(write_fd, "w") as handle:
                self.assertFalse(diagnostics._is_regular_file(handle))
        finally:
            os.close(read_fd)

    def test_dev_null_is_not(self) -> None:
        """A character device, which is what a Finder launch is likely to
        hand the process — and the case the redirect exists for."""
        with open(os.devnull, "w") as handle:
            self.assertFalse(diagnostics._is_regular_file(handle))

    def test_a_stream_with_no_descriptor_is_not(self) -> None:
        self.assertFalse(diagnostics._is_regular_file(io.StringIO()))

    def test_a_missing_stderr_is_not(self) -> None:
        """`sys.stderr` really can be None in a GUI-launched process."""
        self.assertFalse(diagnostics._is_regular_file(None))


class RedirectStderrTests(_IsolatedHome):
    def setUp(self) -> None:
        super().setUp()
        self._pipe: tuple[int, IO[str]] | None = None

    def tearDown(self) -> None:
        super().tearDown()
        if self._pipe is not None:
            read_fd, handle = self._pipe
            handle.close()
            os.close(read_fd)

    def _stderr_is_a_pipe(self) -> None:
        """Point `sys.stderr` at a pipe — not a regular file, which is
        what a Finder launch looks like from in here."""
        read_fd, write_fd = os.pipe()
        self._pipe = (read_fd, os.fdopen(write_fd, "w"))
        sys.stderr = self._pipe[1]

    def test_it_takes_stderr_when_nothing_else_has(self) -> None:
        self._stderr_is_a_pipe()
        path = diagnostics.redirect_stderr_to_log(paths.UI_ROLE)
        self.assertEqual(path, self.log_dir() / "ui.stderr.log")
        print("hello from the menu bar", file=sys.stderr)
        self.assertIn("hello from the menu bar", path.read_text())

    def test_a_line_reaches_the_file_with_no_explicit_flush(self) -> None:
        """Line buffering, asserted as behaviour. A wedged process never
        reaches a flush or an exit, so a block-buffered handle would hold
        back exactly the lines that say what it was doing."""
        self._stderr_is_a_pipe()
        path = diagnostics.redirect_stderr_to_log(paths.UI_ROLE)
        assert path is not None
        sys.stderr.write("half a line")
        self.assertNotIn("half a line", path.read_text())
        sys.stderr.write(" and the newline\n")
        self.assertIn("half a line and the newline\n", path.read_text())

    def test_it_leaves_a_regular_file_stderr_completely_alone(self) -> None:
        """The launchd case. `StandardErrorPath` is already this file and
        launchd is the writer; a second file description on it would
        break the one-writer-per-file contract."""
        existing = self.home / "launchd-opened.log"
        with open(existing, "w") as handle:
            sys.stderr = handle
            self.assertIsNone(diagnostics.redirect_stderr_to_log(paths.UI_ROLE))
            self.assertIs(sys.stderr, handle)

        self.assertFalse((self.log_dir() / "ui.stderr.log").exists())

    def test_it_appends_and_does_not_rotate(self) -> None:
        """The deliberate omission, asserted so it cannot be "tidied up"
        into a rotation later. The process that calls this is the one
        about to lose the single-instance race, and a rename issued by
        the loser moves the winner's log out from under its descriptor."""
        self.log_dir().mkdir(parents=True)
        existing = self.log_dir() / "ui.stderr.log"
        existing.write_text("y" * (11 * 1024 * 1024))

        self._stderr_is_a_pipe()
        path = diagnostics.redirect_stderr_to_log(paths.UI_ROLE)
        assert path is not None
        print("appended", file=sys.stderr)

        self.assertFalse((self.log_dir() / "ui.stderr.log.old").exists())
        self.assertTrue(path.read_text().endswith("appended\n"))
        self.assertGreater(path.stat().st_size, 11 * 1024 * 1024)

    def test_it_returns_none_when_the_log_directory_cannot_be_created(self) -> None:
        self._stderr_is_a_pipe()
        original = sys.stderr
        with patch("display.paths.ensure_dir", return_value=False):
            self.assertIsNone(diagnostics.redirect_stderr_to_log(paths.UI_ROLE))
        self.assertIs(sys.stderr, original)


class NoteTests(_IsolatedHome):
    def test_it_writes_one_timestamped_line(self) -> None:
        captured = io.StringIO()
        sys.stderr = captured
        diagnostics.note("something happened")
        line = captured.getvalue()
        self.assertTrue(line.endswith("something happened\n"))
        # `[YYYY-MM-DD HH:MM:SS] ` — the logs are append-only across every
        # launch, so an undated line cannot be attributed to one.
        self.assertRegex(line, r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ")

    def test_it_does_not_raise_when_stderr_is_unusable(self) -> None:
        """The contention caller must be able to `return 0` no matter
        what. An exception there becomes a non-zero exit, which
        `KeepAlive {SuccessfulExit: false}` *does* respawn."""
        closed = open(self.home / "closed.log", "w")
        closed.close()
        sys.stderr = closed
        diagnostics.note("this cannot be written anywhere")


class MenubarContentionTests(unittest.TestCase):
    """`menubar.main()` for real, in a subprocess, with the lock held.

    It is the only honest way to test the launchd branch: "was fd 2
    already a regular file" is decided by whoever started the process.

    🔴 **These launch as the LaunchAgent, and that is load-bearing now.**
    This docstring used to say "the contention path returns before any
    AppKit object is built, so this needs no window server". **Phase 4b
    made that false.** A losing *human* launch now builds an `NSPanel`,
    shows it, and runs a run loop until the window is dismissed — which
    is the whole point of the phase, and which no `subprocess.run` can
    ever dismiss. Left as it was, each of these tests put a real panel on
    the developer's screen and sat there until its 120s timeout.

    `XPC_SERVICE_NAME` set to the UI agent's own label is the measured
    discriminator for "started by the LaunchAgent" — see
    `contention_state.launched_by_agent` — and an agent respawn
    deliberately shows nothing and signals nothing. Everything these
    tests assert (exit 0, the reason reaching the log, the stack file
    left alone, one writer per stderr) happens before that branch and is
    unchanged by it. The human branch's exit code is covered in
    `ui/test_menubar.py`, where the window is not real.
    """

    MAIN = "from ui import menubar; raise SystemExit(menubar.main())"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        lock = self.home / ".viewlab" / "ui.lock"
        lock.parent.mkdir(parents=True)
        self.holder = open(lock, "a+")
        fcntl.flock(self.holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.holder.write("4242\n")
        self.holder.flush()
        self.ui_log = self.home / "Library" / "Logs" / "ImageView" / "ui.stderr.log"

    def tearDown(self) -> None:
        self.holder.close()
        self._tmp.cleanup()

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["HOME"] = str(self.home)
        env["PYTHONPATH"] = str(REPO)
        # See the class docstring: without this the child shows a window
        # and blocks until its timeout.
        env["XPC_SERVICE_NAME"] = paths.UI_AGENT_LABEL
        return env

    def test_a_losing_menu_bar_records_why_and_still_exits_0(self) -> None:
        """Exit 0 is load-bearing and must not change; what is new is
        that the reason outlives the process."""
        result = subprocess.run(
            [sys.executable, "-c", self.MAIN],
            cwd=REPO,
            env=self._env(),
            capture_output=True,  # a pipe, so not a regular file
            text=True,
            timeout=120,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.ui_log.is_file(), "the UI wrote no log at all")
        logged = self.ui_log.read_text()
        self.assertIn("exiting 0 deliberately", logged)
        self.assertIn("pid 4242", logged)
        self.assertIn(str(self.home / ".viewlab" / "ui.lock"), logged)

    def test_a_losing_menu_bar_does_not_rotate_the_holder_s_stacks_log(self) -> None:
        """The ordering rule, as behaviour rather than as a comment.

        Rotation copies aside and then truncates in place. A losing
        instance that rotated `ui.stacks.log` would zero the *holder's*
        dump file under the descriptor `faulthandler` is still pointed
        at — so every dump the holder had already written, from the
        process anyone is actually trying to diagnose, would be gone from
        the file every instruction names. (Before the copy-truncate fix
        the same mistake *renamed* that file instead, which broke the
        descriptor outright; the ordering rule predates the fix and
        outlives it.) Arming therefore happens after the guard, and this
        is what says so. The size assertion below is the load-bearing
        one now: `.old` not existing and the size being unchanged were
        one fact under rename and are two under copy-truncate."""
        stacks = self.home / "Library" / "Logs" / "ImageView" / "ui.stacks.log"
        stacks.parent.mkdir(parents=True)
        # Comfortably over log_rotation.MAX_LOG_BYTES, so an
        # unconditional rotate cannot pass by being under the threshold.
        stacks.write_bytes(b"z" * (11 * 1024 * 1024))
        before = stacks.stat().st_size

        result = subprocess.run(
            [sys.executable, "-c", self.MAIN],
            cwd=REPO,
            env=self._env(),
            capture_output=True,
            text=True,
            timeout=120,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(
            stacks.with_name("ui.stacks.log.old").exists(),
            "a losing instance rotated the holder's stack-dump file",
        )
        self.assertEqual(stacks.stat().st_size, before)

    def test_it_writes_to_launchds_file_and_creates_no_second_one(self) -> None:
        """The one-writer rule, end to end: given a real file on fd 2 the
        app must add nothing of its own."""
        launchd_log = self.home / "launchd-opened.log"
        with open(launchd_log, "w") as handle:
            result = subprocess.run(
                [sys.executable, "-c", self.MAIN],
                cwd=REPO,
                env=self._env(),
                stdout=subprocess.DEVNULL,
                stderr=handle,
                timeout=120,
            )

        self.assertEqual(result.returncode, 0)
        self.assertIn("exiting 0 deliberately", launchd_log.read_text())
        self.assertFalse(
            self.ui_log.exists(),
            "the app opened its own log while launchd already owned stderr",
        )


if __name__ == "__main__":
    unittest.main()
