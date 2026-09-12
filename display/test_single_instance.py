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
        self.assertTrue(single_instance.acquire(self.lock).acquired)

    def test_acquire_never_returns_none(self) -> None:
        """🔴 The migration hazard, pinned. `acquire()` used to return
        None on contention, and every call site was `if ... is None`. That
        idiom is now silently False, which would let a second instance walk
        straight past the guard — the exact failure the guard exists to
        prevent. A future edit that reintroduces a None return makes this
        fail rather than making the guard stop guarding."""
        self.assertIsNotNone(single_instance.acquire(self.lock))
        buf = io.StringIO()
        with redirect_stderr(buf):
            self.assertIsNotNone(single_instance.acquire(self.lock))
        blocker = self.tmpdir / "blocker2"
        blocker.write_text("i am a file")
        with redirect_stderr(buf):
            self.assertIsNotNone(single_instance.acquire(blocker / "display.lock"))

    def test_lock_file_is_created(self) -> None:
        single_instance.acquire(self.lock)
        self.assertTrue(self.lock.is_file())

    def test_parent_directory_is_created_on_demand(self) -> None:
        nested = self.tmpdir / "a" / "b" / "display.lock"
        self.assertTrue(single_instance.acquire(nested).acquired)
        self.assertTrue(nested.is_file())

    def test_holder_pid_is_recorded(self) -> None:
        single_instance.acquire(self.lock)
        self.assertEqual(single_instance.read_holder_pid(self.lock), os.getpid())

    def test_second_acquire_is_contended(self) -> None:
        """CONTENDED is the signal main() turns into a clean exit(0)."""
        self.assertTrue(single_instance.acquire(self.lock).acquired)
        buf = io.StringIO()
        with redirect_stderr(buf):
            second = single_instance.acquire(self.lock)
        self.assertTrue(second.contended)
        self.assertFalse(second.acquired)
        self.assertFalse(second.unguarded)
        self.assertIsNone(second.handle)

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
        self.assertTrue(single_instance.acquire(self.lock).acquired)

    def test_release_allows_reacquisition(self) -> None:
        single_instance.acquire(self.lock)
        single_instance.release()
        self.assertTrue(single_instance.acquire(self.lock).acquired)

    def test_unopenable_lock_path_starts_without_the_guard(self) -> None:
        """Refusing to run because a *lock file* could not be created
        would turn a cosmetic problem into a total outage. Behaviour
        unchanged from when this returned a bare handle; what is new is
        that the caller can now *tell*, which is what lets `main()` say so
        and lets `arm_stack_dumps` decline to rotate a log it does not
        own."""
        blocker = self.tmpdir / "blocker"
        blocker.write_text("i am a file")
        buf = io.StringIO()
        with redirect_stderr(buf):
            result = single_instance.acquire(blocker / "display.lock")
        self.assertTrue(result.unguarded)
        self.assertFalse(result.contended)
        self.assertIsNotNone(result.handle)
        self.assertIn("WITHOUT the single-instance guard", buf.getvalue())

    def test_an_unguarded_result_is_not_mistakable_for_an_acquired_one(self) -> None:
        """The whole point of the third outcome. These two used to be
        the same value."""
        blocker = self.tmpdir / "blocker3"
        blocker.write_text("i am a file")
        buf = io.StringIO()
        with redirect_stderr(buf):
            unguarded = single_instance.acquire(blocker / "display.lock")
        single_instance.release()
        acquired = single_instance.acquire(self.lock)
        self.assertNotEqual(unguarded.outcome, acquired.outcome)
        self.assertTrue(acquired.acquired)
        self.assertFalse(unguarded.acquired)

    def test_acquisition_has_no_truth_value_to_get_wrong(self) -> None:
        """Deliberately no `__bool__`: a three-state result with an
        implicit truth value is the ambiguity this type removes. The
        default dataclass has no `__bool__`, so `bool()` is always True —
        which is exactly why callers must ask the question they mean."""
        contended = single_instance.Acquisition(single_instance.Outcome.CONTENDED)
        self.assertNotIn("__bool__", vars(single_instance.Acquisition))
        self.assertTrue(bool(contended))
        self.assertTrue(contended.contended)

    def test_holder_pid_of_a_missing_file_is_none(self) -> None:
        self.assertIsNone(single_instance.read_holder_pid(self.tmpdir / "nope"))

    def test_holder_pid_of_a_garbage_file_is_none(self) -> None:
        self.lock.write_text("not a pid")
        self.assertIsNone(single_instance.read_holder_pid(self.lock))

    def test_holder_pid_of_an_undecodable_file_is_none(self) -> None:
        """🔴 This function's docstring says "never raises", and it did:
        `UnicodeDecodeError` is a **`ValueError`, not an `OSError`**, so a
        lock file with one bad byte went straight through the handler
        written to make it total. A half-written file interrupted by a
        crash is exactly how that happens — and Phase 4a put a classifier,
        a window-server query and a signal downstream of this read."""
        for label, payload in (
            ("a lone continuation byte", b"\x80"),
            ("a truncated multi-byte sequence", b"\xff\xfe123"),
            ("a NUL", b"\x00\x00"),
        ):
            with self.subTest(label):
                self.lock.write_bytes(payload)
                self.assertIsNone(single_instance.read_holder_pid(self.lock))

    def test_holder_pid_survives_a_decodable_pid_with_trailing_junk(self) -> None:
        """The replacement characters must not accidentally turn into a
        readable number either."""
        self.lock.write_bytes(b"1234\xff\n")
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
            print("BLOCKED" if got.contended else "ACQUIRED")
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
        self.assertTrue(single_instance.acquire(self.lock).acquired)


if __name__ == "__main__":
    unittest.main()
