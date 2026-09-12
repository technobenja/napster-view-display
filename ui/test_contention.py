"""The half of the contended-launch path that touches the machine.

`ui/test_contention_state.py` covers the pure classifier. This file
covers `ctypes`, the window server, a real writability probe and a real
signal — against real processes, because the whole point of these four
observations is that they are made from outside a process that will not
cooperate, and a mocked `proc_pidpath` proves nothing about that.

Two gates here are the kind that pass for the wrong reason if written
casually, and each carries its guard:

* **The writability probe** is checked under `chmod 0o500`. Root ignores
  permission bits, so the test asserts `os.geteuid() != 0` and skips
  **loudly** rather than reporting a pass it did not earn.

* **The stack-capture refusal** is checked by spawning a real child that
  has *not* armed `faulthandler` and confirming it is **still alive**
  afterwards. `SIGUSR1`'s default disposition is terminate, so if the
  precondition is ever removed this test does not merely fail — the
  child dies, which is the exact harm the precondition prevents.

Anything that needs the window server is skipped loudly outside an Aqua
session. A green CI badge does not cover those, and `ui/README.md` says
so.
"""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from display import diagnostics, paths
from ui import contention
from ui import contention_state as cs

CHILD_SECONDS = 30


def aqua_session() -> bool:
    """Whether this process is in a graphical login session.

    `launchctl managername` is the same discriminator the release gate
    uses to tell "the keychain is locked" from "there are no secrets" —
    a Background session (SSH, a Claude Code run, a launchd agent) has no
    window server, and a test that silently passes there is a test that
    covers nothing on the only machine that matters.
    """
    try:
        done = subprocess.run(
            ["launchctl", "managername"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.stdout.strip() == "Aqua"


AQUA = aqua_session()
NOT_AQUA = (
    "needs a window server: `launchctl managername` is not Aqua, so "
    "CGWindowListCopyWindowInfo has nothing to answer with. A green run "
    "here has NOT covered the window-server observations."
)


def facts(**overrides) -> cs.Facts:
    base = dict(
        lock_path="/tmp/ui.lock",
        lock_pid=os.getpid(),
        holder_exe=contention.own_exe(),
        holder_started_at=contention.proc_start_time(os.getpid()),
        our_exe=contention.own_exe(),
        display_pid=None,
        heartbeat_at=0.0,
        heartbeat_present=True,
        heartbeat_pid=None,
        stacks_armed=False,
        stacks_path=None,
        state_dir="/tmp",
        state_writable=True,
        state_detail=None,
        windows=(),
        display_windows=None,
        now=time.time(),
    )
    base.update(overrides)
    return cs.Facts(**base)


class ProcPidPathTests(unittest.TestCase):
    """Identity, against real processes."""

    def test_our_own_pid_resolves(self) -> None:
        got = contention.proc_pidpath(os.getpid())
        self.assertIsNotNone(got)
        self.assertTrue(Path(got).is_absolute())

    def test_our_own_path_is_not_sys_executable_under_a_venv(self) -> None:
        """Not a preference — a measured fact this code depends on. The
        kernel records the framework interpreter, not the venv launcher,
        so an `is_ours` written against `sys.executable` would reject our
        own twin in every source-tree run."""
        got = contention.proc_pidpath(os.getpid())
        self.assertTrue(cs.is_ours(got, contention.own_exe()))
        if Path(sys.executable).resolve() != Path(got).resolve():
            self.assertFalse(
                cs.is_ours(got, sys.executable),
                "if these ever agree, the comment in is_ours() needs revisiting",
            )

    def test_a_real_spawned_sleep_is_rejected(self) -> None:
        """🔴 The gate the plan names. A recycled pid belonging to
        something else must not read as a live, wedged ImageView — the
        notice would tell a user to force-quit it."""
        child = subprocess.Popen(["/bin/sleep", str(CHILD_SECONDS)])
        self.addCleanup(_reap, child)
        path = _wait_for_path(child.pid)
        self.assertEqual(path, "/bin/sleep")
        self.assertFalse(cs.is_ours(path, contention.own_exe()))
        self.assertIsNotNone(
            cs.unidentified_reason(facts(lock_pid=child.pid, holder_exe=path))
        )

    def test_an_absent_pid_is_none(self) -> None:
        self.assertIsNone(contention.proc_pidpath(999_999))

    def test_a_reaped_child_is_none(self) -> None:
        """The realistic race: the holder exits between the lock read and
        the identity check."""
        child = subprocess.Popen(["/bin/sleep", "1"])
        child.kill()
        child.wait()
        self.assertIsNone(contention.proc_pidpath(child.pid))

    def test_nonsense_pids_are_none_without_a_syscall(self) -> None:
        for pid in (None, 0, -1, -99999):
            with self.subTest(pid=pid):
                self.assertIsNone(contention.proc_pidpath(pid))


class ProcStartTimeTests(unittest.TestCase):
    """The identity that survives pid reuse.

    An executable path does not: the kernel hands a recycled pid to a new
    process and `proc_pidpath` answers about *that* one, truthfully. A
    start time is stamped at fork, so it cannot be shared by two
    processes on the same pid — which turns "probably still the same
    process" into "the same process".
    """

    def test_our_own_start_time_is_a_plausible_pair(self) -> None:
        got = contention.proc_start_time(os.getpid())
        self.assertIsNotNone(got)
        seconds, micros = got
        self.assertGreater(seconds, 1_500_000_000)
        self.assertLessEqual(seconds, int(time.time()) + 1)
        self.assertTrue(0 <= micros < 1_000_000)

    def test_an_absent_pid_is_none_not_a_zero_pair(self) -> None:
        """🔴 `proc_pidinfo` returns 0 for a pid that is gone, leaving the
        struct as this process zeroed it. A size check that accepted that
        would hand back `(0, 0)` — a *comparable value* — and two absent
        pids would then compare equal, which on the signal path reads as
        "the same process"."""
        self.assertIsNone(contention.proc_start_time(999_999))

    def test_a_reaped_child_is_none(self) -> None:
        child = subprocess.Popen(["/bin/sleep", "1"])
        child.kill()
        child.wait()
        self.assertIsNone(contention.proc_start_time(child.pid))

    def test_nonsense_pids_are_none(self) -> None:
        for pid in (None, 0, -1, -99999):
            with self.subTest(pid=pid):
                self.assertIsNone(contention.proc_start_time(pid))

    def test_it_is_stable_for_one_process(self) -> None:
        first = contention.proc_start_time(os.getpid())
        time.sleep(0.05)
        self.assertEqual(contention.proc_start_time(os.getpid()), first)

    def test_two_processes_do_not_share_a_start_time(self) -> None:
        """If they did, the whole re-check would be decorative."""
        child = subprocess.Popen(["/bin/sleep", str(CHILD_SECONDS)])
        self.addCleanup(_reap, child)
        _wait_for_path(child.pid)
        self.assertNotEqual(
            contention.proc_start_time(child.pid),
            contention.proc_start_time(os.getpid()),
        )


class VerifiedDisplayPidTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.lock = Path(self._tmp.name) / "display.lock"

    def test_an_absent_lock_file_yields_none(self) -> None:
        self.assertIsNone(
            contention.verified_display_pid(self.lock, contention.own_exe())
        )

    def test_our_own_pid_verifies(self) -> None:
        self.lock.write_text(f"{os.getpid()}\n")
        self.assertEqual(
            contention.verified_display_pid(self.lock, contention.own_exe()),
            os.getpid(),
        )

    def test_a_foreign_pid_is_dropped_rather_than_reported(self) -> None:
        """🔴 Never an unverified number. The only use for this pid is a
        "leave that one alone" sentence, and an unverified pid there
        points confidently at whatever is running under that id."""
        child = subprocess.Popen(["/bin/sleep", str(CHILD_SECONDS)])
        self.addCleanup(_reap, child)
        _wait_for_path(child.pid)
        self.lock.write_text(f"{child.pid}\n")
        self.assertIsNone(
            contention.verified_display_pid(self.lock, contention.own_exe())
        )

    def test_a_garbage_lock_file_yields_none(self) -> None:
        self.lock.write_text("not a pid")
        self.assertIsNone(
            contention.verified_display_pid(self.lock, contention.own_exe())
        )


class ProbeWritableTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_a_writable_directory_passes(self) -> None:
        ok, detail = contention.probe_writable(self.root)
        self.assertTrue(ok)
        self.assertIsNone(detail)

    def test_the_probe_leaves_nothing_behind(self) -> None:
        """This runs against a *running instance's* state directory."""
        contention.probe_writable(self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_an_absent_directory_is_created(self) -> None:
        nested = self.root / "state"
        ok, _ = contention.probe_writable(nested)
        self.assertTrue(ok)
        self.assertTrue(nested.is_dir())

    def test_a_file_where_the_directory_should_be_fails(self) -> None:
        blocker = self.root / "state"
        blocker.write_text("i am a file")
        ok, detail = contention.probe_writable(blocker)
        self.assertFalse(ok)
        self.assertTrue(detail)

    def test_an_unwritable_directory_fails(self) -> None:
        """⚠️ Root ignores permission bits entirely, so under `sudo` this
        would pass while proving nothing. Skip loudly rather than
        silently."""
        if os.geteuid() == 0:
            self.skipTest(
                "running as root: chmod 0o500 is not enforced for uid 0, so this "
                "test would pass without exercising the probe at all. Re-run as "
                "an ordinary user — this case is NOT covered by this run."
            )
        locked = self.root / "locked"
        locked.mkdir()
        locked.chmod(0o500)
        self.addCleanup(locked.chmod, 0o700)
        ok, detail = contention.probe_writable(locked)
        self.assertFalse(ok)
        self.assertIn("denied", (detail or "").lower())

    def test_an_unwritable_directory_reaches_the_muted_verdict(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("see test_an_unwritable_directory_fails — NOT covered")
        locked = self.root / "locked"
        locked.mkdir()
        locked.chmod(0o500)
        self.addCleanup(locked.chmod, 0o700)
        ok, detail = contention.probe_writable(locked)
        stale = facts(
            heartbeat_at=time.time() - 1000.0, state_writable=ok, state_detail=detail
        )
        self.assertIs(cs.classify(stale), cs.Verdict.MUTED)


@unittest.skipUnless(AQUA, NOT_AQUA)
class WindowFactsTests(unittest.TestCase):
    """Read-only queries against the live window server. Nothing here
    creates, moves or touches a window."""

    def test_our_own_pid_is_asked_and_answered(self) -> None:
        """`()` not `None`: asked, and this process owns nothing."""
        got = contention.window_facts(os.getpid())
        self.assertIsNotNone(got, "an Aqua session must be able to ask")
        self.assertIsInstance(got, tuple)

    def test_an_absent_pid_is_asked_and_found_empty(self) -> None:
        self.assertEqual(contention.window_facts(999_999), ())

    def test_a_nonsense_pid_is_not_asked_at_all(self) -> None:
        for pid in (None, 0, -1):
            with self.subTest(pid=pid):
                self.assertIsNone(contention.window_facts(pid))

    def test_every_field_read_needs_no_screen_recording(self) -> None:
        """The four fields this app reads are readable without a TCC
        grant; `kCGWindowName` is not, and is never read. Asserted
        against whatever is on screen right now."""
        import Quartz

        info = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID
        )
        self.assertTrue(len(info) > 0, "an Aqua session has windows on screen")
        for entry in info:
            for key in (
                "kCGWindowOwnerPID",
                "kCGWindowLayer",
                "kCGWindowBounds",
                "kCGWindowIsOnscreen",
            ):
                self.assertIn(key, entry)

    def test_window_names_are_redacted_which_is_why_none_are_read(self) -> None:
        """Not a requirement, a *measurement*, pinned so that anyone who
        later reaches for `kCGWindowName` sees why they must not: without
        Screen Recording most names are absent, so a classifier keyed on
        them behaves differently depending on a permission this app does
        not ask for."""
        import Quartz

        info = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID
        )
        named = sum(1 for entry in info if entry.get("kCGWindowName"))
        self.assertLess(
            named,
            len(info),
            "every window name was readable — this process appears to hold "
            "Screen Recording, so this measurement does not describe a "
            "normal user's machine",
        )


class CaptureStacksTests(unittest.TestCase):
    """A real signal to a real process. 🔴 `SIGUSR1` terminates by
    default; every refusal below is checked by confirming the child
    survives."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dump = Path(self._tmp.name) / "ui.stacks.log"

    def _armed_child(self) -> subprocess.Popen:
        script = (
            "import faulthandler, signal, sys, time\n"
            f"handle = open({str(self.dump)!r}, 'a', buffering=1)\n"
            "faulthandler.register(signal.SIGUSR1, file=handle, all_threads=True)\n"
            "print('ready', flush=True)\n"
            f"time.sleep({CHILD_SECONDS})\n"
        )
        # `own_exe()`, not `sys.executable`. MEASURED: a child spawned as
        # `display/.venv/bin/python3` reports
        # `.../bin/python3.13` to `proc_pidpath`, while *this* process
        # reports `.../Python.app/Contents/MacOS/Python` — the framework
        # stub a venv re-execs through for window-server access. They are
        # the same installation and different paths, so a child spawned
        # the obvious way reads as a foreign process and `is_ours`
        # correctly refuses it. Spawning the executable the kernel
        # records for us is what makes this child our twin.
        child = subprocess.Popen(
            [contention.own_exe() or sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(_reap, child)
        self.assertEqual(child.stdout.readline().strip(), "ready")
        return child

    def _bare_child(self) -> subprocess.Popen:
        child = subprocess.Popen(
            [
                contention.own_exe() or sys.executable,
                "-c",
                f"import time; time.sleep({CHILD_SECONDS})",
            ]
        )
        self.addCleanup(_reap, child)
        _wait_for_path(child.pid)
        return child

    def _facts_for(self, child: subprocess.Popen, **overrides) -> cs.Facts:
        base = dict(
            lock_pid=child.pid,
            holder_exe=contention.proc_pidpath(child.pid),
            holder_started_at=contention.proc_start_time(child.pid),
            heartbeat_pid=child.pid,
            stacks_armed=True,
            stacks_path=str(self.dump),
        )
        base.update(overrides)
        return facts(**base)

    def test_an_armed_holder_is_signalled_and_the_dump_lands(self) -> None:
        child = self._armed_child()
        got = contention.capture_stacks(self._facts_for(child), cs.Verdict.UNRESPONSIVE)
        self.assertTrue(got.signalled)
        self.assertTrue(got.grew, f"no dump landed: {got}")
        self.assertEqual(got.path, str(self.dump))
        self.assertIn("most recent call first", self.dump.read_text())
        self.assertIsNone(child.poll(), "a stack dump must not kill the holder")

    def test_an_unarmed_holder_is_never_signalled_and_survives(self) -> None:
        """🔴 The refusal, as code. `SIGUSR1`'s handler exists only from
        v1.1.3, and this app upgrades by dragging a new bundle over a
        running old one — so a v1.1.6 arriving instance can perfectly
        well meet a v1.1.2 holder. Signalling it destroys the evidence in
        the one situation where the user was being careful.

        If the `stacks_armed` precondition is removed, this does not
        merely fail: the child dies."""
        child = self._bare_child()
        got = contention.capture_stacks(
            self._facts_for(child, stacks_armed=False), cs.Verdict.UNRESPONSIVE
        )
        self.assertFalse(got.signalled)
        self.assertTrue(got.wanted)
        self.assertIn("terminate", got.refused or "")
        time.sleep(0.3)
        self.assertIsNone(
            child.poll(), "the unarmed holder was SIGNALLED and is now dead"
        )

    def test_disagreeing_pids_are_never_signalled_and_the_child_survives(self) -> None:
        """🔴 "If the lock's pid and ui_status.json's pid disagree,
        signal nothing and say so." The lock pid may be recycled."""
        child = self._bare_child()
        got = contention.capture_stacks(
            self._facts_for(child, heartbeat_pid=child.pid + 1),
            cs.Verdict.UNRESPONSIVE,
        )
        self.assertFalse(got.signalled)
        self.assertIn("disagree", got.refused or "")
        time.sleep(0.3)
        self.assertIsNone(child.poll())

    def test_a_foreign_holder_is_never_signalled(self) -> None:
        child = subprocess.Popen(["/bin/sleep", str(CHILD_SECONDS)])
        self.addCleanup(_reap, child)
        _wait_for_path(child.pid)
        got = contention.capture_stacks(
            self._facts_for(child), cs.Verdict.UNRESPONSIVE
        )
        self.assertFalse(got.signalled)
        self.assertIn("unidentified", got.refused or "")
        time.sleep(0.3)
        self.assertIsNone(child.poll(), "/bin/sleep was signalled and died")

    def test_an_armed_holder_with_no_path_is_never_signalled(self) -> None:
        child = self._bare_child()
        got = contention.capture_stacks(
            self._facts_for(child, stacks_path=None), cs.Verdict.UNRESPONSIVE
        )
        self.assertFalse(got.signalled)
        time.sleep(0.3)
        self.assertIsNone(child.poll())

    def test_a_nul_byte_in_the_stacks_path_is_not_a_traceback(self) -> None:
        """`stacks_path` is a string read out of a JSON file, and
        `Path("/a\\0b").stat()` raises **ValueError: embedded null
        byte** — not an `OSError`, so it escaped a function that must not
        raise. `os.kill` is patched: this test is about the path
        handling, not the signal."""
        child = self._armed_child()
        with patch("os.kill"):
            got = contention.capture_stacks(
                self._facts_for(child, stacks_path="/tmp/a\x00b.log"),
                cs.Verdict.UNRESPONSIVE,
            )
        self.assertFalse(got.grew)
        self.assertIsNone(child.poll())

    def test_no_verdict_outside_the_two_wedge_ones_asks_for_a_dump(self) -> None:
        child = self._armed_child()
        for verdict in cs.Verdict:
            if verdict in cs.CAPTURE_STACKS_FOR:
                continue
            with self.subTest(verdict.value):
                got = contention.capture_stacks(self._facts_for(child), verdict)
                self.assertFalse(got.wanted)
                self.assertFalse(got.signalled)
        self.assertIsNone(child.poll())

    def test_a_nonpositive_pid_is_never_signalled(self) -> None:
        """🔴 `kill(0, sig)` signals every process in this process group
        and `kill(-1, sig)` every process this user may signal — which on
        a machine running this app includes the picture display.

        Hand-built `Facts` that pass every other gate: `is_ours` compares
        strings, and the heartbeat cross-check compares `-1 == -1`. Not
        reachable through `gather()` today, but `Facts` is a public type
        and 4b plus `tools/status.py` are new consumers of it — a safety
        that is emergent in one caller is no safety at all for the next.

        `os.kill` is patched and asserted never called. **It is not run
        unpatched on this machine**, deliberately."""
        for pid in (0, -1, -4242):
            with self.subTest(pid=pid):
                hostile = facts(
                    lock_pid=pid,
                    holder_exe=contention.own_exe(),
                    holder_started_at=contention.proc_start_time(os.getpid()),
                    heartbeat_pid=pid,
                    stacks_armed=True,
                    stacks_path=str(self.dump),
                )
                with patch("os.kill") as kill:
                    got = contention.capture_stacks(hostile, cs.Verdict.UNRESPONSIVE)
                kill.assert_not_called()
                self.assertFalse(got.signalled)
                self.assertTrue(got.refused)

    def test_the_pid_guard_holds_without_help_from_the_classifier(self) -> None:
        """🔴 The guard in `capture_stacks` must work on its own.

        Mutation-found: deleting the `<= 0` half of it survived the whole
        suite, because `unidentified_reason` is consulted first and has
        its own copy — so every test was proving the *caller's* guard.
        That is precisely the shape the review flagged: a safety that is
        emergent in one caller is no safety for the next, and 4b plus
        `tools/status.py` are new callers of this type.

        So the classifier's opinion is stubbed out and the guard is asked
        to stand alone. `os.kill` is patched throughout; `kill(-1, ...)`
        is never run on this machine."""
        for pid in (0, -1, -4242):
            with self.subTest(pid=pid):
                hostile = facts(
                    lock_pid=pid,
                    holder_exe=contention.own_exe(),
                    holder_started_at=contention.proc_start_time(os.getpid()),
                    heartbeat_pid=pid,
                    stacks_armed=True,
                    stacks_path=str(self.dump),
                )
                with patch.object(
                    contention.cstate, "unidentified_reason", return_value=None
                ):
                    with patch("os.kill") as kill:
                        got = contention.capture_stacks(
                            hostile, cs.Verdict.UNRESPONSIVE
                        )
                kill.assert_not_called()
                self.assertIn("no usable pid", got.refused or "")

    def test_a_nonpositive_pid_is_also_refused_by_the_classifier(self) -> None:
        """Both layers, because the one in `capture_stacks` is the one
        that guards the `os.kill` and the one in `unidentified_reason`
        is what keeps the *message* from naming it."""
        for pid in (0, -1):
            with self.subTest(pid=pid):
                hostile = facts(lock_pid=pid, holder_exe=contention.own_exe())
                self.assertIsNotNone(cs.unidentified_reason(hostile))
                self.assertIs(cs.classify(hostile), cs.Verdict.UNIDENTIFIED)

    def test_an_absent_heartbeat_pid_cannot_authorise_a_signal(self) -> None:
        """A document that omits `pid` while asserting
        `"stacks_armed": true` would skip the cross-check entirely, and
        "absent silently became approved" is a shape this repo has been
        bitten by. On the signal path the heartbeat pid must **agree**,
        not merely fail to contradict.

        Costs no compatibility: a build able to write `stacks_armed` is
        by construction a build that writes `pid`."""
        child = self._bare_child()
        with patch("os.kill") as kill:
            got = contention.capture_stacks(
                self._facts_for(child, heartbeat_pid=None), cs.Verdict.UNRESPONSIVE
            )
        kill.assert_not_called()
        self.assertFalse(got.signalled)
        self.assertIn("does not name the same pid", got.refused or "")
        time.sleep(0.3)
        self.assertIsNone(child.poll())

    def test_a_v115_document_still_classifies_without_a_pid(self) -> None:
        """The other side of the same coin: `classify` must keep
        tolerating a document with no `pid`, because every build before
        v1.1.5 wrote one. Only the signal path is strict."""
        self.assertIsNone(cs.unidentified_reason(facts(heartbeat_pid=None)))

    def test_a_pid_reused_between_gather_and_the_signal_is_not_signalled(self) -> None:
        """🔴 TOCTOU. Everything in `Facts` was read in `gather()`, and
        between that and the signal this process does a status read, a
        mkdir, an mkstemp-write-unlink, a second lock read, a second
        `proc_pidpath` and two window-server round trips.

        No attacker is needed: the user force-quits the wedged holder
        from Activity Monitor while the second copy is launching, macOS
        hands the pid to something new, and SIGUSR1 terminates it — which
        would present as exactly the unexplained disappearance this phase
        exists to diagnose.

        A start time is stamped at fork, so two processes cannot share a
        pid and a start time. Simulated by handing `capture_stacks` a
        `Facts` whose recorded start time is one microsecond off."""
        child = self._armed_child()
        recorded = contention.proc_start_time(child.pid)
        self.assertIsNotNone(recorded)
        stale = self._facts_for(
            child, holder_started_at=(recorded[0], recorded[1] + 1)
        )
        with patch("os.kill") as kill:
            got = contention.capture_stacks(stale, cs.Verdict.UNRESPONSIVE)
        kill.assert_not_called()
        self.assertFalse(got.signalled)
        self.assertIn("replaced", got.refused or "")
        self.assertIsNone(child.poll())

    def test_an_executable_that_changes_before_the_signal_is_not_signalled(self) -> None:
        """The reviewer's shape: `proc_pidpath` answers about whatever
        owns the pid *now*. Patched to return a foreign path on its
        **second** call — the re-read immediately before `os.kill`."""
        child = self._armed_child()
        # `Facts` is built FIRST, with the real reader, exactly as
        # `gather()` would — so the patch below only affects the single
        # re-read that `capture_stacks` performs immediately before the
        # signal. That is the second observation, and it is the one that
        # has to disagree.
        good = self._facts_for(child)
        self.assertEqual(good.holder_exe, contention.proc_pidpath(child.pid))
        with patch.object(contention, "proc_pidpath", return_value="/bin/sleep"):
            with patch("os.kill") as kill:
                got = contention.capture_stacks(good, cs.Verdict.UNRESPONSIVE)
        kill.assert_not_called()
        self.assertFalse(got.signalled)
        self.assertIn("executable changed", got.refused or "")
        self.assertIsNone(child.poll())

    def test_an_unavailable_start_time_refuses_rather_than_assuming(self) -> None:
        """"Cannot tell" costs a diagnostic; signalling the wrong process
        costs somebody's running program. The trade is explicit."""
        child = self._armed_child()
        with patch.object(contention, "proc_start_time", return_value=None):
            with patch("os.kill") as kill:
                got = contention.capture_stacks(
                    self._facts_for(child), cs.Verdict.UNRESPONSIVE
                )
        kill.assert_not_called()
        self.assertIn("start time could not be confirmed", got.refused or "")
        self.assertIsNone(child.poll())

    def test_the_happy_path_still_signals_after_all_of_that(self) -> None:
        """The gates must not have closed the door on the case they
        exist to serve."""
        child = self._armed_child()
        got = contention.capture_stacks(self._facts_for(child), cs.Verdict.UNRESPONSIVE)
        self.assertTrue(got.signalled)
        self.assertTrue(got.grew, f"no dump landed: {got}")
        self.assertIsNone(child.poll())

    def test_a_holder_that_exits_first_is_a_refusal_not_a_traceback(self) -> None:
        child = self._bare_child()
        pid = child.pid
        child.kill()
        child.wait()
        got = contention.capture_stacks(
            facts(
                lock_pid=pid,
                holder_exe=contention.own_exe(),
                heartbeat_pid=pid,
                stacks_armed=True,
                stacks_path=str(self.dump),
            ),
            cs.Verdict.UNRESPONSIVE,
        )
        self.assertFalse(got.grew)
        self.assertFalse(got.signalled)
        # Not a disjunction: `refused or not signalled` passes under most
        # mutations, which is the opposite of what a test is for. This
        # path now stops at the start-time re-check — the holder is gone,
        # so `proc_start_time` answers None — and that is the sentence it
        # must produce.
        self.assertIn("start time could not be confirmed", got.refused or "")


class GatherTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.ui_lock = self.root / "ui.lock"
        self.display_lock = self.root / "display.lock"
        self.status = self.root / "state" / "ui_status.json"
        self.state = self.root / "state"

    def gather(self, **kwargs) -> cs.Facts:
        return contention.gather(
            self.ui_lock, self.display_lock, self.status, self.state, **kwargs
        )

    def test_an_empty_machine_gathers_without_raising(self) -> None:
        got = self.gather()
        self.assertIsNone(got.lock_pid)
        self.assertIsNone(got.holder_exe)
        self.assertIsNone(got.display_pid)
        self.assertEqual(got.heartbeat_at, 0.0)
        self.assertIs(cs.classify(got), cs.Verdict.UNIDENTIFIED)

    def test_our_own_pid_in_the_lock_reads_as_identified(self) -> None:
        self.ui_lock.write_text(f"{os.getpid()}\n")
        got = self.gather()
        self.assertEqual(got.lock_pid, os.getpid())
        self.assertIsNone(cs.unidentified_reason(got))

    def test_the_heartbeat_record_round_trips_through_gather(self) -> None:
        from ui import menubar_state as ms

        self.ui_lock.write_text(f"{os.getpid()}\n")
        ms.write_ui_heartbeat(
            self.status,
            1234.5,
            pid=os.getpid(),
            exe="/x",
            stacks_armed=True,
            stacks_path="/l.log",
        )
        got = self.gather()
        self.assertEqual(got.heartbeat_at, 1234.5)
        self.assertEqual(got.heartbeat_pid, os.getpid())
        self.assertTrue(got.stacks_armed)
        self.assertEqual(got.stacks_path, "/l.log")

    def test_a_v115_heartbeat_with_only_a_stamp_still_identifies(self) -> None:
        """Backwards compatibility, in the direction that matters: a
        v1.1.5 holder wrote no pid, and must not read as an impostor."""
        from ui import menubar_state as ms

        self.ui_lock.write_text(f"{os.getpid()}\n")
        ms.write_ui_heartbeat(self.status, time.time())
        got = self.gather()
        self.assertIsNone(got.heartbeat_pid)
        self.assertFalse(got.stacks_armed)
        self.assertIsNone(cs.unidentified_reason(got))

    def test_an_absent_heartbeat_file_is_reported_as_absent(self) -> None:
        """🔴 Absent is not stale. Every build before v1.1.5 writes no
        `ui_status.json` at all, and this app upgrades by dragging a new
        bundle over a running old one — so `heartbeat_present` is what
        stops the first contended launch after an upgrade accusing a
        healthy holder."""
        self.ui_lock.write_text(f"{os.getpid()}\n")
        self.assertFalse(self.gather().heartbeat_present)

    def test_a_written_heartbeat_file_is_reported_as_present(self) -> None:
        from ui import menubar_state as ms

        self.ui_lock.write_text(f"{os.getpid()}\n")
        ms.write_ui_heartbeat(self.status, 1.0)
        self.assertTrue(self.gather().heartbeat_present)

    def test_an_unparseable_heartbeat_file_is_not_present(self) -> None:
        """A file that exists but is not a JSON object tells us nothing
        about the holder's vintage, so it must not read as "this build
        reports"."""
        self.ui_lock.write_text(f"{os.getpid()}\n")
        self.status.parent.mkdir(parents=True, exist_ok=True)
        self.status.write_text("{not json")
        self.assertFalse(self.gather().heartbeat_present)

    def test_the_state_probe_runs_against_the_real_directory(self) -> None:
        got = self.gather()
        self.assertTrue(got.state_writable)
        self.assertTrue(self.state.is_dir())
        self.assertEqual(list(self.state.iterdir()), [])


class ReportTests(unittest.TestCase):
    """The whole path, including the rule that only a human launch gets a
    response."""

    LABEL = "com.example.imageview.ui"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.ui_lock = self.root / "ui.lock"
        self.ui_lock.write_text(f"{os.getpid()}\n")
        self._note = patch.object(contention.diagnostics, "note")
        self.note = self._note.start()
        self.addCleanup(self._note.stop)

    def report(self, environ: dict) -> cs.Notice | None:
        return contention.report(
            self.ui_lock,
            self.root / "display.lock",
            self.root / "state" / "ui_status.json",
            self.root / "state",
            self.LABEL,
            environ=environ,
        )

    def notes(self) -> str:
        return "\n".join(str(call.args[0]) for call in self.note.call_args_list)

    def test_a_human_launch_gets_a_notice(self) -> None:
        got = self.report({})
        self.assertIsNotNone(got)
        self.assertIn(got.verdict, set(cs.Verdict))

    def test_an_agent_respawn_gets_none(self) -> None:
        self.assertIsNone(self.report({"XPC_SERVICE_NAME": self.LABEL}))
        self.assertIn("started by the LaunchAgent", self.notes())

    def test_both_launches_log_the_verdict(self) -> None:
        """Silence to the user is not silence to the log — that is the
        whole of Phase 1."""
        self.report({"XPC_SERVICE_NAME": self.LABEL})
        self.assertIn("contended launch", self.notes())

    def test_the_facts_line_distinguishes_could_not_look_from_nothing(self) -> None:
        with patch.object(contention, "window_facts", return_value=None):
            self.report({})
        self.assertIn("could not be listed", self.notes())
        self.note.reset_mock()
        with patch.object(contention, "window_facts", return_value=()):
            self.report({})
        self.assertIn("windows none", self.notes())

    def test_a_broken_gather_returns_none_instead_of_raising(self) -> None:
        """🔴 The arriving instance is a diagnostic; if it throws, it
        becomes the fault. Its caller must `return 0`, and a non-zero
        exit is respawned by KeepAlive{SuccessfulExit: false}."""
        with patch.object(contention, "gather", side_effect=RuntimeError("boom")):
            self.assertIsNone(self.report({}))

    def test_a_broken_classifier_returns_none_instead_of_raising(self) -> None:
        with patch.object(contention.cstate, "classify", side_effect=ValueError("no")):
            self.assertIsNone(self.report({}))

    def test_a_broken_note_does_not_escape(self) -> None:
        self.note.side_effect = RuntimeError("the log is on fire")
        # The traceback this produces is the designed behaviour — the
        # broad except prints and returns None — but it belongs in this
        # test's buffer, not in the suite's output.
        with contextlib.redirect_stderr(io.StringIO()) as buf:
            self.assertIsNone(self.report({}))
        self.assertIn("could not report on the lock holder", buf.getvalue())

    def test_an_agent_launch_never_signals_the_holder(self) -> None:
        """An agent respawn losing the lock is what happens at every
        login. Signalling the holder there would dump its stacks on every
        boot for no reason."""
        with patch.object(contention, "capture_stacks") as capture:
            self.report({"XPC_SERVICE_NAME": self.LABEL})
        capture.assert_not_called()


class ArmingContractTests(unittest.TestCase):
    """`stacks_armed` is only trustworthy if `diagnostics` actually
    publishes what it armed."""

    def test_the_armed_path_is_readable_after_arming(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(paths, "log_dir", return_value=Path(tmp)):
                self.assertIsNone(diagnostics.armed_stacks_path())
                armed = diagnostics.arm_stack_dumps("uitest")
                self.addCleanup(diagnostics.disarm_stack_dumps)
                self.assertEqual(diagnostics.armed_stacks_path(), armed)
            diagnostics.disarm_stack_dumps()
            self.assertIsNone(diagnostics.armed_stacks_path())

    def test_an_unguarded_instance_does_not_rotate(self) -> None:
        """A process holding no lock has not proven it is the sole
        writer, and rotation truncates in place — it would empty a
        running instance's dumps."""
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(paths, "log_dir", return_value=Path(tmp)):
                with patch.object(diagnostics, "rotate_if_oversized") as rotate:
                    diagnostics.arm_stack_dumps("uitest", sole_writer=False)
                    self.addCleanup(diagnostics.disarm_stack_dumps)
                    rotate.assert_not_called()

    def test_a_guarded_instance_does_rotate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(paths, "log_dir", return_value=Path(tmp)):
                with patch.object(diagnostics, "rotate_if_oversized") as rotate:
                    diagnostics.arm_stack_dumps("uitest", sole_writer=True)
                    self.addCleanup(diagnostics.disarm_stack_dumps)
                    rotate.assert_called_once()


def _wait_for_path(pid: int, timeout: float = 5.0) -> str | None:
    """Wait for a just-spawned child to be visible to `proc_pidpath`.

    `Popen` returns before the child has finished `execve`, so an
    immediate lookup can legitimately answer the *parent's* path or
    nothing at all. Polling rather than sleeping keeps the suite fast.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        path = contention.proc_pidpath(pid)
        if path is not None:
            return path
        time.sleep(0.01)
    return None


def _reap(child: subprocess.Popen) -> None:
    try:
        child.kill()
    except OSError:
        pass
    try:
        child.wait(timeout=5)
    except subprocess.SubprocessError:
        pass
    if child.stdout is not None:
        child.stdout.close()


class AnchorEvidenceTests(unittest.TestCase):
    """`observe()` carries what Phase 4b needs to place its window.

    `report()` returns the verdict and nothing else, and a `Notice`
    carries window *layers* rather than window *bounds* — deliberately,
    because the words must never contain a screen coordinate. 4b's
    geometry needs exactly the bounds the words must not have, so
    `observe()` hands them over alongside. Nothing here decides anything;
    what it must not do is re-derive "can a person see this icon" from
    geometry of its own.

    `gather` is rebound rather than mocked so that the real `classify`,
    the real `notice` and the real wiring all run. The stamp is fresh, so
    the verdict is `SERVING_VISIBLE`, which is not in `CAPTURE_STACKS_FOR`
    — nothing is signalled.
    """

    #: MEASURED on this machine: the holder owns a status item on the menu
    #: bar and one on the picture display, and the buried one reports
    #: `kCGWindowIsOnscreen: True` exactly like the other.
    MENU_BAR = cs.WindowFact(layer=25, on_screen=True, x=1169.0, y=0.0, width=34.0, height=24.0)
    BURIED = cs.WindowFact(layer=25, on_screen=True, x=842.0, y=-960.0, width=34.0, height=24.0)
    PICTURE = cs.WindowFact(
        layer=1000, on_screen=True, x=313.0, y=-960.0, width=960.0, height=960.0
    )

    def observe_with(self, prepared: cs.Facts) -> contention.Report:
        original = contention.gather
        contention.gather = lambda *args, **kwargs: prepared
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                return contention.observe(
                    Path("/tmp/ui.lock"),
                    Path("/tmp/display.lock"),
                    Path("/tmp/ui_status.json"),
                    Path("/tmp"),
                    "an.agent.label",
                    environ={},
                )
        finally:
            contention.gather = original

    def prepared(self, **overrides) -> cs.Facts:
        base = dict(
            heartbeat_at=time.time(),
            heartbeat_present=True,
            windows=(self.MENU_BAR, self.BURIED),
            display_windows=(self.PICTURE,),
            now=time.time(),
        )
        base.update(overrides)
        return facts(**base)

    def test_the_buried_icon_is_never_offered_as_an_anchor(self) -> None:
        """🔴 The one that keeps a diagnostic window off a 2.1-inch round
        display. `visible_status_items`, not `status_items`: macOS gives a
        status item a window on **every** attached screen, and the copy on
        the picture display sits underneath a full-screen picture while
        reporting itself on screen."""
        got = self.observe_with(self.prepared())
        self.assertEqual(got.icons, (self.MENU_BAR,))

    def test_the_picture_windows_come_through_so_that_screen_can_be_excluded(self) -> None:
        got = self.observe_with(self.prepared())
        self.assertEqual(got.avoid, (self.PICTURE,))

    def test_an_unlistable_display_agent_offers_no_anchor_at_all(self) -> None:
        """🔴 The same `None` that is benign for the words is harmful for
        the geometry, and this is the seam that knows the difference.

        `visible_status_items` treats "the display agent's windows could
        not be listed" as "exclude nothing", which for 4a's copy is the
        benign reading. For 4b it means the icon parked on the picture
        display survives as an anchor, `avoid` is empty at the same
        instant, and the panel is drawn on a 2.1-inch round screen under
        the slideshow — where the person who double-clicked cannot see it.

        It needs no window-server failure to happen:
        `verified_display_pid` answers None whenever the display lock is
        unreadable or names a pid that fails the executable check, while
        the display agent is drawing perfectly.
        """
        got = self.observe_with(self.prepared(display_windows=None))
        self.assertEqual(got.icons, ())
        self.assertEqual(got.avoid, ())
        self.assertIsNotNone(got.notice, "the verdict is unaffected; only the anchor is")

    def test_a_listable_display_agent_still_offers_the_menu_bar_icon(self) -> None:
        """The other side of the same guard: refusing the anchor is for
        "could not look", not for every launch."""
        self.assertEqual(self.observe_with(self.prepared()).icons, (self.MENU_BAR,))

    def test_an_offscreen_display_window_excludes_nothing(self) -> None:
        """A screen is only the picture display while something is
        actually drawing on it."""
        hidden = cs.WindowFact(
            layer=1000, on_screen=False, x=313.0, y=-960.0, width=960.0, height=960.0
        )
        got = self.observe_with(self.prepared(display_windows=(hidden,)))
        self.assertEqual(got.avoid, ())

    def test_a_window_server_that_could_not_be_asked_offers_nothing(self) -> None:
        """"could not look" is not "not found" — the caller centres its
        window instead of pointing at a coordinate it does not have."""
        got = self.observe_with(self.prepared(windows=None, display_windows=None))
        self.assertEqual(got.icons, ())
        self.assertEqual(got.avoid, ())
        self.assertIsNotNone(got.notice, "a human launch still gets a verdict")

    def test_an_agent_respawn_carries_no_notice(self) -> None:
        """And therefore shows no window. `report()` has always said this;
        it is asserted again here because `observe()` is what the menu bar
        now calls."""
        original = contention.gather
        prepared = self.prepared()
        contention.gather = lambda *args, **kwargs: prepared
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                got = contention.observe(
                    Path("/tmp/ui.lock"),
                    Path("/tmp/display.lock"),
                    Path("/tmp/ui_status.json"),
                    Path("/tmp"),
                    "an.agent.label",
                    environ={"XPC_SERVICE_NAME": "an.agent.label"},
                )
        finally:
            contention.gather = original
        self.assertIsNone(got.notice)

    def test_report_is_still_just_the_verdict(self) -> None:
        """The narrower contract stays narrow: a caller that only wants to
        know what was found must not start receiving geometry."""
        original = contention.gather
        prepared = self.prepared()
        contention.gather = lambda *args, **kwargs: prepared
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                got = contention.report(
                    Path("/tmp/ui.lock"),
                    Path("/tmp/display.lock"),
                    Path("/tmp/ui_status.json"),
                    Path("/tmp"),
                    "an.agent.label",
                    environ={},
                )
        finally:
            contention.gather = original
        self.assertIsInstance(got, cs.Notice)


if __name__ == "__main__":
    unittest.main()
