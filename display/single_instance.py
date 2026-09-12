"""Single-instance guard via `flock`.

Why this exists, structurally rather than procedurally: the live
service may use a label from an earlier source-tree install; the packaged app introduces new
labels. Ordering the install steps carefully does not survive contact —
`bootout` does not remove the plist (so the old agent re-bootstraps at
next login and two processes fight for the screen a reboot later, long
after the connection to the install has been lost), `bootout` exits
non-zero when the label is not loaded (so a `set -e` installer aborts on
the common path), there are two old labels, and a developer may keep a git checkout
whose `display/launchd/README.md` documents the old install commands.

So the display process takes an exclusive `flock` on
`~/.viewlab/display.lock` at startup; if it is already held, it logs the
holder's pid and **exits 0**. The exit code is load-bearing: the plist's
`KeepAlive {SuccessfulExit: false}` means a clean exit is not respawned,
so the loser stays down instead of respawn-looping. Exiting non-zero
here would produce exactly the tight relaunch loop this is meant to
prevent.

This also makes drag-to-upgrade safe: Finder's "replace" is
delete-then-move, and a second copy launched over a running one cannot
get past this.

The returned handle must be **held for the life of the process** — the
lock is released when the file descriptor closes, and letting it be
garbage-collected would silently drop the lock. `acquire()` keeps its own
module-level reference for exactly that reason, so callers cannot get it
wrong by accident.

**`acquire()` reports three outcomes, not two, and the third is the one
this module used to hide.** `Acquired` and `Contended` were always
distinguishable; `Unguarded` — the lock file could not be created at all,
so this process is running with no guard — was returned as an unlocked
handle indistinguishable from success, because `is None` was the whole
contract. `_no_guard()`'s *behaviour* is unchanged and deliberately so
(refusing to start because a *lock file* could not be created still
inverts this project's failure philosophy). What changed is that callers
can no longer be unable to tell, which buys two things that were
previously impossible to write: a log line naming the unguarded state
plainly, and a `diagnostics.arm_stack_dumps(..., sole_writer=False)` that
declines to rotate a log this process has not proven it owns.

🔴 **`acquire()` never returns `None` any more.** The old idiom
`if acquire(...) is None:` is now silently false, which would let a
second instance proceed past the guard — the exact failure the guard
exists to prevent. Every call site reads `.contended` / `.acquired` /
`.unguarded`, and `AcquisitionTests` pins that `None` is never returned.
"""

from __future__ import annotations

import dataclasses
import enum
import fcntl
import os
import sys
from pathlib import Path
from typing import IO

# Holds the locked file object for the process lifetime. Without this,
# the only reference would be the caller's, and a caller that dropped it
# (or a bare `acquire(path)` call whose result is discarded) would
# release the lock at the next garbage collection — the failure would be
# invisible until two displays fought for the screen.
_held: IO[str] | None = None


def read_holder_pid(path: Path) -> int | None:
    """Best-effort read of the pid recorded in the lock file, for log
    output only. Returns None if unreadable or not a plain integer —
    never raises, and never used for anything but a message."""
    try:
        # `errors="replace"` because `UnicodeDecodeError` is a
        # `ValueError`, not an `OSError` — so a lock file with one bad
        # byte raised straight through this handler, out of a function
        # whose docstring says it never raises, and onto the launch path
        # that now reads it for more than a message.
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    try:
        return int(text)
    except ValueError:
        return None


class Outcome(enum.Enum):
    """What `acquire()` actually did. Three values, because there are
    three things that can happen and the third used to be invisible."""

    #: The exclusive lock is held by this process for its lifetime.
    ACQUIRED = "acquired"
    #: Another process holds it. The caller exits **0** — see the module
    #: docstring for why the exit code is load-bearing.
    CONTENDED = "contended"
    #: The lock file could not be created or opened, so nothing is
    #: guarding anything. The caller proceeds anyway, deliberately.
    UNGUARDED = "unguarded"


@dataclasses.dataclass(frozen=True)
class Acquisition:
    """`acquire()`'s answer.

    Deliberately **no `__bool__`**. An implicit truth value on a
    three-state result is precisely the ambiguity this type exists to
    remove: `if guard:` would have to pick one of two readings for
    `UNGUARDED` and would be wrong for half its callers either way. Ask
    the question you mean.

    `handle` is the open, locked file object for `ACQUIRED`, the
    `os.devnull` sentinel for `UNGUARDED`, and `None` for `CONTENDED`. It
    is retained module-side regardless; callers should not need it, and
    it is exposed only so that a caller can hold a second reference if it
    ever wants to.
    """

    outcome: Outcome
    handle: IO[str] | None = None

    @property
    def acquired(self) -> bool:
        return self.outcome is Outcome.ACQUIRED

    @property
    def contended(self) -> bool:
        return self.outcome is Outcome.CONTENDED

    @property
    def unguarded(self) -> bool:
        return self.outcome is Outcome.UNGUARDED


def acquire(path: Path, description: str = "display process") -> Acquisition:
    """Take the exclusive, non-blocking lock on `path`.

    `description` names the role in the contention message only. It
    exists because this module now guards two different things — the
    display agent on `display.lock` and the menu bar on `ui.lock` (
    plus Step 5's UI LaunchAgent) — and a log line reading "another
    display process" when what actually happened was a second menu bar
    would send the next reader looking at the wrong process entirely.

    Returns an `Acquisition`, **never `None`**. On `CONTENDED` the caller
    should exit **0**.

    A lock file that cannot be opened at all (unwritable home, read-only
    volume) is `UNGUARDED`, not a refusal to start: declining to run the
    display because a *lock file* could not be created would turn a
    cosmetic problem into a total outage, which inverts this project's
    standing failure philosophy (degrade toward "keep showing the last
    good frame", never toward not running at all). That behaviour is
    unchanged — only its *visibility* to the caller is new. It is still
    logged loudly here as well.
    """
    global _held

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(
            f"single_instance.py: cannot create {path.parent} ({exc}); "
            f"starting WITHOUT the single-instance guard.",
            file=sys.stderr,
        )
        return _no_guard()

    try:
        handle = open(path, "a+")
    except OSError as exc:
        print(
            f"single_instance.py: cannot open {path} ({exc}); starting "
            f"WITHOUT the single-instance guard.",
            file=sys.stderr,
        )
        return _no_guard()

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        holder = read_holder_pid(path)
        holder_text = f"pid {holder}" if holder is not None else "an unknown pid"
        print(
            f"single_instance.py: another {description} ({holder_text}) "
            f"already holds {path}. Exiting cleanly — a clean exit is not "
            f"respawned by KeepAlive{{SuccessfulExit: false}}, so this "
            f"instance stays down rather than fighting for the screen.",
            file=sys.stderr,
        )
        handle.close()
        return Acquisition(Outcome.CONTENDED)

    try:
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
    except OSError as exc:
        # The lock itself is held regardless of whether the pid landed —
        # the pid is only ever used for a log message.
        print(
            f"single_instance.py: holding {path} but could not record the "
            f"pid in it ({exc}).",
            file=sys.stderr,
        )

    _held = handle
    return Acquisition(Outcome.ACQUIRED, handle)


def _no_guard() -> Acquisition:
    """Sentinel for "could not lock, proceeding anyway".

    **Behaviour preserved where it matters, and improved in one place.**
    This still opens `os.devnull`, still retains it module-side, and
    still lets the caller proceed; the label on the way out is new. Uses
    os.devnull so nothing downstream can write through it by accident.

    The one change is the `try` around that open, and calling it
    "preserved exactly" would have been wrong: previously an unopenable
    `/dev/null` raised out of `acquire()`, which means a traceback, a
    **non-zero exit**, and the respawn loop `KeepAlive
    {SuccessfulExit: false}` turns that into — from the function whose
    whole job is to prevent exactly that. At that point the machine has
    bigger problems than this app, and the answer is still "run anyway",
    now with no handle at all.
    """
    global _held
    try:
        handle: IO[str] | None = open(os.devnull, "a+")
    except OSError:
        handle = None
    _held = handle
    return Acquisition(Outcome.UNGUARDED, handle)


def release() -> None:
    """Drop the lock. Only needed by tests — a real process holds it
    until exit, when the kernel releases it."""
    global _held
    if _held is not None:
        try:
            fcntl.flock(_held.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            _held.close()
        except OSError:
            pass
        _held = None
