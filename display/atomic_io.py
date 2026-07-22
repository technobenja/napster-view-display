"""Shared atomic-write-then-rename helper.

Extracted from app.py, cache.py, and rotation.py, which each carried an
identical copy of this ~15-line temp-file-then-`os.replace()` dance.
`os.replace()` is atomic only when source and destination share a
filesystem, which is why the temp file is created in the destination's
own parent directory rather than a system temp dir.

On any failure the temp file is cleaned up and the original exception
re-raised: `path` is left untouched (old contents or absent, never a
half-written file). Callers decide what "write failed" means for their
own data - none of app.py's status.json, cache.py's manifest.json, or
rotation.py's rotation_state.json treat a write failure as fatal to the
caller's larger operation, but this helper itself does not swallow it.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_json(path: Path, data: Any) -> None:
    """Write `data` as JSON to `path` atomically (temp file + os.replace)."""
    atomic_write_bytes(path, json.dumps(data, indent=2).encode("utf-8"), suffix=".json")


def atomic_write_bytes(path: Path, data: bytes, *, suffix: str = ".tmp") -> None:
    """Write raw `data` to `path` atomically (temp file + os.replace).

    Added for LaunchAgent plists, which `plistlib.dumps()` produces as
    bytes. A half-written plist matters more than a half-written status
    file: launchd reads it at login, and a truncated one is a service
    that silently never starts. `suffix` only shapes the temp file's
    name, which is visible in the destination directory for the moment
    the write takes.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
