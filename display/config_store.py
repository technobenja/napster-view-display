"""Config resolution order, first-run migration, and hot-reload change
detection.

Two jobs, kept together because both hinge on the same "read this JSON
file defensively and validate it" primitive:

**1. Resolution order, specified once so that any tool reading
this config honours the same order:**

1. `~/.viewlab/<name>.json` if present and valid
2. else the bundled `display/config/<name>.json`, **copied to
   `~/.viewlab/` on first read** (atomic write, via atomic_io)
3. else the conservative bundled fallback compiled into the module

The resolved `ConfigSource` is returned to the caller so that a silent
fallback is observable: both apps record which source
they used in their status file.

**2. `schema_version` must actually be read.** It is decorative in
the shipped code: `calibration.py` parses right past it. Harmless in one
app, dangerous across two independently-versioned ones. An unknown MAJOR
version means "this file was written by a newer app than me," and the
correct response is to refuse it loudly rather than attempt a parse that
half-works — v1 is additive-only (new optional keys yes, changed
meanings never), so a *known* major can always be parsed by an older
reader, and an *unknown* one never can be.

**3. Hot-reload change detection.** `WatchedConfig` compares
`(st_mtime_ns, st_size, st_ino)` rather than mtime alone: mtime alone
misses a same-second rewrite, and an atomic write-then-rename (which is
what the UI will use) replaces the inode entirely, so `st_ino` is the
signal that actually fires reliably for this project's own write
pattern.

On a malformed file `WatchedConfig.poll()` returns None — the caller
keeps its **last-good in-memory config**, never the safe default. Plan
The reason: mid-calibration, falling back to the default
would make the circle jump wildly on every half-typed edit.

Nothing here raises. Every failure path logs and degrades.
"""

from __future__ import annotations

import dataclasses
import enum
import json
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Generic, NamedTuple, TypeVar

from display.atomic_io import atomic_write_json

T = TypeVar("T")

# The only schema major this code understands. v1 is additive-only.
SUPPORTED_SCHEMA_MAJOR = 1


class ConfigSource(enum.Enum):
    """Which step of resolution order actually produced the value.

    Recorded in status.json ("Both apps record which calibration
    source they used in their status file, so a silent fallback is
    observable")."""

    USER = "user"
    BUNDLED_SEED = "bundled-seed"
    FALLBACK = "fallback"


@dataclasses.dataclass(frozen=True)
class Resolved(Generic[T]):
    """A loaded config plus the provenance the resolution order must surface."""

    value: T
    source: ConfigSource
    # None when source is FALLBACK — there was no usable file.
    path: Path | None


# -- defensive JSON reading --------------------------------------------


def read_json_object(path: Path, label: str) -> dict[str, Any] | None:
    """Read `path` as a JSON object. Returns None — never raises — on a
    missing file, an unreadable one (permissions), invalid JSON, or a
    top-level value that is not an object.

    `label` names the calling module in log output ("calibration.py"),
    matching the existing convention in calibration.py/settings.py where
    every log line is prefixed with its own module name."""
    try:
        raw = path.read_text()
    except FileNotFoundError:
        # Absent is an ordinary, expected state (first run) — not worth a
        # log line on its own; the caller logs which source it settled on.
        return None
    except OSError as exc:
        print(f"{label}: cannot read {path} ({exc}).", file=sys.stderr)
        return None

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"{label}: {path} is not valid JSON ({exc}).", file=sys.stderr)
        return None

    if not isinstance(data, dict):
        print(f"{label}: {path} has an unexpected shape (not a JSON object).", file=sys.stderr)
        return None
    return data


def schema_major(data: Mapping[str, Any]) -> int | None:
    """Extract the MAJOR component of `schema_version`.

    Returns `SUPPORTED_SCHEMA_MAJOR` when the key is absent: files
    predating the field (the shipped settings.json has no
    `schema_version` at all) are v1 by definition, and refusing them
    would break every existing install for no safety benefit.

    Returns None when the value is present but not interpretable as a
    version — which is treated exactly like an unknown major, since a
    reader that cannot tell what version it is holding must not guess.
    Accepts int, float, and dotted-string forms (1, 1.2, "1", "1.2")."""
    if "schema_version" not in data:
        return SUPPORTED_SCHEMA_MAJOR
    value = data["schema_version"]
    if isinstance(value, bool):  # bool is an int subclass; never a version
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        head = value.strip().split(".", 1)[0]
        try:
            return int(head)
        except ValueError:
            return None
    return None


def schema_is_supported(data: Mapping[str, Any], path: Path, label: str) -> bool:
    """"unknown major version → use bundled fallback and log
    loudly". This is the loud part; the caller supplies the fallback."""
    major = schema_major(data)
    if major == SUPPORTED_SCHEMA_MAJOR:
        return True
    print(
        f"{label}: {path} declares schema_version="
        f"{data.get('schema_version')!r}, whose major version is not "
        f"{SUPPORTED_SCHEMA_MAJOR}. This file was written by a different "
        f"version of the app and cannot be safely parsed by this one "
        f"(v{SUPPORTED_SCHEMA_MAJOR} is additive-only, so a changed major "
        f"means changed meanings). REFUSING IT and falling back — the "
        f"file is left untouched. Upgrade the app, or move the file "
        f"aside to start fresh.",
        file=sys.stderr,
    )
    return False


def load_valid(
    path: Path,
    validate: Callable[[Mapping[str, Any]], T | None],
    label: str,
) -> T | None:
    """Read, schema-check, and validate one file. Returns None on any
    failure whatsoever, never raises.

    This is the single primitive behind both the resolution order and the
    hot-reload path, which is what guarantees the running app never
    accepts a file at reload time that it would have rejected at
    startup."""
    data = read_json_object(path, label)
    if data is None:
        return None
    if not schema_is_supported(data, path, label):
        return None
    try:
        return validate(data)
    except Exception as exc:  # a validator bug must not take the app down
        print(f"{label}: validating {path} raised unexpectedly ({exc!r}); rejecting it.", file=sys.stderr)
        return None


# -- resolution order + first-run migration -----------------------


def resolve_config(
    *,
    user_path: Path,
    bundled_path: Path,
    validate: Callable[[Mapping[str, Any]], T | None],
    fallback: Callable[[], T],
    label: str,
) -> Resolved[T]:
    """The resolution order, including the first-run copy.

    Step 2's copy is best-effort: if `~/.viewlab/` cannot be written, the
    bundled value is still used and the app runs normally — it just
    re-reads the seed on every start. Failing to boot because a *cache of
    a file we already have* could not be written would be absurd."""
    user_value = load_valid(user_path, validate, label)
    if user_value is not None:
        return Resolved(value=user_value, source=ConfigSource.USER, path=user_path)

    bundled_data = read_json_object(bundled_path, label)
    if bundled_data is not None and schema_is_supported(bundled_data, bundled_path, label):
        bundled_value: T | None
        try:
            bundled_value = validate(bundled_data)
        except Exception as exc:
            print(f"{label}: validating {bundled_path} raised unexpectedly ({exc!r}).", file=sys.stderr)
            bundled_value = None
        if bundled_value is not None:
            _seed_user_copy(user_path, bundled_data, label)
            return Resolved(
                value=bundled_value, source=ConfigSource.BUNDLED_SEED, path=bundled_path
            )

    print(
        f"{label}: neither {user_path} nor {bundled_path} yielded a valid "
        f"config; using the conservative built-in fallback.",
        file=sys.stderr,
    )
    return Resolved(value=fallback(), source=ConfigSource.FALLBACK, path=None)


def _seed_user_copy(user_path: Path, data: Mapping[str, Any], label: str) -> None:
    """Copy validated bundled seed data to the user's config directory
    (step 2, "copied to `~/.viewlab/` on first read (atomic write)").

    Copies the *parsed* object rather than the raw bytes, deliberately:
    it guarantees the file that lands in `~/.viewlab/` is exactly what
    this app validated, and it drops nothing but whitespace. Only ever
    called when `user_path` did not yield a valid config — so a user file
    that exists but is malformed is never overwritten; the user's
    hand-edited work is preserved for them to fix.
    """
    if user_path.exists():
        print(
            f"{label}: {user_path} exists but is not usable; leaving it "
            f"untouched (not overwriting a hand-edited file) and running "
            f"from the bundled seed for now.",
            file=sys.stderr,
        )
        return
    try:
        atomic_write_json(user_path, dict(data))
    except OSError as exc:
        print(
            f"{label}: could not seed {user_path} from the bundled "
            f"default ({exc}); continuing with the bundled values.",
            file=sys.stderr,
        )
        return
    print(f"{label}: seeded {user_path} from the bundled default.", file=sys.stderr)


# -- hot-reload change detection ----------------------------------


class FileStamp(NamedTuple):
    """The identity triple. `st_ino` is not paranoia: an atomic
    write-then-rename — what atomic_io.py does, and what the UI will use
    — always lands a *different inode*, so this is the field that fires
    for this project's own writes."""

    mtime_ns: int
    size: int
    inode: int


def stamp(path: Path) -> FileStamp | None:
    """Stat `path`, or None if it does not exist / cannot be stat'ed."""
    try:
        st = path.stat()
    except OSError:
        return None
    return FileStamp(mtime_ns=st.st_mtime_ns, size=st.st_size, inode=st.st_ino)


class WatchedConfig(Generic[T]):
    """Polls one file in `~/.viewlab/` for changes.

    Watches the **user path only, never the repo's `display/config/`**. That is not tidiness: the LaunchAgent runs from the live
    tree, so watching the repo copy would mean a half-saved edit during
    development is acted on immediately by the running display.

    `poll()` returns a new value only when the file changed *and* the new
    contents are valid. Every other outcome — unchanged, deleted,
    malformed, unreadable, wrong schema major — returns None, which means
    "keep what you have". The last-good value is the caller's; this class
    deliberately does not hold it, so there is no way for a stale copy
    here to diverge from what the app is actually drawing.
    """

    def __init__(
        self,
        path: Path,
        validate: Callable[[Mapping[str, Any]], T | None],
        label: str,
    ) -> None:
        self._path = path
        self._validate = validate
        self._label = label
        # Adopt the current state as already-seen so the first tick after
        # startup never re-applies what startup just loaded.
        self._stamp = stamp(path)
        self._warned_missing = False

    @property
    def path(self) -> Path:
        return self._path

    def poll(self) -> T | None:
        current = stamp(self._path)

        if current is None:
            # Deleted or unstat-able. This keeps the last-good
            # config; it deliberately does NOT re-run the resolution
            # order and re-seed from the bundle, which would silently
            # discard the user's calibration mid-run.
            if self._stamp is not None and not self._warned_missing:
                print(
                    f"{self._label}: {self._path} has disappeared; keeping "
                    f"the last-good configuration in memory.",
                    file=sys.stderr,
                )
                self._warned_missing = True
            self._stamp = None
            return None

        self._warned_missing = False
        if current == self._stamp:
            return None

        # Record the new stamp *before* validating, so a file that stays
        # malformed is complained about once per edit rather than four
        # times a second.
        self._stamp = current

        value = load_valid(self._path, self._validate, self._label)
        if value is None:
            print(
                f"{self._label}: {self._path} changed but is not usable; "
                f"keeping the last-good configuration in memory (NOT "
                f"reverting to defaults).",
                file=sys.stderr,
            )
            return None

        print(f"{self._label}: reloaded {self._path}.", file=sys.stderr)
        return value
