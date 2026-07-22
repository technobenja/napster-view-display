"""Runtime settings — `~/.viewlab/settings.json`.

Tunable knobs, not calibration data (that's calibration.py) and not a
secret (there are none — Image Server is unauthenticated same-lab
infrastructure). Same failure philosophy as calibration.py: a missing
file, bad JSON, an unknown `schema_version` major, or an out-of-range
value falls back to a safe default and logs loudly. Never raises.

Canonical location and resolution order are identical to calibration's — `~/.viewlab/settings.json` first, the bundled
`display/config/settings.json` as read-only seed copied there on first
read, built-in defaults last. The shipped seed carries no
`schema_version`, which config_store treats as v1 by definition.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from display import blank_schedule, paths
from display import source_settings
from display.blank_schedule import BlankSchedule
from display.config_store import ConfigSource, Resolved, WatchedConfig, resolve_config, schema_is_supported
from display.source_settings import SourceSettings

_LABEL = "settings.py"

# Read-only bundled seed — not where the running app's settings live
# (that is paths.settings_path()).
DEFAULT_SETTINGS_PATH = paths.bundled_settings_path()

# Confirmed defaults — chosen deliberately rather than guessed: the
# rotation and fade durations were settled by hand on the real device,
# and the crossfade-adoption decision (fade_duration_s > 0) came from the
# same session.
FALLBACK_ROTATION_INTERVAL_S = 900.0  # 15 minutes
FALLBACK_POLL_INTERVAL_S = 1800.0  # 30 minutes
FALLBACK_FADE_DURATION_S = 2.0
#: Default for the **legacy** flat `image_studio_base_url` key only.
#: A neutral placeholder, not a working endpoint: since Step 6 the bundled
#: seed carries no source at all, so a fresh install goes through the
#: first-run flow instead of inheriting somebody else's server. This value
#: is reached only when a config predating the source block omits the key, and it must
#: still satisfy the `http(s)://` check below — an empty string here would
#: invalidate the whole settings document rather than mean "no source".
FALLBACK_IMAGE_STUDIO_BASE_URL = "http://localhost:8883"
FALLBACK_POOL = "starred"
FALLBACK_CACHE_MAX = 300

#: "Order: shuffle or in order". True is the pre-existing
#: behaviour — `Rotation` has always shuffled — so a settings file that
#: predates this key keeps the rotation it already had.
FALLBACK_SHUFFLE = True

VALID_POOLS = {"starred", "all"}


@dataclasses.dataclass(frozen=True)
class Settings:
    rotation_interval_s: float
    poll_interval_s: float
    fade_duration_s: float
    # Legacy flat keys (the pre-source-block format). Retained in this step because they are
    # the *input* to the source migration in source_settings.py, and a
    # machine whose settings.json still carries only these must keep
    # working across the restart that introduces `source`. Retiring them
    # belongs with the rest of the sweep in Step 6.
    image_studio_base_url: str
    pool: str
    cache_max: int
    #: Source block, migrated from the flat keys above when absent.
    source: SourceSettings = dataclasses.field(
        default_factory=source_settings.default_source
    )
    #: Rotation order. `Rotation` shuffles when this is True and
    #: walks the source's own listing order when it is False.
    shuffle: bool = FALLBACK_SHUFFLE
    #: Schedule. Disabled by default, and a malformed block parses
    #: to a disabled one — see `blank_schedule.parse_schedule`.
    blank_schedule: BlankSchedule = dataclasses.field(
        default_factory=BlankSchedule
    )


def _fallback_settings() -> Settings:
    return Settings(
        rotation_interval_s=FALLBACK_ROTATION_INTERVAL_S,
        poll_interval_s=FALLBACK_POLL_INTERVAL_S,
        fade_duration_s=FALLBACK_FADE_DURATION_S,
        image_studio_base_url=FALLBACK_IMAGE_STUDIO_BASE_URL,
        pool=FALLBACK_POOL,
        cache_max=FALLBACK_CACHE_MAX,
        # Note this is *not* built from the two lab-specific fallbacks
        # above: with no readable config at all, default is "a
        # folder on this Mac", not somebody else's Image Server server.
        source=source_settings.default_source(),
        shuffle=FALLBACK_SHUFFLE,
        blank_schedule=BlankSchedule(),
    )


def _validated_settings(data: Mapping[str, Any]) -> Settings | None:
    try:
        rotation_interval_s = float(data.get("rotation_interval_s", FALLBACK_ROTATION_INTERVAL_S))
        poll_interval_s = float(data.get("poll_interval_s", FALLBACK_POLL_INTERVAL_S))
        fade_duration_s = float(data.get("fade_duration_s", FALLBACK_FADE_DURATION_S))
        image_studio_base_url = str(data.get("image_studio_base_url", FALLBACK_IMAGE_STUDIO_BASE_URL))
        pool = str(data.get("pool", FALLBACK_POOL))
        cache_max = int(data.get("cache_max", FALLBACK_CACHE_MAX))
    except (TypeError, ValueError):
        return None

    if rotation_interval_s <= 0:
        return None
    if poll_interval_s <= 0:
        return None
    if fade_duration_s < 0:
        return None
    if not image_studio_base_url.startswith(("http://", "https://")):
        return None
    if pool not in VALID_POOLS:
        return None
    if cache_max <= 0:
        return None

    return Settings(
        rotation_interval_s=rotation_interval_s,
        poll_interval_s=poll_interval_s,
        fade_duration_s=fade_duration_s,
        image_studio_base_url=image_studio_base_url,
        pool=pool,
        cache_max=cache_max,
        # migration: an explicit `source` block wins, the legacy
        # flat keys above are the fallback, a folder source is the last
        # resort. Never None, so `settings.source` is always usable.
        source=source_settings.source_from_settings_data(data),
        # Neither of these can fail validation in a way that rejects the
        # whole document: a bad `shuffle` falls back to the shipped
        # behaviour and a bad `blank_schedule` parses to a disabled one.
        # A settings file is the thing keeping a display alive, and
        # refusing it wholesale over a cosmetic key would be the wrong
        # trade — the same reasoning `source_from_settings_data` follows.
        shuffle=(
            data["shuffle"]
            if isinstance(data.get("shuffle"), bool)
            else FALLBACK_SHUFFLE
        ),
        blank_schedule=blank_schedule.parse_schedule(data.get("blank_schedule")),
    )


def load_settings(path: Path | str = DEFAULT_SETTINGS_PATH) -> Settings:
    """Load and validate settings.json, falling back to safe defaults on
    any failure. Never raises."""
    path = Path(path)

    try:
        raw = path.read_text()
    except OSError as exc:
        print(
            f"settings.py: cannot read {path} ({exc}); using fallback defaults.",
            file=sys.stderr,
        )
        return _fallback_settings()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(
            f"settings.py: {path} is not valid JSON ({exc}); using fallback defaults.",
            file=sys.stderr,
        )
        return _fallback_settings()

    if not isinstance(data, dict):
        print(
            f"settings.py: {path} has an unexpected shape; using fallback defaults.",
            file=sys.stderr,
        )
        return _fallback_settings()

    # Unknown schema major → bundled fallback, logged loudly.
    if not schema_is_supported(data, path, _LABEL):
        return _fallback_settings()

    settings = _validated_settings(data)
    if settings is None:
        print(
            f"settings.py: {path} failed validation; using fallback defaults.",
            file=sys.stderr,
        )
        return _fallback_settings()

    return settings


def validate_settings(data: Mapping[str, Any]) -> Settings | None:
    """Public name for the validator, shared by the resolution order
    and the hot-reload watcher. Never raises."""
    return _validated_settings(data)


def load_settings_resolved() -> Resolved[Settings]:
    """Resolution order (user file, then bundled seed copied to
    `~/.viewlab/`, then built-in defaults), with provenance."""
    return resolve_config(
        user_path=paths.settings_path(),
        bundled_path=paths.bundled_settings_path(),
        validate=validate_settings,
        fallback=_fallback_settings,
        label=_LABEL,
    )


def settings_watcher() -> WatchedConfig[Settings]:
    """A watcher over `~/.viewlab/settings.json` — user path only."""
    return WatchedConfig(paths.settings_path(), validate_settings, _LABEL)


__all__ = [
    "BlankSchedule",
    "ConfigSource",
    "DEFAULT_SETTINGS_PATH",
    "FALLBACK_SHUFFLE",
    "Settings",
    "SourceSettings",
    "load_settings",
    "load_settings_resolved",
    "settings_watcher",
    "validate_settings",
]
