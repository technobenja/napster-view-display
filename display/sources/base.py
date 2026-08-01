"""The `ImageSource` interface.

`ImageSource` owns **byte retrieval as well as listing**. The earlier
"listing only" cut was wrong because `cache.py` was not source-agnostic:
it took an `httpx.Client` and built `/images/{filename}` itself, which is
meaningless for a local folder and wrong for an arbitrary URL. So:

    list_images() -> list[ImageRecord]
    fetch(record)  -> bytes | None      # never raises

`ImageCache.sync(records, source)` now calls `source.fetch(record)`, httpx
lives entirely inside the HTTP sources, and — the reason this refactor is
worth doing at all — safety checks get **exactly one enforcement
point** instead of three copies.

`ImageRecord` deliberately dropped `starred`, `width` and `height`:
none of the three fed any decision, and all three were Image Server
vocabulary leaking into a record that now describes a JPEG in someone's
Pictures folder just as often. `prompt` survives that cut for one specific
reason stated above — it is the source of the Image Server
`display_label`, and without it the menu bar would show a 36-character
UUID.
"""

from __future__ import annotations

import abc
import dataclasses
import enum
import hashlib
import re
from pathlib import Path

# Capped at ~28 chars so it fits a menu bar title.
MAX_LABEL_CHARS = 28
_ELLIPSIS = "..."

# Ids were already allow-listed to this character class, but
# *unbounded*. An id is used to build an on-disk filename, so an
# unbounded one is an ENAMETOOLONG (or a silently truncated collision)
# waiting to happen on a source that isn't Image Server's UUIDs.
MAX_ID_CHARS = 128
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def is_safe_id(value: object) -> bool:
    """True for a bounded, path-safe id. Never raises on any input type."""
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_ID_CHARS
        and bool(SAFE_ID_RE.match(value))
    )


def make_display_label(text: object, fallback: str = "") -> str:
    """Normalize arbitrary source-supplied text into a label.

    Collapses whitespace (a multi-line prompt must not become a multi-line
    menu title), caps at MAX_LABEL_CHARS *including* the ellipsis, and
    falls back rather than returning an empty string. Never raises — the
    input is whatever an API or filesystem handed us."""
    if not isinstance(text, str):
        text = fallback
    label = " ".join(text.split())
    if not label:
        label = " ".join(str(fallback).split())
    if len(label) > MAX_LABEL_CHARS:
        label = label[: MAX_LABEL_CHARS - len(_ELLIPSIS)].rstrip() + _ELLIPSIS
    return label


def stable_id_from_path(path: Path | str) -> str:
    """`sha256(absolute_path)[:16]` — required derivation.

    Stability across runs is not cosmetic: `rotation.py` persists the walk
    order keyed by id and resumes only when the pool hash matches, so an
    id derived from anything run-scoped (an index, `id()`, a mtime) would
    silently defeat resume on every restart — the pool would look
    different every time and reshuffle from position 0.

    The consequence to state plainly: **renaming or moving a file changes
    its id**, so it re-enters the rotation as a new picture.
    """
    absolute = str(Path(path).resolve())
    return hashlib.sha256(absolute.encode("utf-8")).hexdigest()[:16]


class ListOutcome(enum.Enum):
    """Why the last `list_images()` returned what it did.

    This exists because the old contract — `[]` for everything — is
    indistinguishable on a wall display from "there are no pictures".
    A screen bolted to a wall has no log, no console and no user holding
    it; the glass is the only channel, so the three failures below have to
    be separable *at the source* or the display can only ever guess.

    They are separated because they have different fixes:
      UNREACHABLE  -> the server or the LAN is down
      UNAUTHORIZED -> the credential is wrong, rotated, or missing
      EMPTY        -> reachable and authorised, but nothing matched
    """

    OK = "ok"
    EMPTY = "empty"
    UNAUTHORIZED = "unauthorized"
    UNREACHABLE = "unreachable"


@dataclasses.dataclass(frozen=True)
class ListStatus:
    """The outcome of one listing call, plus enough detail to say
    something true on screen."""

    outcome: ListOutcome = ListOutcome.OK
    #: Rows the server sent, *before* `_is_usable` filtering. Lets the
    #: display distinguish "the server has no starred pictures" from "the
    #: server sent 50 rows and none of them were usable" — same empty
    #: pool, completely different fix.
    rows_returned: int = 0
    #: Short technical reason ("HTTP 401", "timeout"), for status.json and
    #: the log. Not shown on the glass; the glass gets plain English.
    detail: str = ""
    #: Whether this request actually carried a credential.
    #:
    #: 🔴 Load-bearing, and it exists because measurement contradicted the
    #: plan. A *wrong* token does not produce 401 here: the server's
    #: check fails closed to the anonymous tier, which returns HTTP 200
    #: with zero rows. So "bad credential" and "genuinely empty" are
    #: indistinguishable in the response — the only thing that separates
    #: them is knowing whether we sent a token at all, which only the
    #: client knows. Without this flag the display confidently reports
    #: "authorised, but nothing matched" at the exact moment the token is
    #: broken, which is the worst of the available lies.
    credential_sent: bool = False

    @property
    def ok(self) -> bool:
        return self.outcome is ListOutcome.OK


@dataclasses.dataclass(frozen=True)
class ImageRecord:
    """One image, as described by whichever source produced it.

    `locator` is the source's own private handle for the bytes — an
    absolute path for `FolderSource`, an image URL for `JsonUrlSource`, a
    filename for `ImageServerSource`. Nothing outside the source that
    created it interprets this field; it exists so `fetch(record)` does
    not need a second lookup table keyed by id.
    """

    id: str
    filename: str
    display_label: str = ""
    locator: str = ""
    style: str = ""
    # Kept on the Image Server path specifically because it is
    # the source of that source's display_label. Empty everywhere else.
    prompt: str = ""


class ImageSource(abc.ABC):
    """What the display loop is allowed to know about where pictures come
    from. Both methods are on the "never raises" side of this project's
    conventions: a source that cannot reach its backend returns `[]` or
    `None`, and the caller keeps showing the last good frame."""

    #: Stable identifier for this source type. Used for cache namespacing
    #: and settings serialization, so it must not change casually.
    kind: str = "unknown"

    #: Whether `ImageCache` should copy bytes locally. False for
    #: `FolderSource`: a local folder needs no cache, and copying up to
    #: 300 of the user's own photos into `~/Library/Caches/` would be a
    #: rude and pointless use of their disk.
    caches: bool = True

    #: How often to re-list. Thirty minutes is right for an HTTP backend
    #: and absurd for a local folder — half an hour between dropping in a
    #: photo and seeing it reads as broken.
    poll_interval_s: float = 1800.0

    @property
    def cache_namespace(self) -> str:
        """A filesystem-safe subdirectory name isolating this source's
        cached bytes from every other source's. Without it,
        switching from one Image Server server to another — or from a URL
        list to Image Server — silently serves the previous source's
        images, because ids only have to be unique *within* a source."""
        digest = hashlib.sha256(self.identity().encode("utf-8")).hexdigest()[:12]
        return f"{self.kind}-{digest}"

    def identity(self) -> str:
        """Everything that makes this source configuration distinct from
        another of the same kind (base URL, folder path, list URL).
        Subclasses override; the default is kind alone."""
        return self.kind

    @property
    def label(self) -> str:
        """Human-readable "where pictures come from", for `source_label` in status.json and settings status line.

        Distinct from `identity()` on purpose: identity is a cache key
        and must stay exact and stable, while this is read by a person
        and should stay short. Overriding subclasses must **not** widen
        it into a full URL or an absolute path; the label cap on the adjacent
        `display_label` for the same reason, and a menu bar is not a
        place to render `/Users/someone/Pictures/holidays/2019`."""
        return self.kind

    @abc.abstractmethod
    def list_images(self) -> list[ImageRecord]:
        """Current contents of the source. `[]` on any failure.

        The return type stays a bare list — every caller and the whole
        cache/rotation path is built on it. *Why* it was empty is reported
        alongside, on `last_status`, so adding the diagnosis did not
        require rewriting the contract that works.
        """

    @property
    def last_status(self) -> ListStatus:
        """Outcome of the most recent `list_images()`.

        The default is deliberately honest rather than optimistic: a
        source that has not been asked yet, or one that does not
        distinguish failures, reports OK and the display shows nothing
        unusual. Only sources that genuinely know better override this —
        claiming a diagnosis you cannot make is worse than making none.
        """
        return getattr(self, "_last_status", ListStatus())

    @abc.abstractmethod
    def fetch(self, record: ImageRecord) -> bytes | None:
        """Validated image bytes, or None. Never raises.

        Implementations must run the returned bytes through
        `image_safety` before handing them back, so no caller ever has to
        remember to."""

    def path_for(self, record: ImageRecord) -> Path | None:
        """Local path for a record, for `caches = False` sources only.
        Returns None for sources whose bytes only exist over the network."""
        return None

    def close(self) -> None:
        """Release any held resources (HTTP clients). Never raises."""

    def __enter__(self) -> ImageSource:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


__all__ = [
    "MAX_ID_CHARS",
    "MAX_LABEL_CHARS",
    "SAFE_ID_RE",
    "ImageRecord",
    "ImageSource",
    "ListOutcome",
    "ListStatus",
    "is_safe_id",
    "make_display_label",
    "stable_id_from_path",
]
