"""Log rotation — "simple rotation (e.g. truncate or rotate past
a fixed size, checked at each RunAtLoad)."

`launchd` itself does not rotate `StandardOutPath`/`StandardErrorPath` —
it opens them and leaves growth entirely up to whatever's writing. This
app runs unattended indefinitely, often for months at a stretch, so an
unbounded log is a real disk-hygiene problem rather than a hypothetical
one.

This module is called once, at process startup, before any real logging
happens (see `app.py`'s `main()`) — matching the granularity the run loop
actually asks for ("checked at each RunAtLoad"), not a background watcher
or a check on every write.

Strategy: **copy-then-truncate-in-place.**

The obvious implementation is rename-then-let-the-next-write-recreate,
and that is what this module did until it was found to be broken.
**launchd opens `StandardErrorPath` and dups it onto fd 2 *before*
exec'ing the process**, so by the time this module runs, fd 2 already
*is* the log file. A rename does not follow a descriptor: it moves the
inode launchd is holding, so everything the process goes on to write
lands in `.old` while the path every doc, runbook and support answer
names sits empty — silently, for the whole life of that process, and
again on every subsequent start. Measured on a live install before
v1.1.3:
`display.stderr.log` at 12,181,049 bytes against a 10 MB cap with no
`.old` beside it, because a human had been moving it aside by hand as a
deployment step. Moving the call below the single-instance guard does
**not** fix this; the descriptor is inherited whether or not anyone is
contending for the lock.

Truncating **in place** is what a descriptor can survive: the inode is
preserved, so launchd's fd stays valid and stays pointed at the file
everyone reads.

**Why copy-truncate's usual race does not bite here.** The standard
objection to copy-truncate is that anything written between the copy and
the truncate is lost. This module runs at process startup, before this
process has written a byte, and this app's one-writer-per-file rule
(see `diagnostics.py`) means nothing else is writing to that path
either. The window exists; there is nothing in it.

**And the tail-of-log objection is answered by the copy, not by the
rename.** The content most useful for diagnosing *why* a service
restarted is the tail of the previous run, and it survives here exactly
as it did before — in `path` + `.old`, one prior generation, overwritten
each rotation, at the cost of up to 2x `max_bytes` of steady-state disk
use per log file (3x transiently, while the copy is in flight). An
explicit, bounded tradeoff, not unbounded growth.

**Measured, not assumed:** launchd opens both `StandardOutPath` and
`StandardErrorPath` with `O_APPEND` — verified on macOS 15 (Darwin
24.6.0) with a throwaway LaunchAgent, two ways: `fcntl(F_GETFL)` on the
inherited fd returned `0o12` (`O_RDWR | O_APPEND`) for fd 1 and fd 2,
and behaviourally, a write issued after an *external* truncate landed at
offset 0 (31-byte file, 8 disk blocks, marker text as the first bytes)
rather than at the descriptor's stale ~1 MiB offset. That is the fact
this whole strategy rests on: without `O_APPEND` a truncate would leave
the inherited descriptor's offset where it was and the next write would
punch megabytes of NUL bytes into the file — strictly worse than the bug
being fixed. It is written down here because this module's docstring
previously *asserted* append mode with nothing behind it, which is the
kind of claim this project has been burned by. Re-measure before
trusting it on a future OS.

**And the measurement is not the only thing holding it up.**
`_inherited_fd_appends()` re-checks the property at runtime, on every
rotation, against the descriptors this process actually holds: if fd 1 or
fd 2 is this very file (matched by `(st_dev, st_ino)` on a real `fstat`)
and is *not* open in append mode, rotation is skipped and says so. That
turns an unverifiable platform assumption into an observable one and
fails safe — an oversized-but-intact log beats a NUL hole. **No unit test
can substitute for it**, because a test opens its own descriptors and so
can only ever observe its own `open()` call, never launchd's.

**Rotation refuses to act through a symlink.** Both the read and the
truncate use `O_NOFOLLOW`. The old `path.replace()` moved the *link*
rather than its target, so it never followed one either; that property
was about to be given up silently, and `diagnostics._open_log()` states
the same posture for the files it opens.

**Nothing here raises, including the reporting.** Under launchd
`sys.stderr` *is* the log file, so on a full disk the report of a failed
copy is itself a failing write — see `_report()`.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

# 10 MB per log file — generous enough that normal operation (a handful
# of print() lines per poll/rotation tick, every 15-30 min) would take
# months to reach it, but bounded so an unexpected print-spam bug (e.g. a
# tight exception-retry loop) can't fill the disk unattended.
MAX_LOG_BYTES = 10 * 1024 * 1024


def rotate_if_oversized(path: Path, max_bytes: int = MAX_LOG_BYTES) -> None:
    """Rotate `path` if it exceeds `max_bytes`, else leave it alone.

    Rotation copies the oversized file's contents to `path` with `.old`
    appended (replacing any previous `.old` generation) and then
    truncates `path` to zero length **in place**. The file at `path`
    keeps its inode, so a descriptor another process already holds on it
    — in practice launchd's, dup'd onto fd 1/fd 2 before this process was
    exec'd — stays valid and keeps writing to the file everyone reads.

    Never raises: a missing file is a no-op (nothing to rotate on first
    run, or when run interactively where launchd isn't redirecting
    stdout/stderr here at all), and any OSError during stat/copy/truncate
    is reported to stderr and swallowed — a logging-hygiene failure must
    never block app startup.

    **Degradation is deliberately asymmetric.** If the copy fails — a
    full disk being the case that matters — the oversized file is left
    completely intact and untruncated, and any existing `.old` is left
    intact too. One over-cap log file is a disk-hygiene miss; truncating
    without a good copy would destroy the only record of what the app
    was doing, which is the thing the log exists for. Over-cap and
    readable beats within-cap and empty. If the *truncate* fails after a
    successful copy, the result is simply the pre-existing state plus a
    fresh `.old`: nothing is lost, and the next start tries again.
    """
    try:
        info = path.stat()
    except FileNotFoundError:
        return
    except OSError as exc:
        _report(f"log_rotation: cannot stat {path} ({exc}); leaving as-is.")
        return

    if info.st_size <= max_bytes:
        return

    if not _inherited_fd_appends(info):
        return

    old_path = path.with_name(path.name + ".old")
    if not _copy_to_old(path, old_path, stat.S_IMODE(info.st_mode)):
        return

    _truncate_in_place(path, old_path)


def _truncate_in_place(path: Path, old_path: Path) -> None:
    """Empty `path` without replacing it. Reports and returns on failure.

    Reached by NAME rather than through an inherited descriptor, because
    this function also serves paths that no descriptor in this process
    points at — an interactive run, and
    `diagnostics.arm_stack_dumps()`, which rotates before it opens.
    **That is a matter of reach, not of who holds what.** Under launchd
    fd 1 and fd 2 in *this* process ARE these files: `lsof` on the live
    display agent shows `ImageView` holding `1u` on `display.stdout.log`
    and `2u` on `display.stderr.log`. Those inherited descriptors are the
    entire reason this module was rewritten, and
    `_inherited_fd_appends()` is what checks the assumption they rest on.

    The guarantee is the same either way and does not depend on which
    handle does the work: truncation preserves the inode, so *every*
    holder's descriptor stays valid and stays pointed at this file.

    `O_NOFOLLOW` because rotation must not act through a symlink — see
    `_copy_to_old()`, which refuses the same way and for the same reason.
    **That flag is unreachable defence-in-depth, and is deliberately kept
    anyway.** `_copy_to_old()` runs first and refuses a symlinked `path`
    outright, so this call can never see one; mutation testing confirms
    it — dropping `O_NOFOLLOW` here is the one mutation in this module
    that survives the suite, because no reachable state exercises it. It
    stays because the ordering, not the flag, is what makes it
    unreachable, and the ordering is a thing a future change can alter
    without noticing this.
    """
    try:
        fd = os.open(path, os.O_WRONLY | os.O_NOFOLLOW)
    except OSError as exc:
        _report(
            f"log_rotation: copied {path} to {old_path} but could not "
            f"open it to truncate ({exc}); leaving it oversized."
        )
        return
    try:
        os.ftruncate(fd, 0)
    except OSError as exc:
        _report(
            f"log_rotation: copied {path} to {old_path} but could not "
            f"truncate it ({exc}); leaving it oversized."
        )
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _inherited_fd_appends(info: os.stat_result) -> bool:
    """True when it is safe to truncate the file `info` describes.

    **The runtime half of this module's central assumption.** Truncating
    a file another descriptor is open on is only safe if that descriptor
    appends: `truncate()` does not move a file offset, so a non-appending
    holder sitting at ~12 MB would write its next line at ~12 MB into a
    now-empty file and the kernel would fill the gap with NUL bytes. A
    log whose first megabytes are zeroes is strictly worse than the
    unbounded growth this module exists to stop.

    launchd was **measured** setting `O_APPEND` (see the module
    docstring), but a measurement is a fact about one OS on one day, and
    this project has been burned by exactly that kind of claim outliving
    its evidence. So the property is checked rather than trusted: for
    each of fd 1 and fd 2, if it refers to this very file — compared by
    `(st_dev, st_ino)` on a real `fstat`, the same test
    `diagnostics._is_regular_file()` makes, and never by name — its flags
    must carry `O_APPEND`. Descriptors pointing anywhere else are not our
    business and are skipped.

    This converts an unverifiable platform assumption into an observable
    one, and it fails safe: on False the caller leaves the log oversized
    and intact, which is a disk-hygiene miss rather than a corrupted log.

    A descriptor that matches but whose flags cannot be read is also
    False — "cannot confirm" is not "confirmed safe", and the cost of
    being wrong is asymmetric.

    Note this cannot see descriptors held by *other* processes. It
    covers the case the module was written for (launchd's, inherited by
    this process) and no other; the sole-writer rule in
    `diagnostics.py` is what covers the rest.
    """
    for fd in (1, 2):
        try:
            st = os.fstat(fd)
        except OSError:
            continue
        if (st.st_dev, st.st_ino) != (info.st_dev, info.st_ino):
            continue
        try:
            appends = bool(fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_APPEND)
        except OSError as exc:
            _report(
                f"log_rotation: cannot read the flags of fd {fd}, which is "
                f"this log ({exc}); skipping rotation rather than risk a "
                f"sparse log."
            )
            return False
        if not appends:
            _report(
                f"log_rotation: fd {fd} is this log and is NOT open in "
                f"append mode, so truncating it would leave a multi-megabyte "
                f"NUL hole; skipping rotation and leaving the log oversized. "
                f"This platform no longer matches the measurement in "
                f"log_rotation.py's docstring — re-measure before changing "
                f"this module."
            )
            return False
    return True


def _report(message: str) -> None:
    """Print to stderr, and swallow the failure if even that cannot be done.

    **Not belt and braces — the one case that matters is the one that
    breaks.** Under launchd `sys.stderr` IS the log file, so a report of
    "the copy failed because the disk is full" is itself a write to a
    full disk and raises `OSError(ENOSPC)`. `app.py`'s `main()` calls
    `rotate_if_oversized()` unguarded, and `KeepAlive
    {SuccessfulExit: false}` turns the resulting non-zero exit into a
    respawn loop — so the module's "never raises" contract has to hold
    here specifically, on the exact failure the docstring singles out as
    the one worth degrading well for.

    Under the old rename implementation this path was effectively
    unreachable, because a rename needs no data blocks. Copying does, so
    the fix moved this from "never fires" to "fires exactly when the disk
    is full". It is not the only such line — `main()`'s own first
    `print()` fails identically one statement later, and closing that is
    a separate change — but rotation must not be *a* cause.

    `ValueError` too: a closed or detached `sys.stderr` raises that
    rather than `OSError`, and covering it costs nothing.
    """
    try:
        print(message, file=sys.stderr)
    except (OSError, ValueError):
        pass


def _copy_to_old(path: Path, old_path: Path, mode: int) -> bool:
    """Copy `path` over `old_path`, atomically. True if `old_path` now
    holds a complete copy; False (having reported to stderr) if not.

    Temp-file-then-`os.replace()`, the same dance as `atomic_io.py` and
    for the same reason: a partial copy written directly onto `old_path`
    would leave the single retained generation as a truncated prefix,
    where the old rename-based implementation guaranteed `.old` was
    always a *complete* generation. Preserving that guarantee is worth
    one temp file. It is created in `old_path`'s own parent so the
    `os.replace()` is a same-filesystem rename and therefore atomic.

    **`dir=old_path.parent` is load-bearing, not tidiness.**
    `os.replace()` is atomic only when source and destination share a
    filesystem. A temp file in `TMPDIR` would be on a different volume
    often enough to matter and the rename would degrade to a copy, which
    is the exact non-atomicity this function exists to avoid. Under test
    the two happen to be the same volume, so only
    `test_the_temp_file_is_created_beside_its_destination` holds this in
    place — it spies on the kwarg, because no assertion about the
    resulting files can tell the two apart.

    `mode` is the source log's permission bits, copied onto the temp file
    because `mkstemp` creates 0600 and silently tightening `.old` beyond
    what the log itself allows is a change nobody asked for (launchd's
    logs land 0644; `diagnostics._LOG_MODE` files are 0600). They arrive
    as `stat.S_IMODE`, i.e. all twelve bits — setuid/setgid/sticky would
    be carried across too. Meaningless on a log file, and preserving
    exactly what was there beats silently dropping bits.

    `O_NOFOLLOW` on the source, via `open()`'s `opener`. The old
    `path.replace()` moved the *link* and so never acted through one;
    plain `open()` would follow it, quietly reversing a defensive
    property nobody decided to give up. It also removes a real
    inconsistency: `diagnostics.arm_stack_dumps()` rotates and then calls
    `_open_log()`, which uses `O_NOFOLLOW` — so a symlinked stacks log
    used to be rotated through and *then* refused. Now both refuse, and
    the refusal is reported.
    """
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=old_path.parent, prefix=".tmp-", suffix=".old"
        )
    except OSError as exc:
        _report(
            f"log_rotation: cannot create a temp file beside {old_path} "
            f"({exc}); leaving {path} as-is."
        )
        return False

    try:
        dst = os.fdopen(fd, "wb")
    except OSError as exc:
        # The descriptor is live and now unreachable, so close it here —
        # the same hand-off `diagnostics._open_log()` makes, for the same
        # reason. `with os.fdopen(fd, ...)` would leak it on this path.
        try:
            os.close(fd)
        except OSError:
            pass
        _report(
            f"log_rotation: cannot open a temp file beside {old_path} "
            f"({exc}); leaving {path} as-is."
        )
        _discard(tmp_name)
        return False

    try:
        with dst:
            os.fchmod(dst.fileno(), mode)
            with open(path, "rb", opener=_nofollow_opener) as src:
                shutil.copyfileobj(src, dst)
        os.replace(tmp_name, old_path)
    except OSError as exc:
        _report(
            f"log_rotation: failed to copy {path} to {old_path} ({exc}); "
            f"leaving it as-is."
        )
        _discard(tmp_name)
        return False

    return True


def _nofollow_opener(path: str, flags: int) -> int:
    """`open()` opener that refuses symlinks. See `_copy_to_old()`.

    Used rather than a bare `os.open()` + `os.fdopen()` pair because
    `open()` owns the descriptor from the moment the opener returns it,
    so there is no window in which a raising wrapper leaks it.
    """
    return os.open(path, flags | os.O_NOFOLLOW)


def _discard(tmp_name: str) -> None:
    """Remove a temp file that will never become `.old`. Never raises.

    A temp file that outlives its rotation is invisible litter beside the
    log — nothing reaps `.tmp-*.old`, so a `SIGKILL` mid-copy still leaves
    one behind. That gap is known and unaddressed: it needs a sweep at
    startup, which is a separate change with its own "is this really
    ours" question.
    """
    try:
        os.unlink(tmp_name)
    except OSError:
        pass
