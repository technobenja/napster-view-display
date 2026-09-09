"""What this process leaves behind when nobody is watching it.

Three mechanisms, one concern: a process that has stopped working must
still be diagnosable, on a machine nobody was sitting in front of.

**Why this module exists.** A menu bar process was once observed wedged
while holding `ui.lock`, so every subsequent double-click hit the
single-instance guard and exited 0 in silence — "the app will not
launch". The wedge left **no trace anywhere on the machine**: no hang
report in `~/Library/Logs/DiagnosticReports`, nothing in
`ui.stderr.log`, and `log show --predicate 'process == "ImageView"'`
returned nothing at all — not even from the *display* agent, which had
demonstrably written hundreds of lines over the same period. This app is
invisible to the unified log; it writes to stderr and nowhere else, and
`StandardErrorPath` exists only in the LaunchAgent plists. The shipped
app installs an agent for the display only (`ui_agent.install()` is
reachable from that module's CLI and from nothing else), so until this
module existed the menu bar on every real user's machine was **mute by
construction**. A Force
Quit then destroyed the live evidence, and that root cause is
permanently undetermined.

`arm_stack_dumps()` answers *where is it stuck*.
`faulthandler.register` is implemented in C and dumps every thread's
Python stack **while the GIL is held**, so it works on a process that is
deadlocked and cannot execute a single line of Python. No
`signal.signal` handler can do that: Python-level handlers run only when
the interpreter next reaches bytecode dispatch, which is exactly what a
wedged run loop never does (`SIGNAL_RESPONSIVENESS_INTERVAL_S` in
`display/app.py` is the same fact seen from the other side — a 0.25s
no-op timer exists purely to keep reaching that dispatch point).
`kill -USR1 <pid>` would have answered the question above in one command.

It answers **only** that question. `faulthandler.enable()` — which would
also catch a hard crash, SIGSEGV or the SIGABRT from an uncaught
Objective-C exception — is deliberately **not** called here, and the
choice is recorded rather than left unasked. `enable()` installs handlers
for signals AppKit and PyObjC use as part of normal operation on this
platform, and a diagnostic that can change how a crash is delivered is a
diagnostic that can alter the behaviour it exists to observe; this phase
is explicitly behaviour-preserving. The gap is real: a hard crash still
leaves nothing, and closing it is worth doing on its own evidence, with
its own testing, rather than as a free rider here.

`redirect_stderr_to_log()` answers *and where does it say so*: it hands
a Finder-launched process the log file the LaunchAgent case gets for
free.

`note()` is a timestamped line. The logs are append-only across every
launch, so an untimestamped line cannot be attributed to the launch that
wrote it.

**Two rules this module keeps, both paid for elsewhere in this project:**

1. **Nothing here may raise.** Every function swallows its own failures
   and reports by return value, matching the standing convention in
   `paths.py`, `calibration.py` and `log_rotation.py` that defensive
   reads and writes never raise. Diagnostics that can kill the process
   they exist to explain are worse than none: the "DO NOT ADD WORK TO THIS
   TIMER" comment on `SIGNAL_RESPONSIVENESS_INTERVAL_S` records what that
   cost last time — a heartbeat that could raise took
   away the ability to *stop* the service.

2. **Rotate a file only from the process that has proven it is the sole
   writer.** Rotation is rename-and-recreate, so a second process that
   rotates a log a first process already has open does not truncate it —
   it moves it out from under the writer's file descriptor, silently,
   for the rest of that process's life. `arm_stack_dumps()` is therefore
   called only *after* `single_instance.acquire()` has returned a
   handle, and `redirect_stderr_to_log()` does not rotate at all: its
   whole reason for existing is the double-click-while-already-running
   case, where the calling process is by definition the one that is
   about to lose. See `redirect_stderr_to_log()` for why that is the
   right trade here.

   **"After acquire() returned a handle" is weaker than "this process
   holds the lock", and the gap is real.** `single_instance._no_guard()`
   returns an unlocked handle when the lock file cannot be created at
   all (an unwritable home, a read-only volume), which the call sites
   cannot distinguish from success — `is None` is the whole contract.
   Under that sentinel two processes can both proceed to arm, so the
   sole-writer property degrades exactly when the guard does. That is
   accepted, not overlooked: the sentinel's behaviour is deliberate
   (`single_instance.acquire`'s docstring gives the reasoning) and
   telling the two states apart needs a wider return type than this
   phase is entitled to change. The cost of hitting it is one rotated
   log, on a machine that already cannot write its own lock file.

**Known gaps, recorded so they are not rediscovered as surprises:**

- `<role>.stderr.log` has no rotator, and `redirect_stderr_to_log()`
  adds a writer to it. Growth is human-paced — a few lines per launch —
  but it is unbounded, and the reason nothing rotates it is the
  fd-under-launchd hazard above rather than a decision that it should
  grow forever.
- `role` reaches a filesystem path with no validation. Latent only:
  every caller passes one of the two module constants in `paths`, and
  `ui_agent.build_plist` takes `.name` off the result, which would
  neuter a traversal. It would stop being latent the moment a role came
  from anywhere but a constant.
"""

from __future__ import annotations

import faulthandler
import os
import signal
import stat
import sys
import time
from pathlib import Path
from typing import IO

from display import paths
from display.log_rotation import rotate_if_oversized

#: The signal `arm_stack_dumps()` claims. Free in this app: the only
#: other handlers are SIGINT/SIGTERM (`smoke_test.install_signal_handlers`).
#: USR1 rather than USR2 by convention, and rather than SIGQUIT because
#: SIGQUIT's default action would kill the process we are trying to read.
STACK_DUMP_SIGNAL = signal.SIGUSR1

# Both handles are held for the process lifetime, in the shape of
# `single_instance._held` and for a related reason — but the faulthandler
# half of that reason turned out **not** to be what the plan assumed, and
# the correction is worth keeping.
#
# The assumption was that `faulthandler` stores only the file
# *descriptor*, so a collected file object would close the fd under it
# and the dump would go somewhere silently wrong. MEASURED on this
# project's pinned interpreter, CPython 3.13.12: `register(file=...)`
# takes a strong reference to the file object and drops it on
# `unregister()` (the fd survives a `gc.collect()` after the only Python
# reference goes out of scope, and closes the moment the signal is
# unregistered). So on this build `_dump_file` is not load-bearing, and
# no test can prove it is — a mutation removing it stays green.
#
# It stays anyway, for two reasons that do not depend on that detail.
# The documented contract is still "the file must be kept open until the
# fault handler is unregistered", and depending on an undocumented
# implementation detail of the interpreter is the kind of thing that
# breaks on an upgrade nobody connected to it. And `disarm_stack_dumps()`
# needs a reference to close deterministically rather than at the
# collector's convenience.
#
# `_stderr_file` is genuinely load-bearing: `sys.stderr` is reassignable
# by anything, and this must not be the sole reference to the log.
_dump_file: IO[str] | None = None
_stderr_file: IO[str] | None = None


#: `O_NOFOLLOW` refuses to open through a symlink; `0o600` keeps the mode
#: off the default 0o644 these files would otherwise get. Neither is a
#: privilege boundary here — `~/Library/Logs/` is 0700 and everything in
#: play runs as one uid — but these are brand-new files and the correct
#: defaults are one line. `O_NOFOLLOW` fails with ELOOP rather than
#: writing somewhere a symlink points, which is the behaviour worth
#: having if this ever runs anywhere less private.
_LOG_OPEN_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW
_LOG_MODE = 0o600


def _open_log(path: Path) -> IO[str] | None:
    """Open `path` for appending, line-buffered, 0600. None on any failure.

    `buffering=1` is load-bearing rather than a default worth accepting —
    **for the stderr log**. The process these files exist to explain is
    one that has stopped running Python, so it will never reach a flush
    or an exit, and a block-buffered handle would hold back the last few
    KB, which is the part that says what it was doing. It buys the stack
    dump nothing: `faulthandler` writes to the raw descriptor and never
    passes through this object's buffer at all. Sharing one opener is
    still right — the mode, the flags and the failure handling are the
    same question for both files — but the buffering argument is not.

    `errors="backslashreplace"` so that an encoding error cannot be
    raised out of a log write, which would be this module failing at the
    one thing it must never do.
    """
    try:
        fd = os.open(path, _LOG_OPEN_FLAGS, _LOG_MODE)
    except OSError as exc:
        print(f"diagnostics: cannot open {path} ({exc}).", file=sys.stderr)
        return None
    try:
        return os.fdopen(fd, "a", buffering=1, encoding="utf-8", errors="backslashreplace")
    except OSError as exc:
        # The descriptor is live and now unreachable, so close it here
        # rather than leaking one per failed launch.
        os.close(fd)
        print(f"diagnostics: cannot open {path} ({exc}).", file=sys.stderr)
        return None


def _is_regular_file(stream: object) -> bool:
    """True when `stream` is backed by a regular file on disk.

    This is the launchd test, and it is deliberately made by `fstat` and
    not by comparing paths. launchd opens `StandardErrorPath` *before*
    exec, so in an agent-started process fd 2 already **is** the log
    file; a Finder launch gives the process `/dev/null` or a pipe, and a
    Terminal gives it a tty. Matching on the *name* instead would break
    the moment a user redirected the app's stderr somewhere else by hand,
    and would silently stop working if launchd's path and ours ever
    drifted apart — whereas "someone already pointed this at a file" is
    the actual condition we care about.

    Anything that cannot answer — a `StringIO` under test, a `None`
    stderr under `pythonw`, a closed stream — reports False, i.e. "safe
    to redirect", because that is the state a Finder launch is in and
    getting it wrong in that direction costs a log file rather than a
    duplicated writer.
    """
    try:
        fd = stream.fileno()  # type: ignore[attr-defined]
    except (AttributeError, OSError, ValueError):
        return False
    try:
        return stat.S_ISREG(os.fstat(fd).st_mode)
    except OSError:
        return False


def redirect_stderr_to_log(role: str) -> Path | None:
    """Point `sys.stderr` at `~/Library/Logs/ImageView/<role>.stderr.log`.

    Returns the path if this call took ownership of stderr, or None if it
    deliberately did nothing.

    **The LaunchAgent case is left completely alone.** When the plist's
    `StandardErrorPath` already points at this exact file, launchd is the
    writer; redirecting on top of it would put two independent file
    descriptions on one file, which is precisely what this project's
    one-writer-per-file contract forbids. `_is_regular_file()` is how
    that case is detected. It also covers `menubar.py 2>somewhere`, where
    the operator has already said where output goes and this module has
    no business overriding them.

    **This function does not rotate.** Every other log in this app is
    rotated at startup (`log_rotation.py`), and the omission here is
    considered, not forgotten. Rotation renames; it does not truncate. The
    case this redirect exists for is a double-click while the app is
    already running, where the calling process is *by definition* the one
    about to lose the single-instance race — and a rename issued by that
    loser would move the surviving instance's log out from under an fd
    launchd (or the winner) is still writing to, leaving the live log in
    `.old` and a one-line file at the real path. The growth rotation was
    written for is the display agent's per-poll output over months; the
    menu bar writes a handful of lines per launch, so bounding it is not
    worth that.

    Only `sys.stderr` is replaced — fd 2 itself is untouched. An
    in-process replacement is reversible and cannot disturb a descriptor
    launchd owns, and every diagnostic this app emits goes through
    `print(..., file=sys.stderr)`.

    **So this log is not everything the process said.** Anything that
    writes to descriptor 2 without going through `sys.stderr` still goes
    wherever the launch put it, which on a Finder launch is nowhere:
    output from subprocesses, C-level aborts and the interpreter's own
    fatal-error text, and PyObjC/AppKit's logging (which goes to the
    unified log in any case, where this app does not appear). An empty or
    short `<role>.stderr.log` therefore means "the Python level said
    nothing", never "the process said nothing".
    """
    global _stderr_file

    if _is_regular_file(sys.stderr):
        return None

    path = paths.stderr_log_path(role)
    if not paths.ensure_dir(path.parent):
        return None

    handle = _open_log(path)
    if handle is None:
        return None

    _stderr_file = handle
    sys.stderr = handle
    return path


def release_stderr() -> None:
    """Undo `redirect_stderr_to_log()`. Only tests need this — a real
    process keeps the redirect until exit, when the kernel cleans up.

    Mirrors `single_instance.release()`, and exists for the same reason
    that one does: without it a test leaves `sys.stderr` pointing into a
    temporary directory that is about to be deleted, and every later
    write in the process fails.
    """
    global _stderr_file
    if _stderr_file is None:
        return
    if sys.stderr is _stderr_file:
        sys.stderr = sys.__stderr__
    try:
        _stderr_file.close()
    except OSError:
        pass
    _stderr_file = None


def arm_stack_dumps(role: str) -> Path | None:
    """Make `kill -USR1 <pid>` dump every thread's Python stack to
    `~/Library/Logs/ImageView/<role>.stacks.log`.

    Returns the path on success, None if it could not be armed.

    A file of its own, not the stderr log, for the one-writer reason
    again: in the LaunchAgent case launchd owns `<role>.stderr.log` and
    faulthandler writes straight to a descriptor of its own, so pointing
    it there would interleave two independent writers on one file. A
    dedicated file also keeps a multi-KB stack dump from burying the
    surrounding log lines.

    **Call this only after the single-instance lock is won.** It rotates
    the file, and rotation from a losing instance would move a running
    instance's dumps out from under it — see this module's docstring.
    """
    global _dump_file

    # Documented as unavailable on Windows. This app is macOS-only, but a
    # missing attribute here must degrade to "no stack dumps", never to
    # an AttributeError at process entry.
    if not hasattr(faulthandler, "register"):
        return None

    path = paths.stacks_log_path(role)
    if not paths.ensure_dir(path.parent):
        return None

    rotate_if_oversized(path)

    handle = _open_log(path)
    if handle is None:
        return None

    try:
        faulthandler.register(STACK_DUMP_SIGNAL, file=handle, all_threads=True)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"diagnostics: cannot arm stack dumps ({exc}).", file=sys.stderr)
        try:
            handle.close()
        except OSError:
            pass
        return None

    _dump_file = handle
    return path


def disarm_stack_dumps() -> None:
    """Unregister the handler and drop the file. Only tests need this —
    a real process holds both until exit, when the kernel cleans up.
    Mirrors `single_instance.release()`, and exists for the same reason:
    without it a test leaves a live handler pointed at a temporary
    directory the next test has already deleted.
    """
    global _dump_file
    try:
        faulthandler.unregister(STACK_DUMP_SIGNAL)
    except (OSError, ValueError, RuntimeError):
        pass
    if _dump_file is not None:
        try:
            _dump_file.close()
        except OSError:
            pass
        _dump_file = None


def note(message: str) -> None:
    """Write one timestamped line to `sys.stderr`. Never raises.

    Timestamped because these logs are append-only across every launch of
    the app: an undated "another instance holds the lock" line cannot be
    attributed to the launch that produced it, which is the only question
    anyone will be asking when they read it.

    The broad `except` is deliberate and is the point of the function.
    Its most important caller is the contention path in `menubar.main()`,
    which must `return 0`: an exception there would turn a correct silent
    exit into a traceback and a **non-zero** exit code, and
    `KeepAlive {SuccessfulExit: false}` respawns exactly those — the
    tight relaunch loop `single_instance.py` was written to prevent. No
    failure of this function is worth more than the line it drops.

    Callers must keep their own formatting total, since an f-string is
    evaluated before the call and cannot be protected from in here.
    """
    try:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", file=sys.stderr, flush=True)
    except Exception:
        pass
