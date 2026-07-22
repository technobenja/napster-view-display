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
"""

from __future__ import annotations

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
        text = path.read_text().strip()
    except OSError:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def acquire(path: Path, description: str = "display process") -> IO[str] | None:
    """Take the exclusive, non-blocking lock on `path`.

    `description` names the role in the contention message only. It
    exists because this module now guards two different things — the
    display agent on `display.lock` and the menu bar on `ui.lock` (
    plus Step 5's UI LaunchAgent) — and a log line reading "another
    display process" when what actually happened was a second menu bar
    would send the next reader looking at the wrong process entirely.

    Returns the open file object on success (also retained internally),
    or None if another process holds it — in which case the caller should
    exit **0**.

    A lock file that cannot be opened at all (unwritable home, read-only
    volume) is treated as "no contention" and returns a sentinel-free
    success: refusing to start the display because a *lock file* could
    not be created would turn a cosmetic problem into a total outage,
    which inverts this project's standing failure philosophy (degrade toward "keep showing the last good frame", never toward not
    running at all). This is logged loudly.
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
        return None

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
    return handle


def _no_guard() -> IO[str]:
    """Sentinel for "could not lock, proceeding anyway".

    Returning a real (unlocked) object rather than None keeps the
    caller's contract simple: None means, and only means, "another
    instance is running — exit 0". Uses os.devnull so nothing downstream
    can write through it by accident.
    """
    global _held
    handle = open(os.devnull, "a+")
    _held = handle
    return handle


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
