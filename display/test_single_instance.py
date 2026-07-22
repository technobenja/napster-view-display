"""Unit tests for single_instance.py.

`flock` locks are held by the *open file description*, not by the
process, so two separate `open()` calls contend with each other even
inside one process. That is what makes the contention case testable here
without spawning a subprocess — and one subprocess test is included
anyway, because the cross-process case is the one that actually matters
and it is cheap to prove for real.

    ./.venv/bin/python3 -m unittest test_single_instance -v
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from display import single_instance


class AcquireTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.lock = self.tmpdir / "display.lock"

    def tearDown(self) -> None:
        single_instance.release()
        self._tmp.cleanup()

    def test_first_acquire_succeeds(self) -> None:
        self.assertIsNotNone(single_instance.acquire(self.lock))

    def test_lock_file_is_created(self) -> None:
        single_instance.acquire(self.lock)
        self.assertTrue(self.lock.is_file())

    def test_parent_directory_is_created_on_demand(self) -> None:
        nested = self.tmpdir / "a" / "b" / "display.lock"
        self.assertIsNotNone(single_instance.acquire(nested))
        self.assertTrue(nested.is_file())

    def test_holder_pid_is_recorded(self) -> None:
        single_instance.acquire(self.lock)
        self.assertEqual(single_instance.read_holder_pid(self.lock), os.getpid())

    def test_second_acquire_returns_none(self) -> None:
        """None is the signal main() turns into a clean exit(0)."""
        self.assertIsNotNone(single_instance.acquire(self.lock))
        buf = io.StringIO()
        with redirect_stderr(buf):
            self.assertIsNone(single_instance.acquire(self.lock))

    def test_contention_message_names_the_holder_pid(self) -> None:
        single_instance.acquire(self.lock)
        buf = io.StringIO()
        with redirect_stderr(buf):
            single_instance.acquire(self.lock)
        self.assertIn(f"pid {os.getpid()}", buf.getvalue())

    def test_contention_message_explains_the_clean_exit(self) -> None:
        """The clean exit is load-bearing (KeepAlive SuccessfulExit:
        false); the log has to say so or the next reader will "fix" it."""
        single_instance.acquire(self.lock)
        buf = io.StringIO()
        with redirect_stderr(buf):
            single_instance.acquire(self.lock)
        self.assertIn("Exiting cleanly", buf.getvalue())

    def test_stale_lock_file_from_a_dead_process_is_reacquirable(self) -> None:
        """flock is released by the kernel when the holder dies, so a
        leftover file with a stale pid in it must not block startup."""
        self.lock.write_text("999999\n")
        self.assertIsNotNone(single_instance.acquire(self.lock))

    def test_release_allows_reacquisition(self) -> None:
        single_instance.acquire(self.lock)
        single_instance.release()
        self.assertIsNotNone(single_instance.acquire(self.lock))

    def test_unopenable_lock_path_starts_without_the_guard(self) -> None:
        """Refusing to run because a *lock file* could not be created
        would turn a cosmetic problem into a total outage. Must not
        return None — None means "another instance is running"."""
        blocker = self.tmpdir / "blocker"
        blocker.write_text("i am a file")
        buf = io.StringIO()
        with redirect_stderr(buf):
            handle = single_instance.acquire(blocker / "display.lock")
        self.assertIsNotNone(handle)
        self.assertIn("WITHOUT the single-instance guard", buf.getvalue())

    def test_holder_pid_of_a_missing_file_is_none(self) -> None:
        self.assertIsNone(single_instance.read_holder_pid(self.tmpdir / "nope"))

    def test_holder_pid_of_a_garbage_file_is_none(self) -> None:
        self.lock.write_text("not a pid")
        self.assertIsNone(single_instance.read_holder_pid(self.lock))


class CrossProcessTests(unittest.TestCase):
    """The case that actually matters: two display processes, one View."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.lock = Path(self._tmp.name) / "display.lock"

    def tearDown(self) -> None:
        single_instance.release()
        self._tmp.cleanup()

    def test_a_second_process_cannot_take_a_held_lock(self) -> None:
        single_instance.acquire(self.lock)
        script = textwrap.dedent(
            f"""
            import sys
            sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})
            from display import single_instance
            from pathlib import Path
            got = single_instance.acquire(Path({str(self.lock)!r}))
            print("ACQUIRED" if got is not None else "BLOCKED")
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
        )
        self.assertIn("BLOCKED", result.stdout)
        self.assertIn(f"pid {os.getpid()}", result.stderr)

    def test_the_lock_is_released_when_the_holding_process_exits(self) -> None:
        script = textwrap.dedent(
            f"""
            import sys
            sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})
            from display import single_instance
            from pathlib import Path
            single_instance.acquire(Path({str(self.lock)!r}))
            """
        )
        subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
        self.assertIsNotNone(single_instance.acquire(self.lock))


if __name__ == "__main__":
    unittest.main()
