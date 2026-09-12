"""Watching the lock holder from outside, because it will not cooperate.

`ui/contention_state.py` decides what a losing launch may say. This
module is the shell that gathers what it decides from — the part that
touches `ctypes`, the window server, the filesystem and a signal.

**The idea this phase turns on.** Every earlier attempt to tell a wedged
menu bar from a busy one added a second *in-process* signal, and each one
failed on the same rock: a modal run loop is identical whether or not a
human can see the panel, so no timer inside the wedged process can
separate "invisibly deadlocked" from "showing you a dialog". The process
*asking* the question is healthy, is on the same machine, and can simply
look. All four observations below were measured available on macOS with
**no TCC grant of any kind**.

**Three rules this module keeps.**

1. **Nothing here may raise.** The arriving instance is a diagnostic; if
   it throws, it becomes the fault — and its caller is the contention
   path in `menubar.main()`, which must `return 0`, because
   `KeepAlive {SuccessfulExit: false}` respawns exactly the non-zero
   exits a traceback would produce. Every function returns a value and
   swallows its own failures, matching `diagnostics.py` and `paths.py`.

2. **Nothing here acts.** No kill, no quit, no take-over, no window. The
   one signal sent is `SIGUSR1`, which is a *read* of the holder's
   stacks — and see `capture_stacks` for the precondition that keeps it
   one.

3. **"Could not look" is never reported as "not found."** The window
   server returning nothing because it could not be asked, and returning
   nothing because the holder owns nothing, are different answers;
   `window_facts` returns `None` for the first and `()` for the second.
   This project has paid for that distinction twice — a `screencapture`
   that exits 0 with bare wallpaper, and a keychain sweep that read
   "cannot read" as "no secrets".
"""

from __future__ import annotations

import ctypes
import dataclasses
import os
import sys
import tempfile
import time
import traceback
from collections.abc import Mapping
from pathlib import Path

from display import diagnostics
from display import single_instance
from ui import contention_state as cstate
from ui import menubar_state as ms

#: `PROC_PIDPATHINFO_MAXSIZE` from `<sys/proc_info.h>`: 4 * `MAXPATHLEN`.
#: `proc_pidpath` writes at most this many bytes and the header is
#: explicit that a smaller buffer is an error rather than a truncation.
PROC_PIDPATHINFO_MAXSIZE = 4096

#: How long to wait for the holder's stack dump to land. `faulthandler`
#: writes from a C signal handler while the GIL is held, so it completes
#: on a process that cannot execute a single line of Python — the wait is
#: for the kernel to deliver and for the write to reach the file, not for
#: the holder to do any work. A second is generous for that and is the
#: entire cost of this path, paid only on the two verdicts that ask for a
#: dump.
STACK_CAPTURE_WAIT_S = 1.0

#: Poll interval while waiting. Short enough that the common case returns
#: in well under a tenth of the budget.
STACK_CAPTURE_POLL_S = 0.05


def _libproc() -> ctypes.CDLL | None:
    """The handle `proc_pidpath` lives behind, or None.

    `CDLL(None)` opens the running process's own symbol table, which on
    macOS includes libSystem and therefore libproc. Looking the library
    up by name instead is the obvious-looking alternative and is worse:
    `libproc.dylib` has no on-disk path under the dyld shared cache, so
    `find_library` can answer None on a perfectly healthy system.

    Cached on the module rather than reopened: this runs once per launch
    today, and a failed open should not be retried in a loop by whatever
    calls it next.
    """
    global _LIBPROC, _LIBPROC_TRIED
    if _LIBPROC_TRIED:
        return _LIBPROC
    _LIBPROC_TRIED = True
    try:
        lib = ctypes.CDLL(None, use_errno=True)
        # Declared explicitly. Without argtypes, ctypes passes the pid as
        # a C int by luck and the buffer size as a signed int, and a
        # 64-bit pointer truncation here would be a segfault inside a
        # diagnostic.
        lib.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        lib.proc_pidpath.restype = ctypes.c_int
        lib.proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        lib.proc_pidinfo.restype = ctypes.c_int
    except (OSError, AttributeError, ValueError):
        _LIBPROC = None
    else:
        _LIBPROC = lib
    return _LIBPROC


_LIBPROC: ctypes.CDLL | None = None
_LIBPROC_TRIED = False


def proc_pidpath(pid: int | None) -> str | None:
    """The full executable path for a live pid, or None.

    None means, and only means, "this app could not obtain a path" —
    which covers an absent pid, a zombie that has been reaped, a pid we
    are not entitled to look at, and a libproc that could not be opened.
    Every one of those makes the holder unidentifiable, which is a single
    verdict, so they do not need to be told apart.

    MEASURED on this machine, 2026-09-10: a live `/bin/sleep` answers
    `/bin/sleep`; pid 999999 answers 0 bytes with `ESRCH`; a reaped child
    answers the same. **And our own pid under `display/.venv/bin/python3`
    answers the Homebrew framework's `Python`, not `sys.executable`** —
    which is why `contention_state.is_ours` compares against this
    function's answer for `os.getpid()` and never against
    `sys.executable`.
    """
    if pid is None or pid <= 0:
        return None
    lib = _libproc()
    if lib is None:
        return None
    try:
        buf = ctypes.create_string_buffer(PROC_PIDPATHINFO_MAXSIZE)
        size = lib.proc_pidpath(pid, buf, PROC_PIDPATHINFO_MAXSIZE)
        # ⚠️ `size <= 0` and the trailing `or None` are REDUNDANT, and a
        # mutation from `<= 0` to `< 0` therefore SURVIVES — recorded
        # here rather than left for the next reader to chase. MEASURED:
        # a failed lookup returns 0 *and* leaves the buffer empty, so
        # `b"".decode() or None` already answers None. Both stay: the
        # header's contract is "0 means failure", and depending on the
        # buffer being untouched on failure is an assumption about a
        # libproc implementation detail, exactly as `diagnostics.py`
        # keeps `_dump_file` despite measuring that CPython does not
        # need it.
        if size <= 0:
            return None
        return buf.value.decode("utf-8", errors="replace") or None
    except (OSError, ValueError, UnicodeError):
        return None


def own_exe() -> str | None:
    """This process's executable path, as the kernel records it."""
    return proc_pidpath(os.getpid())


#: `PROC_PIDTBSDINFO` from `<sys/proc_info.h>`.
PROC_PIDTBSDINFO = 3


class _ProcBsdInfo(ctypes.Structure):
    """`struct proc_bsdinfo`, truncated after the two fields wanted.

    Declared in full up to `pbi_start_tvusec` because the kernel copies
    out a fixed-size record and `proc_pidinfo` returns the number of
    bytes it wrote — so a struct of the wrong size is caught by the
    `!= sizeof` check below rather than by reading garbage at an offset.
    MEASURED on this machine: `sizeof` is 136 and `proc_pidinfo` returns
    exactly 136, which is the kernel agreeing with this layout.
    """

    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


def proc_start_time(pid: int | None) -> tuple[int, int] | None:
    """When `pid` started, as `(seconds, microseconds)`, or None.

    **This is the identity that survives pid reuse.** An executable path
    does not: the kernel hands a recycled pid to a new process and
    `proc_pidpath` answers about *that* one, perfectly truthfully. A
    start time cannot be shared by two processes on the same pid, because
    it is stamped at fork — so "same pid, same start time" is the same
    process, full stop, rather than a narrower window in which it
    probably is.

    Needs no TCC grant, measured on this machine 2026-09-10: it answers
    for our own process, for both halves of a running install, and
    returns None for an absent or reaped pid.
    """
    if pid is None or pid <= 0:
        return None
    lib = _libproc()
    if lib is None:
        return None
    try:
        info = _ProcBsdInfo()
        written = lib.proc_pidinfo(
            pid, PROC_PIDTBSDINFO, 0, ctypes.byref(info), ctypes.sizeof(info)
        )
        # A short (or zero) write means the pid is gone, or that this
        # layout no longer matches the kernel's. Both are "cannot tell",
        # and on the signal path "cannot tell" refuses to signal.
        if written != ctypes.sizeof(info):
            return None
        return (int(info.pbi_start_tvsec), int(info.pbi_start_tvusec))
    except (OSError, ValueError, AttributeError):
        return None


def window_facts(pid: int | None) -> tuple[cstate.WindowFact, ...] | None:
    """What the window server says `pid` has on screen.

    `()` means it was asked and the holder owns nothing. **`None` means
    it could not be asked** — no window server, no Quartz, or a call that
    failed — and `classify` is written so that `None` never produces a
    verdict that asserts something is absent.

    Only `kCGWindowOwnerPID`, `kCGWindowLayer`, `kCGWindowBounds` and
    `kCGWindowIsOnscreen` are read. `kCGWindowName` is deliberately not:
    MEASURED on this machine, 6 of 40 on-screen windows had a readable
    name, the rest being redacted without a Screen Recording grant. A
    classifier keyed on names would behave differently depending on a
    permission this app neither holds nor asks for.

    `kCGWindowIsOnscreen` defaults to True when the key is missing,
    because the *list itself* is the on-screen list — the query is
    `kCGWindowListOptionOnScreenOnly`, so membership is the primary
    evidence and the key is a cross-check, not the source.
    """
    if pid is None or pid <= 0:
        return None
    try:
        import Quartz
    except ImportError:
        return None
    try:
        info = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID
        )
    except Exception:  # noqa: BLE001 - a window-server call in a diagnostic
        return None
    if info is None:
        return None
    out: list[cstate.WindowFact] = []
    try:
        for entry in info:
            if entry.get("kCGWindowOwnerPID") != pid:
                continue
            bounds = entry.get("kCGWindowBounds") or {}
            out.append(
                cstate.WindowFact(
                    layer=int(entry.get("kCGWindowLayer") or 0),
                    on_screen=bool(entry.get("kCGWindowIsOnscreen", True)),
                    x=float(bounds.get("X", 0.0)),
                    y=float(bounds.get("Y", 0.0)),
                    width=float(bounds.get("Width", 0.0)),
                    height=float(bounds.get("Height", 0.0)),
                )
            )
    except Exception:  # noqa: BLE001 - a malformed entry must not be fatal
        return None
    return tuple(out)


def probe_writable(directory: Path) -> tuple[bool, str | None]:
    """Whether this process can actually write into `directory`.

    Returns `(True, None)` or `(False, reason)`. Never raises.

    **A real create-and-unlink, not `os.access`.** The question is
    whether the *holder* can write its heartbeat, and this is the row
    that would otherwise read a perfectly healthy process as dead, so it
    is worth answering properly: `os.access` reports on permission bits
    and misses a full disk, which is as plausible a cause here as a bad
    `chmod` and produces exactly the same silent staleness. The probe
    file is created, written to, and removed in a `finally`.

    The directory is created if absent, for the same reason
    `write_ui_heartbeat` creates its parent: an absent `state/` is a
    thing this app fixes rather than reports.

    ⚠️ **Root ignores permission bits**, so a test that `chmod 0o500`s a
    directory and expects False passes for the wrong reason under `sudo`.
    `ui/test_contention.py` asserts `os.geteuid() != 0` before that case
    and skips loudly otherwise.
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, exc.strerror or str(exc)
    except Exception:  # noqa: BLE001 - a diagnostic must not raise
        return False, "could not be created"

    fd = -1
    probe: Path | None = None
    try:
        fd, name = tempfile.mkstemp(prefix=".writeprobe-", dir=str(directory))
        probe = Path(name)
        os.write(fd, b"x")
    except OSError as exc:
        return False, exc.strerror or str(exc)
    except Exception:  # noqa: BLE001 - a diagnostic must not raise
        return False, "could not be written"
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if probe is not None:
            try:
                probe.unlink()
            except OSError:
                pass
    return True, None


def _holder_pid(lock_path: Path) -> int | None:
    """`read_holder_pid`, with a belt to its braces. It is documented not
    to raise, and this module's whole contract is that nothing on the
    launch path does."""
    try:
        return single_instance.read_holder_pid(lock_path)
    except Exception:  # noqa: BLE001 - a diagnostic must not raise
        return None


def verified_display_pid(display_lock_path: Path, our_exe_path: str | None) -> int | None:
    """The picture display's pid, **or None if it could not be verified.**

    🔴 Never returns an unverified number. The only reason this pid is
    read at all is so that a force-quit instruction can say "and leave
    that one alone" — and an unverified pid in that sentence is worse
    than no pid, because it points confidently at whatever happens to be
    running under that id. Same `proc_pidpath` + `is_ours` check the
    holder gets; a pid that fails it is dropped entirely.

    ⚠️ **The menu bar's pid can be the higher of the two** — the reverse
    of the intuitive reading, and observed that way. Nothing may infer
    which is which from magnitude, start order, or anything at all except
    the lock file that named it.
    """
    pid = _holder_pid(display_lock_path)
    if pid is None:
        return None
    if not cstate.is_ours(proc_pidpath(pid), our_exe_path):
        return None
    return pid


def gather(
    lock_path: Path,
    display_lock_path: Path,
    status_path: Path,
    state_dir: Path,
    now: float | None = None,
) -> cstate.Facts:
    """Collect the observations. Never raises.

    Ordered cheapest-first only incidentally; nothing here depends on
    anything else here except `our_exe`, which both identity checks
    share. That is the property that makes the classifier a pure function
    of the result rather than of the order it was gathered in.
    """
    moment = time.time() if now is None else now
    lock_pid = _holder_pid(lock_path)
    ours = own_exe()
    status = ms.read_ui_status(status_path)
    writable, detail = probe_writable(state_dir)
    display_pid = verified_display_pid(display_lock_path, ours)
    return cstate.Facts(
        lock_path=str(lock_path),
        lock_pid=lock_pid,
        holder_exe=proc_pidpath(lock_pid),
        # Captured here so that the signal path can prove, rather than
        # assume, that the pid still belongs to the same process after
        # everything below has been read. See `capture_stacks`.
        holder_started_at=proc_start_time(lock_pid),
        our_exe=ours,
        display_pid=display_pid,
        heartbeat_at=status.heartbeat_at,
        # "the file is not there" and "the stamp in it has gone cold"
        # are different facts about different builds — see
        # `contention_state.Facts.heartbeat_present`.
        heartbeat_present=status.present,
        heartbeat_pid=status.pid,
        stacks_armed=status.stacks_armed,
        stacks_path=status.stacks_path,
        state_dir=str(state_dir),
        state_writable=writable,
        state_detail=detail,
        windows=window_facts(lock_pid),
        # The display agent's windows are what turn "the holder owns a
        # status item" into "a person can see it": macOS puts a status
        # item on every screen, and one of this app's screens is a
        # 2.1-inch panel with a full-screen picture on it. See
        # `contention_state.visible_status_items`.
        display_windows=window_facts(display_pid),
        now=moment,
    )


def capture_stacks(facts: cstate.Facts, verdict: cstate.Verdict) -> cstate.Capture:
    """Ask the holder for its thread stacks, if and only if that is safe.

    🔴 **The precondition is the point of this function, not a guard on
    it.** `SIGUSR1`'s default disposition is **terminate**, and the
    handler that turns it into a stack dump exists only from v1.1.3. A
    v1.1.6 arriving instance can perfectly well find a v1.1.2 holder —
    this app updates by dragging a new bundle over an old one, and the
    holder is by definition the copy that was already running. Signalling
    it would destroy the process in the one situation where the user was
    being careful, and destroy the evidence this function exists to
    collect. So the arriving instance never guesses: it reads
    `stacks_armed` out of the heartbeat file, which only a process that
    actually armed the handler can have written.

    🔴 **And if the lock's pid and `ui_status.json`'s pid disagree,
    signal nothing and say so.** That is already `UNIDENTIFIED`, which is
    not in `CAPTURE_STACKS_FOR`, and it is re-checked here rather than
    assumed: this function must be safe to call with any verdict.

    Never raises. `os.kill` can raise `ProcessLookupError` (the holder
    exited between the lock read and here, which is a perfectly ordinary
    race) or `PermissionError` (a pid belonging to another user, which
    means the identity check was fooled); both become a refusal with a
    reason rather than a traceback on the launch path.
    """
    if verdict not in cstate.CAPTURE_STACKS_FOR:
        return cstate.Capture()
    reason = cstate.unidentified_reason(facts)
    if reason is not None:
        return cstate.Capture(wanted=True, refused=f"the holder is unidentified — {reason}")
    pid = facts.lock_pid
    if pid is None or pid <= 0:
        # 🔴 Checked here as well as in `unidentified_reason`, and the
        # duplication is the point: this is the function that calls
        # `os.kill`, and `kill(0, ...)` hits the caller's whole process
        # group while `kill(-1, ...)` hits every process the user may
        # signal — the picture display included. A guard that lives only
        # in a caller is a guard that a new caller does not have.
        return cstate.Capture(wanted=True, refused="no usable pid to signal")
    if facts.heartbeat_pid is None or facts.heartbeat_pid != pid:
        # Stricter than `classify`, deliberately, and only here. There,
        # an absent `pid` must be tolerated — every build before v1.1.5
        # wrote a document without one and is perfectly healthy. Here, an
        # absent `pid` would let a document that asserts
        # `"stacks_armed": true` skip the cross-check entirely, so
        # agreement is *required* rather than merely not-contradicted.
        # It costs no compatibility: a build able to write `stacks_armed`
        # is by construction a build that writes `pid`.
        return cstate.Capture(
            wanted=True,
            refused=(
                "the heartbeat file does not name the same pid that holds the "
                "lock, so nothing here can be sure which process it would be "
                "signalling"
            ),
        )
    if not facts.stacks_armed:
        return cstate.Capture(
            wanted=True,
            refused=(
                "that copy of ImageView does not arm the stack-dump handler, so "
                "signalling it would terminate it instead"
            ),
        )
    if not facts.stacks_path:
        return cstate.Capture(
            wanted=True,
            refused="it armed the handler but did not say where it writes",
        )

    # 🔴 Re-observe immediately before signalling. Everything above was
    # read in `gather()`, and between `proc_pidpath` there and here this
    # process has done a status read, a mkdir, an mkstemp-write-unlink, a
    # second lock read, a second `proc_pidpath` and **two window-server
    # round trips** — tens to hundreds of milliseconds. No attacker is
    # needed to lose that race: the user force-quits the wedged holder
    # from Activity Monitor while the second copy is launching, macOS
    # hands the pid to something new, and SIGUSR1 terminates it. That
    # would present as exactly the unexplained disappearance this phase
    # exists to diagnose.
    #
    # The start time is the load-bearing half and an executable path is
    # not: the kernel answers about whatever owns the pid *now*, quite
    # truthfully. Two processes cannot share a pid and a fork timestamp,
    # so "same pid, same start time" closes reuse outright instead of
    # narrowing the window. The path is re-read as well, cheaply, so that
    # a `proc_pidinfo` layout that stops matching some future kernel
    # cannot silently become the only thing being checked.
    #
    # If either observation is unavailable this REFUSES. The trade is
    # explicit: an unavailable check costs a diagnostic, and signalling
    # the wrong process costs somebody's running program.
    started_now = proc_start_time(pid)
    if started_now is None or facts.holder_started_at is None:
        return cstate.Capture(
            wanted=True,
            path=facts.stacks_path,
            refused="the holder's start time could not be confirmed",
        )
    if started_now != facts.holder_started_at:
        return cstate.Capture(
            wanted=True,
            path=facts.stacks_path,
            refused=(
                "the process holding the lock was replaced while this was being "
                "worked out, so the pid now belongs to something else"
            ),
        )
    exe_now = proc_pidpath(pid)
    if exe_now != facts.holder_exe or not cstate.is_ours(exe_now, facts.our_exe):
        return cstate.Capture(
            wanted=True,
            path=facts.stacks_path,
            refused="the holder's executable changed while this was being worked out",
        )

    path = Path(facts.stacks_path)
    try:
        before = path.stat().st_size
    except (OSError, ValueError):
        # `ValueError`, not only `OSError`: `stacks_path` is a string read
        # out of a JSON file, and a NUL byte in it makes `stat()` raise
        # `ValueError: embedded null byte` — which is not an OSError and
        # would escape a function that must not raise.
        before = -1

    try:
        os.kill(pid, diagnostics.STACK_DUMP_SIGNAL)
    except (OSError, ValueError, OverflowError) as exc:
        return cstate.Capture(
            wanted=True,
            path=facts.stacks_path,
            refused=f"it could not be signalled ({exc})",
        )

    deadline = time.monotonic() + STACK_CAPTURE_WAIT_S
    while time.monotonic() < deadline:
        try:
            if path.stat().st_size > before:
                return cstate.Capture(
                    wanted=True, signalled=True, grew=True, path=facts.stacks_path
                )
        except (OSError, ValueError):
            pass
        time.sleep(STACK_CAPTURE_POLL_S)
    return cstate.Capture(wanted=True, signalled=True, path=facts.stacks_path)


@dataclasses.dataclass(frozen=True)
class Report:
    """What a contended launch found, for a caller that has to draw it.

    `notice` is the whole of the decision and is what `report()` has
    always returned. The other two fields are **evidence for placement,
    not for any verdict**: Phase 4b puts its panel directly underneath the
    holder's menu bar icon, so that the copy never has to name a screen
    coordinate, and it must never put it on the picture display.

    * `icons` — the holder's status items **a person can see**, straight
      from `contention_state.visible_status_items`. Re-deriving them in a
      window layer would be a second implementation of "is this icon
      buried under the slideshow", and the two would disagree.
    * `avoid` — the picture display's on-screen windows. A screen holding
      one of these *is* the View, on the evidence of what is drawing
      there rather than a guess from a resolution or a display id.

    🔴 **This is plumbing. It decides nothing, and nothing downstream of
    it may.** The classifier, the thresholds and the verdicts are exactly
    as Phase 4a left them; a `Notice` deliberately carries `window_layers`
    and not window *bounds*, because the words must never contain a
    coordinate — and 4b's geometry needs the bounds the words must not
    have. That gap is why this type exists rather than a new `Notice`
    field.
    """

    notice: cstate.Notice | None
    icons: tuple[cstate.WindowFact, ...] = ()
    avoid: tuple[cstate.WindowFact, ...] = ()


def observe(
    lock_path: Path,
    display_lock_path: Path,
    status_path: Path,
    state_dir: Path,
    agent_label: str,
    environ: Mapping[str, str] | None = None,
    now: float | None = None,
) -> Report:
    """The whole of the contended-launch path. **Never raises.**

    Logs what it found either way, and carries a `Notice` **only for a
    human launch** — an agent respawn that loses the lock at login is
    routine, must not put anything on screen, and must not cost the
    holder a SIGUSR1 every time someone logs in.

    `report()` is this function's `notice`, and is the older, narrower
    entry point. Phase 4b needs the placement evidence as well, so it
    calls this; `tools/status.py` needs neither and calls `gather` and
    `classify` directly.

    The broad `except` is the same one `diagnostics.note` takes and for
    the same reason. Its caller must `return 0`; an exception escaping
    here would turn a correct silent exit into a traceback and a non-zero
    exit code, which `KeepAlive {SuccessfulExit: false}` respawns.
    """
    try:
        facts = gather(
            lock_path, display_lock_path, status_path, state_dir, now=now
        )
        verdict = cstate.classify(facts)
        human = not cstate.launched_by_agent(
            os.environ if environ is None else environ, agent_label
        )
        capture = capture_stacks(facts, verdict) if human else cstate.Capture()
        notice = cstate.notice(facts, verdict, capture)
        diagnostics.note(
            f"menubar: holder facts — lock pid {facts.lock_pid}, exe "
            f"{facts.holder_exe}, heartbeat pid {facts.heartbeat_pid}, "
            f"display pid {facts.display_pid}, stamp {facts.age_text()}, "
            f"state writable {facts.state_writable}, windows "
            f"{_windows_text(facts.windows)}, display windows "
            f"{_windows_text(facts.display_windows)}."
        )
        diagnostics.note(notice.log_line())
        if not human:
            diagnostics.note(
                "menubar: started by the LaunchAgent, so this is routine — no "
                "notice shown and the holder was not signalled."
            )
            return Report(None)
        return Report(
            notice=notice,
            # 🔴 **`None` is "could not look", not "nothing there" — and
            # the same degradation that is BENIGN for the words is
            # HARMFUL for the geometry.**
            #
            # `visible_status_items` documents its `None` case as benign,
            # and for 4a's copy it is: a missed exclusion says "the icon
            # is there, click it", which is the reading that does not
            # accuse the app. 4b reuses the same list to decide **where to
            # put a window**, and there it inverts. The icon that was not
            # excluded is the one parked on the picture display, so the
            # panel anchors to it and draws on a 2.1-inch round screen,
            # under the slideshow, half of it outside the circular mask —
            # while the person who double-clicked sees nothing at all.
            #
            # Worse, `avoid` collapses to `()` at the same instant, so the
            # screen-level exclusion is lost on both sides together. The
            # trigger needs no window-server failure: `verified_display_pid`
            # answers None whenever the display lock is unreadable, names a
            # malformed pid, is denied by `proc_pidpath`, or fails
            # `is_ours` on a mixed source-tree/installed pair — while the
            # display agent is drawing perfectly.
            #
            # So when the display agent's windows could not be listed,
            # this offers **no anchor at all**. The cost is a centred panel
            # with no pointer in a rare case; the alternative is a notice
            # nobody can read carrying a claim that is not true.
            icons=(
                cstate.visible_status_items(facts.windows, facts.display_windows)
                if facts.display_windows is not None
                else ()
            ),
            avoid=tuple(w for w in (facts.display_windows or ()) if w.on_screen),
        )
    except Exception:  # noqa: BLE001 - a diagnostic must never become the fault
        try:
            print(
                f"contention: could not report on the lock holder:\n"
                f"{traceback.format_exc()}",
                file=sys.stderr,
            )
        except Exception:  # noqa: BLE001 - nothing left to do
            pass
        return Report(None)


def report(
    lock_path: Path,
    display_lock_path: Path,
    status_path: Path,
    state_dir: Path,
    agent_label: str,
    environ: Mapping[str, str] | None = None,
    now: float | None = None,
) -> cstate.Notice | None:
    """`observe()`'s verdict on its own. **Never raises.**

    Kept as its own entry point because it is the narrower contract — a
    caller that only wants to know what was found should not have to
    receive window geometry it has no use for, and must not start
    depending on it.
    """
    return observe(
        lock_path,
        display_lock_path,
        status_path,
        state_dir,
        agent_label,
        environ=environ,
        now=now,
    ).notice


def _windows_text(windows: tuple[cstate.WindowFact, ...] | None) -> str:
    """"could not look" and "nothing there" must not print the same."""
    if windows is None:
        return "could not be listed"
    if not windows:
        return "none"
    return ", ".join(f"layer {w.layer} {w.where()}" for w in windows)


__all__ = [
    "PROC_PIDPATHINFO_MAXSIZE",
    "STACK_CAPTURE_POLL_S",
    "STACK_CAPTURE_WAIT_S",
    "Report",
    "capture_stacks",
    "gather",
    "observe",
    "own_exe",
    "proc_start_time",
    "probe_writable",
    "proc_pidpath",
    "report",
    "verified_display_pid",
    "window_facts",
]
