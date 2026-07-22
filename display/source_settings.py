"""The `source` block of settings.json.

Two jobs.

**Validation.** source options must not become the
one unvalidated region of the config: `settings.py` range-checks every
other field, which is why a bad config has never broken the live service.
So URLs are scheme-checked, choices are allow-listed, and every field has
a fallback. Nothing here raises.

**Migration.** The live `settings.json` predates all of this and carries
flat `image_studio_base_url` and `pool` keys. Without a translation step
the running service would come back after its next restart with no source
configured at all — a silent regression on a machine driving a display in
someone's home. `source_from_settings_data()` is that translation: an
explicit `source` block wins, the flat legacy keys are the fallback, and a
folder source is the last resort ("a folder on this Mac" is the
default, preselected option).

The legacy flat keys are deliberately *not* deleted from `Settings` in
this step. They are the migration's input, and removing them belongs with
the rest of the depersonalization sweep in Step 6.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from pathlib import Path
from typing import Any

KIND_FOLDER = "folder"
KIND_JSON_URL = "json_url"
KIND_IMAGE_SERVER = "image_server"

#: The image-server kind used to be persisted as "image_studio". A
#: settings.json written before the rename still spells it that way, so it
#: is accepted as an alias and canonicalized to KIND_IMAGE_SERVER on load.
#: Without this a running display would silently lose its source on the
#: first restart after the upgrade. New installs only ever write the
#: canonical value above.
LEGACY_KIND_IMAGE_SERVER = "image_studio"

VALID_KINDS = frozenset(
    {KIND_FOLDER, KIND_JSON_URL, KIND_IMAGE_SERVER, LEGACY_KIND_IMAGE_SERVER}
)

VALID_SORT_ORDERS = frozenset({"name", "newest", "oldest"})
DEFAULT_SORT_ORDER = "name"

VALID_POOLS = frozenset({"starred", "all"})
DEFAULT_POOL = "starred"

ALLOWED_URL_SCHEMES = ("http://", "https://")


def default_folder() -> str:
    """`~/Pictures`. Resolved at call time, like everything in paths.py,
    so a test can patch `Path.home()` and get an isolated tree."""
    return str(Path.home() / "Pictures")


@dataclasses.dataclass(frozen=True)
class SourceSettings:
    """Validated source configuration. One flat dataclass rather than a
    class hierarchy: the design cut the generic capability descriptor, and with
    three sources carrying five static options between them, a hierarchy
    would be the same machinery wearing a different hat."""

    kind: str = KIND_FOLDER
    # folder
    folder: str = ""
    include_subfolders: bool = False
    sort_order: str = DEFAULT_SORT_ORDER
    # json_url
    list_url: str = ""
    # image_server
    base_url: str = ""
    pool: str = DEFAULT_POOL

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def default_source() -> SourceSettings:
    return SourceSettings(kind=KIND_FOLDER, folder=default_folder())


def _looks_like_a_url(value: object) -> bool:
    return isinstance(value, str) and value.startswith(ALLOWED_URL_SCHEMES)


def validate_source(data: object) -> SourceSettings | None:
    """Validate a `source` block. Returns None — never raises — if it is
    unusable, leaving the caller to fall back."""
    if not isinstance(data, Mapping):
        return None
    kind = data.get("kind")
    if kind not in VALID_KINDS:
        return None

    if kind == KIND_FOLDER:
        folder = data.get("folder") or default_folder()
        if not isinstance(folder, str) or not folder.strip():
            return None
        sort_order = data.get("sort_order", DEFAULT_SORT_ORDER)
        if sort_order not in VALID_SORT_ORDERS:
            sort_order = DEFAULT_SORT_ORDER
        return SourceSettings(
            kind=KIND_FOLDER,
            folder=str(Path(folder).expanduser()),
            include_subfolders=bool(data.get("include_subfolders", False)),
            sort_order=sort_order,
        )

    if kind == KIND_JSON_URL:
        # Scheme-checked here as well as in sources/net.py. This one is a
        # config-validity check that keeps a typo out of the saved file;
        # net.py's is the security boundary and does the resolution work.
        if not _looks_like_a_url(data.get("list_url")):
            return None
        return SourceSettings(kind=KIND_JSON_URL, list_url=str(data["list_url"]))

    if not _looks_like_a_url(data.get("base_url")):
        return None
    pool = data.get("pool", DEFAULT_POOL)
    if pool not in VALID_POOLS:
        pool = DEFAULT_POOL
    return SourceSettings(
        kind=KIND_IMAGE_SERVER, base_url=str(data["base_url"]), pool=pool
    )


def migrate_flat_keys(data: Mapping[str, Any]) -> SourceSettings | None:
    """Translate the legacy flat `image_studio_base_url` / `pool` keys
    into a source block. None if they are absent or unusable."""
    base_url = data.get("image_studio_base_url")
    if not _looks_like_a_url(base_url):
        return None
    pool = data.get("pool", DEFAULT_POOL)
    if pool not in VALID_POOLS:
        pool = DEFAULT_POOL
    return SourceSettings(
        kind=KIND_IMAGE_SERVER, base_url=str(base_url), pool=pool
    )


def source_from_settings_data(data: Mapping[str, Any]) -> SourceSettings:
    """Resolve the source for a settings document, in priority order:
    an explicit valid `source` block, then the migrated legacy flat keys,
    then the default folder source. Always returns something."""
    if not isinstance(data, Mapping):
        return default_source()
    if "source" in data:
        validated = validate_source(data.get("source"))
        if validated is not None:
            return validated
        # An explicit-but-broken source block falls through to the legacy
        # keys rather than to the default, so a hand-edited typo on a
        # machine that still has working flat keys keeps showing pictures.
    migrated = migrate_flat_keys(data)
    if migrated is not None:
        return migrated
    return default_source()


__all__ = [
    "ALLOWED_URL_SCHEMES",
    "DEFAULT_POOL",
    "DEFAULT_SORT_ORDER",
    "KIND_FOLDER",
    "KIND_IMAGE_SERVER",
    "KIND_JSON_URL",
    "LEGACY_KIND_IMAGE_SERVER",
    "VALID_KINDS",
    "VALID_POOLS",
    "VALID_SORT_ORDERS",
    "SourceSettings",
    "default_folder",
    "default_source",
    "migrate_flat_keys",
    "source_from_settings_data",
    "validate_source",
]
