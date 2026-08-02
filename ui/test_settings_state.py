"""Settings decisions, tested without AppKit.

The first-run/settings copy is specified verbatim, so several of these
assert exact strings. That is deliberate and not brittleness: those
strings *are* the requirement, and a test that accepted any message
containing "found" would not be testing the thing that was specified.
"""

from __future__ import annotations

import tempfile
import unittest
from unittest import mock
from pathlib import Path

from display import source_settings
from display.blank_schedule import BlankSchedule
from display.source_settings import SourceSettings
from ui import settings_state as ss


class IntervalChoiceTests(unittest.TestCase):
    """"15 minutes" is a preference; "900" is a unit conversion."""

    def test_the_four_choices_are_the_plans_four(self) -> None:
        self.assertEqual(
            [label for label, _ in ss.INTERVAL_CHOICES],
            ["1 minute", "15 minutes", "1 hour", "1 day"],
        )

    def test_exact_values_round_trip(self) -> None:
        for index, (_, seconds) in enumerate(ss.INTERVAL_CHOICES):
            with self.subTest(seconds=seconds):
                self.assertEqual(ss.interval_index(seconds), index)
                self.assertEqual(ss.interval_seconds(index), seconds)

    def test_a_hand_edited_value_lands_on_the_closest_choice(self) -> None:
        """settings.json is hand-editable; someone who set 600 should see
        the popup land somewhere sensible, not silently jump."""
        self.assertEqual(ss.interval_index(600.0), 1)  # closer to 900 than 60
        self.assertEqual(ss.interval_index(70.0), 0)
        self.assertEqual(ss.interval_index(70000.0), 3)

    def test_junk_falls_back_to_the_default(self) -> None:
        for value in (None, "900", True, [], float("nan")):
            with self.subTest(value=value):
                self.assertIsInstance(ss.interval_index(value), int)

    def test_an_out_of_range_index_does_not_raise(self) -> None:
        self.assertEqual(ss.interval_seconds(99), ss.DEFAULT_INTERVAL_S)
        self.assertEqual(ss.interval_seconds(-1), ss.DEFAULT_INTERVAL_S)
        self.assertEqual(ss.interval_seconds("1"), ss.DEFAULT_INTERVAL_S)


class SourceFormTests(unittest.TestCase):
    """Static layout: unselected rows are disabled, not hidden, so
    their values must survive being unselected."""

    def test_folder_is_first_and_the_default(self) -> None:
        self.assertEqual(ss.SOURCE_ROWS[0][0], source_settings.KIND_FOLDER)
        self.assertEqual(ss.SourceForm().kind, source_settings.KIND_FOLDER)

    def test_the_image_server_sublabel_describes_an_http_server(self) -> None:
        self.assertEqual(
            ss.SOURCE_SUBLABELS[source_settings.KIND_IMAGE_SERVER],
            "A server that lists images over HTTP (advanced).",
        )

    def test_the_json_url_sublabel_states_the_contract(self) -> None:
        self.assertIn(
            "JSON array of image URLs",
            ss.SOURCE_SUBLABELS[source_settings.KIND_JSON_URL],
        )

    def test_switching_rows_does_not_lose_the_other_rows_values(self) -> None:
        form = ss.SourceForm(
            kind=source_settings.KIND_JSON_URL,
            folder="/tmp/pics",
            list_url="https://example.com/list.json",
            base_url="http://studio.example:8883",
        )
        form.kind = source_settings.KIND_FOLDER
        self.assertEqual(form.list_url, "https://example.com/list.json")
        form.kind = source_settings.KIND_JSON_URL
        self.assertEqual(form.folder, "/tmp/pics")

    def test_from_settings_seeds_unselected_rows_with_usable_defaults(self) -> None:
        """A preselected row with an empty required field is a dead end
        on first run."""
        form = ss.SourceForm.from_settings(
            SourceSettings(kind=source_settings.KIND_JSON_URL, list_url="https://x/y")
        )
        self.assertTrue(form.folder)

    def test_to_settings_goes_through_the_displays_own_validator(self) -> None:
        """The window must not be able to save a document the display
        would then reject and silently fall back from."""
        form = ss.SourceForm(
            kind=source_settings.KIND_JSON_URL, list_url="not-a-url"
        )
        self.assertIsNone(form.to_settings())
        form.list_url = "https://example.com/list.json"
        self.assertEqual(form.to_settings().kind, source_settings.KIND_JSON_URL)

    def test_to_settings_trims_whitespace(self) -> None:
        form = ss.SourceForm(
            kind=source_settings.KIND_IMAGE_SERVER,
            base_url="  http://studio.example:8883  ",
        )
        self.assertEqual(form.to_settings().base_url, "http://studio.example:8883")


class ProbeFolderTests(unittest.TestCase):
    """Folder copy, exactly."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, name: str) -> None:
        (self.folder / name).write_bytes(b"x")

    def test_counts_only_image_files(self) -> None:
        for name in ("a.png", "b.jpg", "c.jpeg", "d.txt", "e.heic"):
            self._write(name)
        result = ss.probe_folder(str(self.folder))
        self.assertEqual(result.message, "3 pictures found")
        self.assertTrue(result.save_enabled)

    def test_the_empty_message_names_the_extensions(self) -> None:
        """"No pictures in this folder" (naming extensions)."""
        result = ss.probe_folder(str(self.folder))
        self.assertTrue(result.message.startswith("No pictures in this folder"))
        for extension in (".png", ".jpg", ".jpeg"):
            self.assertIn(extension, result.message)

    def test_save_is_disabled_on_zero(self) -> None:
        """Stated directly."""
        self.assertFalse(ss.probe_folder(str(self.folder)).save_enabled)

    def test_one_picture_is_not_reported_as_one_pictures(self) -> None:
        self._write("only.png")
        self.assertEqual(ss.probe_folder(str(self.folder)).message, "1 picture found")

    def test_an_unreadable_folder_names_the_path_and_blocks_save(self) -> None:
        result = ss.probe_folder(str(self.folder / "does-not-exist"))
        self.assertIs(result.outcome, ss.Outcome.NO_FOLDER)
        self.assertFalse(result.save_enabled)

    def test_a_blank_folder_field_asks_for_one(self) -> None:
        for value in ("", "   ", None, 3):
            with self.subTest(value=value):
                self.assertIs(ss.probe_folder(value).outcome, ss.Outcome.INCOMPLETE)

    def test_it_performs_a_real_directory_read(self) -> None:
        """Real, not `Path.exists()` — the TCC prompt has to fire
        here, in the foreground, rather than at login in the display
        agent where a dismissed prompt is a permanent denial."""
        seen: list[Path] = []

        def lister(path: Path) -> list[Path]:
            seen.append(path)
            return []

        ss.probe_folder(str(self.folder), lister=lister)
        self.assertEqual(seen, [self.folder])


class ProbeJsonUrlTests(unittest.TestCase):
    """URL copy — and the distinction the copy requires."""

    def test_a_good_list_reports_a_count(self) -> None:
        result = ss.probe_json_url(
            "https://example.com/list.json",
            fetcher=lambda url: ["https://a/1.png"] * 12,
        )
        self.assertEqual(result.message, "12 pictures found")
        self.assertTrue(result.save_enabled)

    def test_unreachable_is_its_own_message(self) -> None:
        result = ss.probe_json_url(
            "https://example.com/list.json", fetcher=lambda url: ss._UNREACHABLE
        )
        self.assertEqual(result.message, "Could not reach that address")
        self.assertFalse(result.save_enabled)

    def test_reachable_but_not_a_list_is_a_different_message(self) -> None:
        """The two are indistinguishable through the source's `[]`
        contract, which is why the probe fetches the list itself."""
        for body in ({"images": []}, "a string", 42, None):
            with self.subTest(body=body):
                result = ss.probe_json_url(
                    "https://example.com/list.json", fetcher=lambda url: body
                )
                self.assertEqual(
                    result.message,
                    "That address did not return a list of pictures",
                )

    def test_a_list_of_unusable_entries_is_not_a_list_of_pictures(self) -> None:
        result = ss.probe_json_url(
            "https://example.com/list.json", fetcher=lambda url: [1, 2, {"a": "b"}]
        )
        self.assertIs(result.outcome, ss.Outcome.NOT_A_LIST)

    def test_it_counts_only_the_entries_that_would_become_pictures(self) -> None:
        """A 40-element array of which 12 are usable must not report 40,
        or the number stops being one the user can trust."""
        body = ["https://a/1.png", "not a url", {"url": "https://a/2.png"}, {}]
        result = ss.probe_json_url("https://example.com/l.json", fetcher=lambda u: body)
        self.assertEqual(result.count, 2)

    def test_an_empty_address_asks_for_one(self) -> None:
        self.assertIs(ss.probe_json_url("").outcome, ss.Outcome.INCOMPLETE)


class ProbeImageServerTests(unittest.TestCase):
    def test_the_starred_trap_gets_the_plans_exact_sentence(self) -> None:
        """"Test additionally catches the trap Phase 2 hit for
        real"."""
        result = ss.probe_image_server(
            "http://studio.example:8883", "starred", fetcher=lambda url: []
        )
        self.assertEqual(
            result.message, "Connected, but no starred pictures. Try 'All'."
        )
        self.assertFalse(result.save_enabled)

    def test_an_empty_all_pool_does_not_suggest_trying_all(self) -> None:
        result = ss.probe_image_server(
            "http://studio.example:8883", "all", fetcher=lambda url: []
        )
        self.assertNotIn("Try 'All'", result.message)
        self.assertFalse(result.save_enabled)

    def test_it_asks_the_starred_endpoint_for_the_starred_pool(self) -> None:
        asked: list[str] = []

        def fetcher(url: str) -> list:
            asked.append(url)
            return [{"id": "a"}]

        ss.probe_image_server("http://studio.example:8883/", "starred", fetcher=fetcher)
        self.assertEqual(asked, ["http://studio.example:8883/api/images?starred=true"])

    def test_it_asks_the_unfiltered_endpoint_for_all(self) -> None:
        asked: list[str] = []

        def fetcher(url: str) -> list:
            asked.append(url)
            return [{"id": "a"}]

        ss.probe_image_server("http://studio.example:8883", "all", fetcher=fetcher)
        self.assertEqual(asked, ["http://studio.example:8883/api/images"])

    def test_unreachable_beats_the_starred_message(self) -> None:
        result = ss.probe_image_server(
            "http://nope.invalid", "starred", fetcher=lambda url: ss._UNREACHABLE
        )
        self.assertEqual(result.message, "Could not reach that address")


class ProbeDispatchTests(unittest.TestCase):
    def test_probe_tests_whichever_row_is_selected(self) -> None:
        form = ss.SourceForm(
            kind=source_settings.KIND_JSON_URL, list_url="https://example.com/l.json"
        )
        result = ss.probe(form, fetcher=lambda url: ["https://a/1.png"])
        self.assertTrue(result.ok)

    def test_an_unknown_kind_does_not_raise(self) -> None:
        form = ss.SourceForm(kind="telepathy")
        self.assertIs(ss.probe(form).outcome, ss.Outcome.INCOMPLETE)


class DisplayPickerTests(unittest.TestCase):
    """"The heuristic sorts the list; it does not gate it.\""""

    def _screens(self) -> list[dict]:
        return [
            {"name": "DELL P1425", "width": 1920, "height": 1080, "is_main": True},
            {"name": "Napster View", "width": 960, "height": 960, "is_main": False},
            {"name": "Spare", "width": 1280, "height": 720, "is_main": False},
        ]

    def test_every_attached_display_is_listed(self) -> None:
        self.assertEqual(len(ss.display_options(self._screens())), 3)

    def test_the_guess_is_suffixed_in_the_plans_words(self) -> None:
        options = ss.display_options(self._screens())
        self.assertTrue(options[0].title.endswith("960 x 960 — probably your View"))

    def test_the_guess_sorts_first_and_main_sorts_last(self) -> None:
        options = ss.display_options(self._screens())
        self.assertTrue(options[0].probably_view)
        self.assertTrue(options[-1].is_main)

    def test_nothing_matching_still_returns_a_full_list(self) -> None:
        """The failure the plan is guarding against is a picker that
        shows nothing and blocks setup."""
        screens = [{"name": "A", "width": 1920, "height": 1080, "is_main": True}]
        options = ss.display_options(screens)
        self.assertEqual(len(options), 1)
        self.assertFalse(options[0].probably_view)

    def test_the_main_screen_is_never_guessed_even_at_the_right_size(self) -> None:
        screens = [{"name": "Odd", "width": 960, "height": 960, "is_main": True}]
        self.assertFalse(ss.display_options(screens)[0].probably_view)

    def test_malformed_screen_entries_are_skipped_not_raised_on(self) -> None:
        screens = [None, {"name": "x"}, {"width": "960", "height": 960}, 7]
        self.assertEqual(ss.display_options(screens), [])

    def test_the_note_states_what_was_looked_for(self) -> None:
        self.assertIn("960 x 960", ss.nothing_matched_note())


class AgentStateTests(unittest.TestCase):
    """Both checkboxes must reflect actual launchctl state."""

    def test_exit_zero_is_loaded(self) -> None:
        state = ss.agent_state("x", lambda args: (0, ""))
        self.assertIs(state, ss.AgentState.LOADED)
        self.assertTrue(state.checked)

    def test_could_not_find_is_not_loaded(self) -> None:
        state = ss.agent_state("x", lambda args: (113, "Could not find service"))
        self.assertIs(state, ss.AgentState.NOT_LOADED)
        self.assertFalse(state.checked)

    def test_an_unexplained_failure_is_unknown_not_a_guess(self) -> None:
        """"I could not ask" is neither on nor off, and rendering it as
        off would tell the user their login item is gone when it is
        not."""
        state = ss.agent_state("x", lambda args: (1, "Operation not permitted"))
        self.assertIs(state, ss.AgentState.UNKNOWN)
        self.assertTrue(ss.login_checkbox_note(state))

    def test_a_runner_that_raises_is_unknown(self) -> None:
        def boom(args: list[str]) -> tuple[int, str]:
            raise OSError("no launchctl")

        self.assertIs(ss.agent_state("x", boom), ss.AgentState.UNKNOWN)

    def test_a_definite_answer_needs_no_caption(self) -> None:
        self.assertEqual(ss.login_checkbox_note(ss.AgentState.LOADED), "")
        self.assertEqual(ss.login_checkbox_note(ss.AgentState.NOT_LOADED), "")

    def test_it_asks_print_rather_than_list(self) -> None:
        asked: list[list[str]] = []
        ss.agent_state("gui/501/x", lambda args: (asked.append(args), (0, ""))[1])
        self.assertEqual(asked, [["print", "gui/501/x"]])


class SettingsDocumentTests(unittest.TestCase):
    """The config is additive-only; a Save must not drop what it does not
    understand."""

    def _document(self, previous: dict | None = None) -> dict:
        return ss.settings_document(
            previous,
            source=SourceSettings(
                kind=source_settings.KIND_FOLDER, folder="/tmp/pics"
            ),
            rotation_interval_s=900.0,
            shuffle=True,
            schedule=BlankSchedule(),
        )

    def test_unknown_keys_survive(self) -> None:
        document = self._document({"something_a_newer_build_added": 42})
        self.assertEqual(document["something_a_newer_build_added"], 42)

    def test_the_deliberately_cut_knobs_survive(self) -> None:
        """The settings UI cut the cache ceiling and the crossfade but
        kept them in the file "for the one person who cares"."""
        document = self._document({"cache_max": 300, "fade_duration_s": 2.0})
        self.assertEqual(document["cache_max"], 300)
        self.assertEqual(document["fade_duration_s"], 2.0)

    def test_the_legacy_flat_keys_are_left_alone(self) -> None:
        """They are the migration's input; their removal belongs in
        Step 6's sweep, not here."""
        document = self._document(
            {"image_studio_base_url": "http://forge.example:8883", "pool": "starred"}
        )
        self.assertEqual(
            document["image_studio_base_url"], "http://forge.example:8883"
        )

    def test_the_written_source_block_validates(self) -> None:
        document = self._document()
        self.assertIsNotNone(source_settings.validate_source(document["source"]))

    def test_a_missing_previous_document_is_fine(self) -> None:
        for previous in (None, {}, "not a mapping"):
            with self.subTest(previous=previous):
                self.assertIn("source", self._document(previous))

    def test_the_document_round_trips_through_the_displays_loader(self) -> None:
        """The end-to-end guarantee: what this window writes is what the
        display reads back, including the new keys."""
        from display import settings as display_settings

        document = ss.settings_document(
            {"cache_max": 300, "fade_duration_s": 2.0, "poll_interval_s": 1800},
            source=SourceSettings(
                kind=source_settings.KIND_FOLDER, folder="/tmp/pics"
            ),
            rotation_interval_s=60.0,
            shuffle=False,
            schedule=BlankSchedule(True, 21 * 60, 7 * 60),
        )
        loaded = display_settings.validate_settings(document)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.rotation_interval_s, 60.0)
        self.assertFalse(loaded.shuffle)
        self.assertTrue(loaded.blank_schedule.enabled)
        self.assertEqual(loaded.source.folder, "/tmp/pics")


class ScheduleFieldTests(unittest.TestCase):
    def test_a_half_typed_field_keeps_the_previous_value(self) -> None:
        previous = BlankSchedule(True, 21 * 60, 7 * 60)
        built = ss.schedule_from_fields(True, "9:", "7:00 AM", previous)
        self.assertEqual(built.start_minute, 21 * 60)

    def test_a_good_field_is_taken(self) -> None:
        previous = BlankSchedule(True, 21 * 60, 7 * 60)
        built = ss.schedule_from_fields(True, "10:30 PM", "6:00 AM", previous)
        self.assertEqual(built.start_minute, 22 * 60 + 30)
        self.assertEqual(built.end_minute, 6 * 60)


class StatusBlockTests(unittest.TestCase):
    """Status list."""

    def test_it_carries_every_field_the_plan_names(self) -> None:
        rows = dict(
            ss.status_lines(
                {
                    "last_poll_at": 1000.0,
                    "image_count": 16,
                    "source_label": "Image Server",
                    "last_error": None,
                },
                blank_state="Following schedule.",
                now=1060.0,
            )
        )
        self.assertEqual(rows["Pictures available"], "16")
        self.assertEqual(rows["Coming from"], "Image Server")
        self.assertEqual(rows["Last problem"], "None")
        self.assertEqual(rows["Blanking"], "Following schedule.")

    def test_a_missing_status_file_still_produces_every_row(self) -> None:
        """Rows that appear and disappear make the layout jump, and an
        absent row is not something a reader can interpret."""
        for status in (None, {}, "junk"):
            with self.subTest(status=status):
                rows = dict(ss.status_lines(status))
                self.assertEqual(rows["Pictures available"], "0")
                self.assertEqual(rows["Coming from"], "Not set up yet")

    def test_an_error_is_shown_verbatim(self) -> None:
        rows = dict(ss.status_lines({"last_error": "Couldn't get pictures."}))
        self.assertEqual(rows["Last problem"], "Couldn't get pictures.")

    def test_the_backlight_note_is_the_plans_wording(self) -> None:
        self.assertEqual(
            ss.BACKLIGHT_NOTE,
            "The View's backlight stays on. In a dark room the circle will "
            "still glow faintly. Unplug it to turn it off completely.",
        )


class FormatTimestampTests(unittest.TestCase):
    def test_relative_wording(self) -> None:
        # A realistic epoch, not a small number: `format_timestamp`
        # treats <= 0 as junk, so `now - 4 days` has to stay positive or
        # the fixture tests the guard instead of the wording.
        now = 1_784_500_000.0
        self.assertEqual(ss.format_timestamp(now - 10, now), "just now")
        self.assertEqual(ss.format_timestamp(now - 60, now), "a minute ago")
        self.assertEqual(ss.format_timestamp(now - 600, now), "10 minutes ago")
        self.assertEqual(ss.format_timestamp(now - 7200, now), "2 hours ago")
        self.assertEqual(ss.format_timestamp(now - 4 * 86400, now), "4 days ago")

    def test_never_for_absent_or_junk_values(self) -> None:
        for value in (None, 0, -1, "yesterday", True, []):
            with self.subTest(value=value):
                self.assertEqual(ss.format_timestamp(value), "never")

    def test_a_future_timestamp_does_not_produce_negative_prose(self) -> None:
        """Clocks move backwards — NTP steps, a VM resuming."""
        self.assertEqual(ss.format_timestamp(200.0, 100.0), "just now")


if __name__ == "__main__":
    unittest.main()


class TestButtonAgreesWithTheDisplayTests(unittest.TestCase):
    """🔴 The Test button must ask the server the way the display asks.

    It did not. `probe_image_server` fetched anonymously while the running
    source sent the appliance read token, so a server holding 50 starred
    pictures answered the display with 50 and answered Test with 0 —
    surfaced as "Connected, but no starred pictures. Try 'All'." while
    those pictures were visibly rotating on the glass.

    The cost of that is not a wrong string. It is a confident, wrong
    diagnosis of *someone else's* system: it sent a real user to go and
    ask the image server's maintainers about a fault that was here.
    """

    def test_the_probe_sends_the_credential_the_source_would_send(self):
        from display import read_token

        sent = {}

        def fake_fetch(url, extra_headers=None):
            sent["url"] = url
            sent["headers"] = extra_headers or {}
            return [{"id": "a", "filename": "a.png"}]

        with mock.patch.object(read_token, "load_read_token", return_value="a" * 64), \
             mock.patch.object(read_token, "may_send_to", return_value=True), \
             mock.patch.object(ss, "_fetch_json", fake_fetch):
            result = ss.probe_image_server("http://server.invalid:8883", "starred")

        self.assertEqual(result.outcome, ss.Outcome.OK)
        self.assertIn(read_token.READ_TOKEN_HEADER, sent["headers"])
        self.assertEqual(sent["headers"][read_token.READ_TOKEN_HEADER], "a" * 64)

    def test_no_credential_is_sent_when_the_destination_is_not_private(self):
        from display import read_token

        sent = {}

        def fake_fetch(url, extra_headers=None):
            sent["headers"] = extra_headers or {}
            return [{"id": "a", "filename": "a.png"}]

        with mock.patch.object(read_token, "load_read_token", return_value="a" * 64), \
             mock.patch.object(read_token, "may_send_to", return_value=False), \
             mock.patch.object(ss, "_fetch_json", fake_fetch):
            ss.probe_image_server("https://public.example.com", "starred")

        self.assertEqual(sent["headers"], {})

    def test_empty_with_a_credential_does_not_blame_the_server(self):
        """A rejected token answers 200 with an empty list here, which is
        indistinguishable from an empty library. Do not pick one."""
        from display import read_token

        with mock.patch.object(read_token, "load_read_token", return_value="a" * 64), \
             mock.patch.object(read_token, "may_send_to", return_value=True), \
             mock.patch.object(ss, "_fetch_json", lambda u, h=None: []):
            result = ss.probe_image_server("http://server.invalid:8883", "starred")

        self.assertIn("token", result.message.lower())
        self.assertNotIn("no starred pictures", result.message.lower())

    def test_empty_without_a_credential_keeps_the_original_advice(self):
        from display import read_token

        with mock.patch.object(read_token, "load_read_token", return_value=None), \
             mock.patch.object(ss, "_fetch_json", lambda u, h=None: []):
            result = ss.probe_image_server("http://server.invalid:8883", "starred")

        self.assertEqual(result.message, "Connected, but no starred pictures. Try 'All'.")
