"""Unit tests for source_settings.py — validation and, more
urgently, its migration.

**Why the migration tests carry real weight.** The live `settings.json`
predates the source block and carries flat `image_studio_base_url` and
`pool` keys. Without a translation step the running service — a
LaunchAgent driving a physical display in someone's home — would come back
from its next restart with no source configured at all. That is a silent
regression: nothing errors, the display just stops changing. The design asks for
this "with a test" for that reason, and the fixture below is a byte-for-
byte copy of the shape the live file actually has.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path


from display import settings as settings_module
from display import source_settings
from display.source_settings import SourceSettings

# The exact shape of the shipped/live settings.json before Step 0.
LEGACY_SETTINGS = {
    "rotation_interval_s": 900,
    "poll_interval_s": 1800,
    "fade_duration_s": 2.0,
    "image_studio_base_url": "http://images.example:8883",
    "pool": "starred",
    "cache_max": 300,
}


class MigrationTests(unittest.TestCase):
    def test_flat_keys_become_an_image_server_source(self) -> None:
        source = source_settings.source_from_settings_data(LEGACY_SETTINGS)
        self.assertEqual(source.kind, source_settings.KIND_IMAGE_SERVER)
        self.assertEqual(source.base_url, "http://images.example:8883")
        self.assertEqual(source.pool, "starred")

    def test_a_legacy_image_studio_kind_still_loads_as_image_server(self) -> None:
        """A settings.json written before the rename spells the kind
        "image_studio". It must still resolve to the image-server source,
        canonicalized to the current kind — otherwise a running display
        loses its source on the first restart after the upgrade."""
        data = {
            "source": {
                "kind": "image_studio",
                "base_url": "http://images.example:8883",
                "pool": "starred",
            }
        }
        source = source_settings.source_from_settings_data(data)
        self.assertEqual(source.kind, source_settings.KIND_IMAGE_SERVER)
        self.assertEqual(source.base_url, "http://images.example:8883")
        self.assertEqual(source.pool, "starred")

    def test_the_pool_choice_survives_the_migration(self) -> None:
        source = source_settings.source_from_settings_data(
            {**LEGACY_SETTINGS, "pool": "all"}
        )
        self.assertEqual(source.pool, "all")

    def test_migration_runs_through_the_real_settings_loader(self) -> None:
        """The end-to-end version of the above: a legacy file on disk,
        loaded by the function app.py actually calls."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps(LEGACY_SETTINGS))

            loaded = settings_module.load_settings(path)

        self.assertEqual(loaded.source.kind, source_settings.KIND_IMAGE_SERVER)
        self.assertEqual(loaded.source.base_url, "http://images.example:8883")
        self.assertEqual(loaded.source.pool, "starred")

    def test_the_legacy_keys_are_left_intact_for_a_rollback(self) -> None:
        """Step 0 does not delete the flat keys. They are the migration's
        input, and leaving them means a rollback to the previous build
        still finds a working config — which matters when the thing being
        rolled back drives a display in someone's home."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps(LEGACY_SETTINGS))
            loaded = settings_module.load_settings(path)
            on_disk = json.loads(path.read_text())

        self.assertEqual(on_disk, LEGACY_SETTINGS)
        self.assertEqual(loaded.image_studio_base_url, "http://images.example:8883")

    def test_an_explicit_source_block_wins_over_the_legacy_keys(self) -> None:
        data = {
            **LEGACY_SETTINGS,
            "source": {"kind": "folder", "folder": "/tmp/pictures"},
        }
        source = source_settings.source_from_settings_data(data)
        self.assertEqual(source.kind, source_settings.KIND_FOLDER)
        self.assertEqual(source.folder, "/tmp/pictures")

    def test_a_broken_source_block_falls_back_to_the_legacy_keys(self) -> None:
        """A hand-edited typo on a machine that still has working flat
        keys must keep showing pictures rather than reverting to an empty
        default folder."""
        data = {**LEGACY_SETTINGS, "source": {"kind": "nonsense"}}
        source = source_settings.source_from_settings_data(data)
        self.assertEqual(source.kind, source_settings.KIND_IMAGE_SERVER)

    def test_no_source_and_no_legacy_keys_defaults_to_a_folder(self) -> None:
        """'a folder on this Mac' is the default, preselected
        option — not somebody else's Image Server server."""
        source = source_settings.source_from_settings_data({"rotation_interval_s": 900})
        self.assertEqual(source.kind, source_settings.KIND_FOLDER)
        self.assertTrue(source.folder)

    def test_the_built_in_fallback_settings_use_a_folder_source(self) -> None:
        """With no readable config at all, a stranger's install must not
        silently point at a server they have never heard of."""
        loaded = settings_module.load_settings(Path("/nonexistent/settings.json"))
        self.assertEqual(loaded.source.kind, source_settings.KIND_FOLDER)

    def test_a_junk_base_url_does_not_migrate(self) -> None:
        data = {**LEGACY_SETTINGS, "image_studio_base_url": "not-a-url"}
        self.assertIsNone(source_settings.migrate_flat_keys(data))
        self.assertEqual(
            source_settings.source_from_settings_data(data).kind,
            source_settings.KIND_FOLDER,
        )


class ValidationTests(unittest.TestCase):
    """'Source options must not become the one unvalidated region.'
    settings.py range-checks every other field, which is why a bad config
    has never broken the live service."""

    def test_an_unknown_kind_is_rejected(self) -> None:
        self.assertIsNone(source_settings.validate_source({"kind": "carrier-pigeon"}))

    def test_a_non_mapping_is_rejected_without_raising(self) -> None:
        for junk in (None, [], "folder", 42):
            self.assertIsNone(source_settings.validate_source(junk))

    def test_folder_paths_are_expanded(self) -> None:
        """A `~` reaching FolderSource unexpanded would produce a literal
        './~/Pictures' relative to the process's cwd — which for a
        LaunchAgent is '/'."""
        source = source_settings.validate_source(
            {"kind": "folder", "folder": "~/Pictures"}
        )
        self.assertNotIn("~", source.folder)
        self.assertTrue(source.folder.endswith("/Pictures"))
        self.assertTrue(Path(source.folder).is_absolute())

    def test_an_unknown_sort_order_falls_back_rather_than_rejecting(self) -> None:
        source = source_settings.validate_source(
            {"kind": "folder", "folder": "/tmp/x", "sort_order": "by-vibes"}
        )
        self.assertEqual(source.sort_order, source_settings.DEFAULT_SORT_ORDER)

    def test_include_subfolders_is_coerced_to_a_bool(self) -> None:
        source = source_settings.validate_source(
            {"kind": "folder", "folder": "/tmp/x", "include_subfolders": "yes"}
        )
        self.assertIs(source.include_subfolders, True)

    def test_a_list_url_must_carry_an_allowed_scheme(self) -> None:
        for bad in ("file:///etc/passwd", "ftp://x/y.json", "example.com/y.json", 42, None):
            self.assertIsNone(
                source_settings.validate_source({"kind": "json_url", "list_url": bad}),
                bad,
            )

    def test_a_good_list_url_validates(self) -> None:
        source = source_settings.validate_source(
            {"kind": "json_url", "list_url": "https://example.com/pictures.json"}
        )
        self.assertEqual(source.list_url, "https://example.com/pictures.json")

    def test_an_image_studio_base_url_must_carry_an_allowed_scheme(self) -> None:
        self.assertIsNone(
            source_settings.validate_source(
                {"kind": "image_server", "base_url": "ftp://images.example"}
            )
        )

    def test_an_unknown_pool_falls_back_rather_than_rejecting(self) -> None:
        source = source_settings.validate_source(
            {"kind": "image_server", "base_url": "http://images.example", "pool": "most"}
        )
        self.assertEqual(source.pool, source_settings.DEFAULT_POOL)

    def test_a_source_block_round_trips_through_a_settings_file(self) -> None:
        block = {
            "kind": "json_url",
            "list_url": "https://example.com/pictures.json",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({**LEGACY_SETTINGS, "source": block}))
            loaded = settings_module.load_settings(path)

        self.assertEqual(loaded.source.kind, "json_url")
        self.assertEqual(loaded.source.list_url, "https://example.com/pictures.json")

    def test_the_dataclass_serializes_for_writing_back(self) -> None:
        """Step 4's settings window will need to write one of these."""
        source = SourceSettings(kind="folder", folder="/tmp/x")
        self.assertEqual(
            source_settings.validate_source(source.to_dict()).folder, "/tmp/x"
        )


class FactoryTests(unittest.TestCase):
    def test_each_kind_builds_its_source(self) -> None:
        from display.sources.factory import build_source
        from display.sources.folder import FolderSource
        from display.sources.image_server import ImageServerSource
        from display.sources.json_url import JsonUrlSource

        cases = [
            (SourceSettings(kind="folder", folder="/tmp/x"), FolderSource),
            (
                SourceSettings(kind="json_url", list_url="https://example.com/p.json"),
                JsonUrlSource,
            ),
            (
                SourceSettings(kind="image_server", base_url="http://images.example"),
                ImageServerSource,
            ),
        ]
        for config, expected in cases:
            source = build_source(config)
            self.assertIsInstance(source, expected)
            source.close()

    def test_an_unknown_kind_falls_back_to_a_folder_rather_than_raising(self) -> None:
        """This runs at startup on a machine driving a display, where
        'show nothing and exit' is the worst available outcome."""
        from display.sources.factory import build_source
        from display.sources.folder import FolderSource

        source = build_source(SourceSettings(kind="carrier-pigeon"))
        self.assertIsInstance(source, FolderSource)
        source.close()


if __name__ == "__main__":
    unittest.main()
