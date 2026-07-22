"""Log rotation — "simple rotation (e.g. truncate or rotate past
a fixed size, checked at each RunAtLoad)."

`launchd` itself does not rotate `StandardOutPath`/`StandardErrorPath` —
it just opens them (append mode, matching normal shell-redirect
semantics) and leaves growth entirely up to whatever's writing. This app
runs unattended indefinitely on a machine that's also the household's
primary AI orchestration node, so an unbounded log over months of uptime
is a real disk-hygiene problem, not a hypothetical one.

This module is called once, at process startup, before any real logging
happens (see `app.py`'s `main()`) — matching the granularity the run loop
actually asks for ("checked at each RunAtLoad"), not a background watcher
or a check on every write.

Strategy: **rename-and-truncate, not truncate-in-place.** A truncate at
the exact moment a service is restarting would destroy the very
tail-of-log content most useful for diagnosing *why* it restarted (crash,
`KeepAlive` respawn, etc.) — throwing that away for a slightly smaller
disk footprint isn't worth it on a machine with plenty of free disk.
Rename-and-truncate keeps exactly one prior generation
(`path` + `.old`, overwritten each rotation) at the cost of up to 2x
`max_bytes` of steady-state disk use per log file — an explicit, bounded
tradeoff, not unbounded growth.
"""

from __future__ import annotations

import sys
from pathlib import Path

# 10 MB per log file — generous enough that normal operation (a handful
# of print() lines per poll/rotation tick, every 15-30 min) would take
# months to reach it, but bounded so an unexpected print-spam bug (e.g. a
# tight exception-retry loop) can't fill the disk unattended.
MAX_LOG_BYTES = 10 * 1024 * 1024


def rotate_if_oversized(path: Path, max_bytes: int = MAX_LOG_BYTES) -> None:
    """Rotate `path` if it exceeds `max_bytes`, else leave it alone.

    Rotation renames the oversized file to `path` with `.old` appended
    (replacing any previous `.old` generation) and leaves nothing at
    `path` itself — the next write (launchd reopening the path for the
    freshly-(re)started process) recreates it, same as a normal
    shell-redirect target that doesn't exist yet.

    Never raises: a missing file is a no-op (nothing to rotate on first
    run, or when run interactively where launchd isn't redirecting
    stdout/stderr here at all), and any OSError during stat/rename is
    logged to stderr and swallowed — a logging-hygiene failure must never
    block app startup.
    """
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return
    except OSError as exc:
        print(
            f"log_rotation: cannot stat {path} ({exc}); leaving as-is.",
            file=sys.stderr,
        )
        return

    if size <= max_bytes:
        return

    old_path = path.with_name(path.name + ".old")
    try:
        path.replace(old_path)
    except OSError as exc:
        print(
            f"log_rotation: failed to rotate {path} ({exc}); leaving as-is.",
            file=sys.stderr,
        )
