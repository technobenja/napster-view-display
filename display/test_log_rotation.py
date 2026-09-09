"""Unit tests for log_rotation.py — same conventions as test_settings.py:
stdlib unittest, tempfile.TemporaryDirectory(), no pytest, no new
dependency.
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
from unittest import mock

from display import log_rotation
from display.log_rotation import rotate_if_oversized

SMALL_CAP = 100  # bytes — small enough to exercise both sides cheaply
REPO = Path(__file__).resolve().parents[1]


class RotateIfOversizedTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.log_path = self.tmpdir / "display.stdout.log"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_missing_file_does_not_raise(self) -> None:
        try:
            rotate_if_oversized(self.log_path, max_bytes=SMALL_CAP)
        except Exception as exc:  # noqa: BLE001 - this is the assertion
            self.fail(f"rotate_if_oversized raised {exc!r} for a missing file")
        self.assertFalse(self.log_path.exists())

    def test_empty_file_does_not_raise_and_is_left_alone(self) -> None:
        self.log_path.write_text("")
        rotate_if_oversized(self.log_path, max_bytes=SMALL_CAP)
        self.assertTrue(self.log_path.exists())
        self.assertEqual(self.log_path.read_text(), "")
        self.assertFalse((self.tmpdir / "display.stdout.log.old").exists())

    def test_file_under_cap_is_left_alone(self) -> None:
        content = "x" * (SMALL_CAP - 1)
        self.log_path.write_text(content)
        rotate_if_oversized(self.log_path, max_bytes=SMALL_CAP)
        self.assertEqual(self.log_path.read_text(), content)
        self.assertFalse((self.tmpdir / "display.stdout.log.old").exists())

    def test_file_at_exact_cap_is_left_alone(self) -> None:
        """max_bytes is a threshold to exceed, not meet — a file exactly
        at the cap hasn't overflowed it."""
        content = "x" * SMALL_CAP
        self.log_path.write_text(content)
        rotate_if_oversized(self.log_path, max_bytes=SMALL_CAP)
        self.assertEqual(self.log_path.read_text(), content)
        self.assertFalse((self.tmpdir / "display.stdout.log.old").exists())

    def test_file_over_cap_is_rotated(self) -> None:
        content = "x" * (SMALL_CAP + 1)
        self.log_path.write_text(content)
        rotate_if_oversized(self.log_path, max_bytes=SMALL_CAP)

        old_path = self.tmpdir / "display.stdout.log.old"
        self.assertTrue(old_path.exists())
        self.assertEqual(old_path.read_text(), content)
        # The original file SURVIVES, emptied in place. It is not removed
        # and not replaced: see the held-descriptor test below for why
        # that distinction is the entire point of this module.
        self.assertTrue(self.log_path.exists())
        self.assertEqual(self.log_path.read_text(), "")

    def test_rotation_leaves_no_temp_files_behind(self) -> None:
        self.log_path.write_text("x" * (SMALL_CAP + 1))
        rotate_if_oversized(self.log_path, max_bytes=SMALL_CAP)

        self.assertEqual(
            sorted(p.name for p in self.tmpdir.iterdir()),
            ["display.stdout.log", "display.stdout.log.old"],
        )

    def test_the_temp_file_is_created_beside_its_destination(self) -> None:
        """`os.replace()` is atomic only within one filesystem.

        The whole reason `_copy_to_old` writes a temp file instead of
        copying straight onto `.old` is that the final move is atomic, so
        a failed copy can never leave `.old` as a truncated prefix. That
        holds only while the temp file is on `.old`'s own volume — a temp
        in `TMPDIR` would make the rename a cross-device copy and give
        the guarantee away.

        Nothing about the resulting files can distinguish the two: under
        test the tmpdir and `TMPDIR` are the same volume, so dropping
        `dir=` passes every other assertion in this file. Spy on the
        kwarg, because that is the only place the premise is visible.
        """
        seen: dict[str, object] = {}
        real_mkstemp = log_rotation.tempfile.mkstemp

        def spy(*args: object, **kwargs: object) -> object:
            seen.update(kwargs)
            return real_mkstemp(*args, **kwargs)  # type: ignore[arg-type]

        self.log_path.write_text("x" * (SMALL_CAP + 1))
        with mock.patch.object(log_rotation.tempfile, "mkstemp", spy):
            rotate_if_oversized(self.log_path, max_bytes=SMALL_CAP)

        self.assertEqual(
            seen.get("dir"),
            self.tmpdir,
            "the temp file is not on the same filesystem as its "
            "destination, so os.replace() is no longer atomic",
        )
        self.assertTrue((self.tmpdir / "display.stdout.log.old").exists())

    def test_rotation_preserves_the_old_file_s_permission_bits(self) -> None:
        """`.old` must not be tightened past the log it came from.

        `mkstemp` creates 0600; launchd's own log files land 0644. Copying
        through a temp file would silently change who can read the
        retained generation if the mode were not carried across.
        """
        self.log_path.write_text("x" * (SMALL_CAP + 1))
        os.chmod(self.log_path, 0o644)
        rotate_if_oversized(self.log_path, max_bytes=SMALL_CAP)

        old_path = self.tmpdir / "display.stdout.log.old"
        self.assertEqual(old_path.stat().st_mode & 0o777, 0o644)

    def test_rotation_overwrites_previous_old_generation(self) -> None:
        old_path = self.tmpdir / "display.stdout.log.old"
        old_path.write_text("stale generation from a prior rotation")

        content = "y" * (SMALL_CAP + 1)
        self.log_path.write_text(content)
        rotate_if_oversized(self.log_path, max_bytes=SMALL_CAP)

        self.assertEqual(old_path.read_text(), content)

    def test_default_max_bytes_is_ten_megabytes(self) -> None:
        from display import log_rotation

        self.assertEqual(log_rotation.MAX_LOG_BYTES, 10 * 1024 * 1024)


class HeldDescriptorTests(unittest.TestCase):
    """The bug this module was rewritten for, as behaviour.

    launchd opens `StandardOutPath`/`StandardErrorPath` and dups them onto
    fd 1 / fd 2 **before** exec'ing the process, so by the time
    `rotate_if_oversized()` runs, a descriptor on the log already exists
    and this process cannot close it. A rotation that *renames* moves the
    inode out from under that descriptor: every subsequent write lands in
    `.old` and the path every doc names stays empty, for the whole life of
    the process. Measured on a live install before v1.1.3 —
    `display.stderr.log`
    at 12,181,049 bytes against a 10 MB cap with no `.old`, because the
    file was being moved aside by hand at deploy time.

    These tests open the file with `O_APPEND` because that is what
    launchd was **measured** doing (macOS 15 / Darwin 24.6.0:
    `fcntl(F_GETFL)` returned `O_RDWR | O_APPEND` on both inherited
    descriptors, and a write after an external truncate landed at offset
    0).

    **None of them can observe launchd, and an earlier version of this
    docstring claimed one of them could.** A unit test opens its own
    descriptors, so it tests its own `open()` call; if a future macOS
    stopped setting `O_APPEND`, every test here would go on passing
    unchanged. `test_a_non_append_descriptor_would_leave_a_hole` is a
    **negative control**, not a canary: it proves the `assertNotIn(b"\\x00")`
    next to it is capable of failing, which is worth having and is all it
    does. The platform fact is guarded by two things, neither of them
    automated by this file — re-measurement by a human (the method is in
    `log_rotation.py`'s module docstring) and the runtime precondition
    `_inherited_fd_appends()`, which checks the real inherited
    descriptors on every rotation and skips rather than risk the hole.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.log_path = self.tmpdir / "display.stderr.log"
        self.old_path = self.tmpdir / "display.stderr.log.old"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_writer_holding_the_fd_keeps_writing_to_the_live_path(self) -> None:
        """Fails against the rename implementation, which is the point."""
        content = "x" * (SMALL_CAP + 1)
        self.log_path.write_text(content)

        # Stand in for launchd: the descriptor exists before rotation and
        # is never reopened afterwards.
        fd = os.open(self.log_path, os.O_WRONLY | os.O_APPEND)
        try:
            before = os.fstat(fd)
            rotate_if_oversized(self.log_path, max_bytes=SMALL_CAP)
            os.write(fd, b"line written after rotation\n")
            after_fd = os.fstat(fd)
        finally:
            os.close(fd)

        # Under the old rename implementation the path does not exist at
        # all here. Assert that first: `read_text()` would raise
        # FileNotFoundError before its own assertion message could print.
        self.assertTrue(
            self.log_path.exists(),
            "rotation removed the live log path instead of truncating it "
            "in place",
        )
        self.assertEqual(
            self.log_path.read_text(),
            "line written after rotation\n",
            "the held descriptor is no longer writing to the live log path",
        )
        # ... and the descriptor is still the same file, not an orphan.
        after_path = self.log_path.stat()
        self.assertEqual(
            (before.st_dev, before.st_ino),
            (after_path.st_dev, after_path.st_ino),
            "rotation replaced the inode instead of truncating it",
        )
        self.assertEqual((after_fd.st_dev, after_fd.st_ino),
                         (after_path.st_dev, after_path.st_ino))
        # The prior generation is still retained, complete.
        self.assertEqual(self.old_path.read_text(), content)

    def test_the_live_log_has_no_nul_hole_after_rotation(self) -> None:
        """The failure mode if launchd ever stops using O_APPEND.

        A truncate leaves an inherited descriptor's offset untouched. With
        `O_APPEND` the kernel repositions to EOF on every write, so the
        next byte lands at 0. Without it the write would land at the old
        offset and the kernel would fill the gap with NULs — a log whose
        first megabytes are zeroes, which is strictly worse than the bug
        this replaced. Assert the absence of the hole, not the presence of
        the flag, so this stays true however the platform gets there.
        """
        self.log_path.write_text("x" * (SMALL_CAP + 1))

        fd = os.open(self.log_path, os.O_WRONLY | os.O_APPEND)
        try:
            rotate_if_oversized(self.log_path, max_bytes=SMALL_CAP)
            os.write(fd, b"first line of the new generation\n")
        finally:
            os.close(fd)

        data = self.log_path.read_bytes()
        self.assertNotIn(b"\x00", data, "rotation left a sparse NUL hole")
        self.assertTrue(data.startswith(b"first line"))

    def test_a_non_append_descriptor_would_leave_a_hole(self) -> None:
        """A negative control for the test above, and nothing more.

        Without `O_APPEND` the same sequence does produce the NUL hole,
        which proves the previous test's `assertNotIn` is discriminating
        rather than vacuous. It does **not** detect a platform change:
        both tests open their own descriptors, so both would keep passing
        if launchd stopped setting the flag. See the class docstring.
        """
        self.log_path.write_text("x" * (SMALL_CAP + 1))

        fd = os.open(self.log_path, os.O_WRONLY)  # deliberately no O_APPEND
        try:
            os.lseek(fd, 0, os.SEEK_END)
            rotate_if_oversized(self.log_path, max_bytes=SMALL_CAP)
            os.write(fd, b"first line of the new generation\n")
        finally:
            os.close(fd)

        self.assertIn(b"\x00", self.log_path.read_bytes())


class DegradationTests(unittest.TestCase):
    """`rotate_if_oversized` must never raise, and must never truncate a
    log it failed to copy."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.log_path = self.tmpdir / "display.stdout.log"
        self.old_path = self.tmpdir / "display.stdout.log.old"
        self.content = "x" * (SMALL_CAP + 1)
        self.log_path.write_text(self.content)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _rotate_capturing_stderr(self) -> str:
        buf = io.StringIO()
        with redirect_stderr(buf):
            rotate_if_oversized(self.log_path, max_bytes=SMALL_CAP)
        return buf.getvalue()

    def test_a_failed_copy_leaves_the_oversized_log_completely_intact(self) -> None:
        """Full disk. Over-cap and readable beats within-cap and empty."""
        self.old_path.write_text("previous generation")

        with mock.patch.object(
            log_rotation.shutil, "copyfileobj", side_effect=OSError(28, "No space left")
        ):
            reported = self._rotate_capturing_stderr()

        self.assertEqual(self.log_path.read_text(), self.content)
        self.assertEqual(self.old_path.read_text(), "previous generation")
        self.assertIn("failed to copy", reported)
        self.assertEqual(
            sorted(p.name for p in self.tmpdir.iterdir()),
            ["display.stdout.log", "display.stdout.log.old"],
            "a temp file was left behind by the failed copy",
        )

    def test_a_failed_temp_file_creation_leaves_the_log_intact(self) -> None:
        with mock.patch.object(
            log_rotation.tempfile, "mkstemp", side_effect=OSError(13, "Permission denied")
        ):
            reported = self._rotate_capturing_stderr()

        self.assertEqual(self.log_path.read_text(), self.content)
        self.assertFalse(self.old_path.exists())
        self.assertIn("cannot create a temp file", reported)

    def test_a_failed_truncate_keeps_the_copy_and_does_not_raise(self) -> None:
        with mock.patch.object(
            log_rotation.os, "ftruncate", side_effect=OSError(30, "Read-only file system")
        ):
            reported = self._rotate_capturing_stderr()

        # Nothing lost: the pre-existing state plus a fresh .old.
        self.assertEqual(self.log_path.read_text(), self.content)
        self.assertEqual(self.old_path.read_text(), self.content)
        self.assertIn("could not truncate", reported)

    def test_a_failed_stat_does_not_raise(self) -> None:
        with mock.patch.object(
            Path, "stat", side_effect=OSError(5, "Input/output error")
        ):
            reported = self._rotate_capturing_stderr()
        self.assertIn("cannot stat", reported)


class InheritedDescriptorPreconditionTests(unittest.TestCase):
    """`_inherited_fd_appends()` — the runtime half of the O_APPEND claim.

    This is the only check in the project that can notice launchd's
    behaviour changing, so it is exercised the only way it can be
    honestly exercised: in a **child process**, with a real descriptor
    dup'd onto fd 1 before rotation runs, exactly as launchd arranges it
    before exec. Doing it in-process would mean dup2'ing over the test
    runner's own stdout.
    """

    CHILD = textwrap.dedent(
        """
        import os, sys
        sys.path.insert(0, sys.argv[1])
        from pathlib import Path
        from display.log_rotation import rotate_if_oversized

        log = Path(sys.argv[2])
        flags = os.O_WRONLY | (os.O_APPEND if sys.argv[3] == "append" else 0)
        fd = os.open(log, flags)
        os.dup2(fd, 1)          # stand in for launchd's pre-exec dup
        os.close(fd)
        rotate_if_oversized(log, max_bytes=100)
        """
    )

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.log_path = self.tmpdir / "display.stdout.log"
        self.old_path = self.tmpdir / "display.stdout.log.old"
        self.content = "x" * (SMALL_CAP + 1)
        self.log_path.write_text(self.content)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, mode: str) -> "subprocess.CompletedProcess[str]":
        return subprocess.run(
            [sys.executable, "-c", self.CHILD, str(REPO), str(self.log_path), mode],
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_a_non_appending_inherited_fd_skips_rotation_entirely(self) -> None:
        """Fail safe: an oversized-but-intact log beats a NUL hole."""
        result = self._run("noappend")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.log_path.read_text(), self.content)
        self.assertFalse(
            self.old_path.exists(),
            "rotation copied the log aside even though it refused to truncate",
        )
        self.assertIn("NOT open in append mode", result.stderr)

    def test_an_appending_inherited_fd_rotates_normally(self) -> None:
        """The precondition must gate, not simply block.

        Without this, an `_inherited_fd_appends` that always returned
        False would pass the test above and quietly disable rotation for
        the one configuration the module exists to serve.
        """
        result = self._run("append")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.log_path.read_text(), "")
        self.assertEqual(self.old_path.read_text(), self.content)
        self.assertNotIn("append mode", result.stderr)

    def test_it_ignores_descriptors_pointed_at_other_files(self) -> None:
        """Only fd 1/fd 2 *on this very file* are our business.

        Matched by (st_dev, st_ino), never by name — the same test
        `diagnostics._is_regular_file()` makes.
        """
        unrelated = self.tmpdir / "something-else.log"
        unrelated.write_text("not the log being rotated")
        info = self.log_path.stat()
        with open(unrelated, "a") as handle:
            other = os.fstat(handle.fileno())
            with mock.patch.object(log_rotation.os, "fstat", return_value=other):
                self.assertTrue(log_rotation._inherited_fd_appends(info))


class ReportingTests(unittest.TestCase):
    """The "never raises" contract, on the failure that actually reaches it.

    Under launchd `sys.stderr` IS the log file, so on a full disk the
    report of a failed copy is itself a write to a full disk.
    """

    class _FullDisk(io.StringIO):
        def write(self, *args: object, **kwargs: object) -> int:
            raise OSError(28, "No space left on device")

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.log_path = self.tmpdir / "display.stdout.log"
        self.content = "x" * (SMALL_CAP + 1)
        self.log_path.write_text(self.content)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_report_does_not_raise_when_stderr_itself_fails(self) -> None:
        with mock.patch.object(log_rotation.sys, "stderr", self._FullDisk()):
            log_rotation._report("log_rotation: something went wrong")

    def test_report_does_not_raise_on_a_closed_stderr(self) -> None:
        closed = io.StringIO()
        closed.close()
        with mock.patch.object(log_rotation.sys, "stderr", closed):
            log_rotation._report("log_rotation: something went wrong")

    def test_a_full_disk_does_not_propagate_out_of_rotation(self) -> None:
        """End to end: the copy fails BECAUSE the disk is full, and so
        does the attempt to say so. `app.py`'s main() calls this
        unguarded, and KeepAlive{SuccessfulExit: false} turns a non-zero
        exit into a respawn loop."""
        with mock.patch.object(log_rotation.sys, "stderr", self._FullDisk()):
            with mock.patch.object(
                log_rotation.shutil,
                "copyfileobj",
                side_effect=OSError(28, "No space left on device"),
            ):
                rotate_if_oversized(self.log_path, max_bytes=SMALL_CAP)

        self.assertEqual(self.log_path.read_text(), self.content)


class SymlinkTests(unittest.TestCase):
    """Rotation must not act through a symlink.

    `path.replace()` moved the *link*, so the old implementation never
    followed one; plain `open()`/`os.truncate()` would, which reverses a
    defensive property without anyone deciding to. `diagnostics._open_log()`
    states the same posture, and `arm_stack_dumps()` rotates a path it then
    opens with `O_NOFOLLOW` — so following here meant rotating through a
    link and then refusing the very same path one line later.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.target = self.tmpdir / "real-target.log"
        self.content = "x" * (SMALL_CAP + 1)
        self.target.write_text(self.content)
        self.link = self.tmpdir / "display.stdout.log"
        self.link.symlink_to(self.target)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_symlinked_log_is_refused_and_reported(self) -> None:
        buf = io.StringIO()
        with redirect_stderr(buf):
            rotate_if_oversized(self.link, max_bytes=SMALL_CAP)

        self.assertEqual(self.target.read_text(), self.content)
        self.assertFalse((self.tmpdir / "display.stdout.log.old").exists())
        self.assertIn("failed to copy", buf.getvalue())
        self.assertEqual(
            sorted(q.name for q in self.tmpdir.iterdir()),
            ["display.stdout.log", "real-target.log"],
            "the refused rotation left a temp file behind",
        )


if __name__ == "__main__":
    unittest.main()
