"""Unit tests for image_pool.py — mock transport (success,
timeout, 5xx, malformed JSON, bounded retry), assert it never raises,
assert the starred=true default. Pure stdlib unittest + httpx's built-in
MockTransport (no live network).
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

import httpx

from display import image_pool
from display.image_pool import ImageServerClient, ImageServerClientConfig, _is_usable, record_from_api
from display.sources.base import MAX_ID_CHARS, ImageRecord, ListOutcome

BASE_URL = "http://images.example:8883"

GOOD_ROW = {
    "id": "aaa-1",
    "filename": "aaa-1.png",
    "width": 1024,
    "height": 1024,
    "style": "Minimalist",
    "prompt": "a quiet harbour at dawn, low fog",
    "starred": 1,
    "status": "complete",
    "error": None,
}
PENDING_ROW = {**GOOD_ROW, "id": "bbb-2", "status": "pending"}
ERRORED_ROW = {**GOOD_ROW, "id": "ccc-3", "status": "complete", "error": "OOM"}
UNKNOWN_STATUS_ROW = {**GOOD_ROW, "id": "ddd-4", "status": "generating"}
TRAVERSAL_ID_ROW = {**GOOD_ROW, "id": "../../../../tmp/evil"}


class IsUsableTests(unittest.TestCase):
    def test_complete_no_error_is_usable(self) -> None:
        self.assertTrue(_is_usable(GOOD_ROW))

    def test_pending_is_not_usable(self) -> None:
        self.assertFalse(_is_usable(PENDING_ROW))

    def test_complete_with_error_is_not_usable(self) -> None:
        self.assertFalse(_is_usable(ERRORED_ROW))

    def test_unknown_future_status_is_not_usable(self) -> None:
        """Allow-list behavior: a status this client has never seen before
        (not just a known "bad" one) must still be dropped."""
        self.assertFalse(_is_usable(UNKNOWN_STATUS_ROW))

    def test_path_traversal_shaped_id_is_not_usable(self) -> None:
        """Fix 1: cache.py derives on-disk filenames (and, once
        downloaded, a request URL) straight from `id` - a hostile id
        must never survive _is_usable()."""
        self.assertFalse(_is_usable(TRAVERSAL_ID_ROW))

    def test_non_string_id_is_not_usable(self) -> None:
        self.assertFalse(_is_usable({**GOOD_ROW, "id": 12345}))

    def test_missing_id_is_not_usable(self) -> None:
        row = {k: v for k, v in GOOD_ROW.items() if k != "id"}
        self.assertFalse(_is_usable(row))

    def test_non_dict_row_is_not_usable(self) -> None:
        """Fix 2: a malformed API response can hand back rows that
        aren't dicts at all (e.g. bare strings) - must degrade to
        "unusable," not raise."""
        self.assertFalse(_is_usable("not-a-dict"))
        self.assertFalse(_is_usable(None))
        self.assertFalse(_is_usable(["nested", "list"]))


class RecordFromApiTests(unittest.TestCase):
    """`starred`, `width` and `height` were dropped from the shared
    record — none of the three fed a decision, and all three were this
    API's vocabulary leaking into a record that now also describes a JPEG
    in someone's Pictures folder. What replaced them is `display_label`, which is the reason `prompt` was *not* dropped with
    them."""

    def test_dropped_fields_are_gone_from_the_shared_record(self) -> None:
        record = record_from_api(GOOD_ROW)
        for gone in ("starred", "width", "height"):
            self.assertFalse(
                hasattr(record, gone),
                f"{gone!r} was dropped in section 6.1 but is still on ImageRecord",
            )

    def test_display_label_comes_from_the_prompt(self) -> None:
        record = record_from_api(GOOD_ROW)
        self.assertTrue(record.display_label.startswith("a quiet harbour"))
        self.assertEqual(record.prompt, GOOD_ROW["prompt"])

    def test_display_label_is_capped_for_a_menu_bar(self) -> None:
        """3.4: a label goes in an NSStatusItem title. A 300-character
        prompt there is not a long label, it is a broken menu bar."""
        record = record_from_api({**GOOD_ROW, "prompt": "x" * 300})
        self.assertLessEqual(len(record.display_label), 28)
        self.assertTrue(record.display_label.endswith("..."))

    def test_display_label_falls_back_to_filename_without_a_prompt(self) -> None:
        row = {k: v for k, v in GOOD_ROW.items() if k != "prompt"}
        record = record_from_api(row)
        self.assertEqual(record.display_label, "aaa-1.png")

    def test_multiline_prompt_is_collapsed_to_one_line(self) -> None:
        record = record_from_api({**GOOD_ROW, "prompt": "a harbour\n\n  at dawn"})
        self.assertEqual(record.display_label, "a harbour at dawn")

    def test_style_defaults_to_empty_string_when_absent(self) -> None:
        row = {k: v for k, v in GOOD_ROW.items() if k != "style"}
        record = record_from_api(row)
        self.assertEqual(record.style, "")

    def test_style_defaults_to_empty_string_when_none(self) -> None:
        record = record_from_api({**GOOD_ROW, "style": None})
        self.assertEqual(record.style, "")

    def test_unbounded_id_is_not_usable(self) -> None:
        """6.2: the id character class was already an allow-list, but
        unbounded. An id is used to build an on-disk filename, so an
        unbounded one is an ENAMETOOLONG waiting for the first source that
        is not this API's UUIDs."""
        self.assertTrue(_is_usable({**GOOD_ROW, "id": "a" * MAX_ID_CHARS}))
        self.assertFalse(_is_usable({**GOOD_ROW, "id": "a" * (MAX_ID_CHARS + 1)}))


def _client_with_handler(handler, max_retries: int | None = None) -> ImageServerClient:
    """`max_retries=None` means "whatever the shipped default is" — which
    is the point of most of these tests after that default moved to
    zero. Pass an explicit value only to exercise the knob itself."""
    transport = httpx.MockTransport(handler)
    config = (
        ImageServerClientConfig(base_url=BASE_URL)
        if max_retries is None
        else ImageServerClientConfig(base_url=BASE_URL, max_retries=max_retries)
    )
    return ImageServerClient(config=config, transport=transport)


class ListImagesTests(unittest.TestCase):
    def test_success_filters_to_usable_rows(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[GOOD_ROW, PENDING_ROW, ERRORED_ROW])

        client = _client_with_handler(handler)
        records = client.list_images()
        self.assertEqual([r.id for r in records], ["aaa-1"])

    def test_default_pool_sends_starred_true(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json=[])

        client = _client_with_handler(handler)
        client.list_images()
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].url.params.get("starred"), "true")

    def test_pool_all_omits_starred_param(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json=[])

        transport = httpx.MockTransport(handler)
        client = ImageServerClient(
            config=ImageServerClientConfig(base_url=BASE_URL, pool="all"),
            transport=transport,
        )
        client.list_images()
        self.assertNotIn("starred", captured[0].url.params)

    def test_requests_only_api_images_path(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json=[])

        client = _client_with_handler(handler)
        client.list_images()
        self.assertEqual(captured[0].url.path, "/api/images")

    @mock.patch("display.image_pool.time.sleep", return_value=None)
    def test_timeout_makes_exactly_one_attempt_and_never_sleeps(
        self, mock_sleep
    ) -> None:
        """No in-poll retries, and above
        all **no main-thread sleep**.

        This poll runs off an NSTimer on the same thread as every menu
        action and every calibration nudge. The old behaviour was three
        sequential attempts at a 5s timeout with 1.5s of sleeps between
        them — up to ~16.5s during which the entire UI is frozen and
        indistinguishable from a hung process. The retry is now simply
        the next scheduled poll."""
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            raise httpx.TimeoutException("simulated timeout", request=request)

        client = _client_with_handler(handler)
        records = client.list_images()
        self.assertEqual(records, [])
        self.assertEqual(call_count, 1)
        mock_sleep.assert_not_called()

    def test_default_config_carries_no_retries(self) -> None:
        """The change is in the *default*, which is what app.py
        gets. A test that only exercised an explicitly-constructed
        config would keep passing if the default regressed."""
        self.assertEqual(image_pool.MAX_RETRIES, 0)
        self.assertEqual(
            image_pool.ImageServerClientConfig(base_url="http://example.test").max_retries,
            0,
        )

    @mock.patch("display.image_pool.time.sleep", return_value=None)
    def test_explicit_retries_still_back_off_for_a_non_main_thread_caller(
        self, mock_sleep
    ) -> None:
        """The knob survives the change — only its default moved.
        Kept because `max_retries` is part of `ImageServerClientConfig`'s
        public shape, and a future caller running off the main thread has
        a legitimate use for it."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("simulated timeout", request=request)

        client = _client_with_handler(handler, max_retries=2)
        client.list_images()
        sleeps = [call.args[0] for call in mock_sleep.call_args_list]
        self.assertEqual(sleeps, [0.5, 1.0])

    @mock.patch("display.image_pool.time.sleep", return_value=None)
    def test_server_error_makes_one_attempt_then_returns_empty(
        self, mock_sleep
    ) -> None:
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(500, text="internal error")

        client = _client_with_handler(handler)
        records = client.list_images()
        self.assertEqual(records, [])
        self.assertEqual(call_count, 1)
        mock_sleep.assert_not_called()

    def test_malformed_json_returns_empty_without_retry(self) -> None:
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(200, content=b"not json{{{")

        client = _client_with_handler(handler)
        records = client.list_images()
        self.assertEqual(records, [])
        self.assertEqual(call_count, 1)

    def test_unexpected_row_shape_returns_empty_without_raising(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[{"unexpected": "shape"}])

        client = _client_with_handler(handler)
        records = client.list_images()
        self.assertEqual(records, [])

    def test_non_dict_rows_in_list_are_dropped_without_raising(self) -> None:
        """Fix 2: a row that's a bare string (not a dict) must be
        filtered out by _is_usable's isinstance guard, not raise
        AttributeError out of the list comprehension."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[GOOD_ROW, "not-a-dict-row", 42])

        client = _client_with_handler(handler)
        try:
            records = client.list_images()
        except Exception as exc:  # noqa: BLE001 - this is the assertion
            self.fail(f"list_images() raised {exc!r} instead of skipping the row")
        self.assertEqual([r.id for r in records], ["aaa-1"])

    def test_non_list_top_level_response_returns_empty_without_raising(self) -> None:
        """A malformed API response can be a JSON object instead of a
        list (e.g. an error body like {"detail": "..."}). Iterating a
        dict yields its keys (strings), each of which _is_usable's
        isinstance(row, dict) guard rejects - so this already degrades
        to [] without needing a separate except clause. Confirms that
        behavior directly rather than assuming it."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"detail": "unexpected shape"})

        client = _client_with_handler(handler)
        try:
            records = client.list_images()
        except Exception as exc:  # noqa: BLE001 - this is the assertion
            self.fail(f"list_images() raised {exc!r} instead of returning []")
        self.assertEqual(records, [])

    def test_non_iterable_top_level_response_returns_empty_without_raising(self) -> None:
        """A JSON body of `null` decodes to Python None, which is not
        iterable at all - this must land in list_images()'s
        (ValueError, KeyError, TypeError) except clause, not escape it.
        (Constructed via `content=` rather than httpx's `json=` kwarg -
        the latter treats a literal None as "no body given" rather than
        serializing the JSON literal `null`.)"""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"null")

        client = _client_with_handler(handler)
        try:
            records = client.list_images()
        except Exception as exc:  # noqa: BLE001 - this is the assertion
            self.fail(f"list_images() raised {exc!r} instead of returning []")
        self.assertEqual(records, [])

    def test_a_transient_failure_recovers_on_the_next_poll(self) -> None:
        """The recovery path: the retry is the next
        *poll*, not the next attempt inside this one.

        Every other failure test exercises "fails every time". This one
        confirms the thing that makes dropping in-poll retries safe —
        that a transient 500 costs one poll interval, not the pool."""
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(500, text="internal error")
            return httpx.Response(200, json=[GOOD_ROW])

        client = _client_with_handler(handler)
        self.assertEqual(client.list_images(), [])  # this poll fails
        self.assertEqual(call_count, 1)

        records = client.list_images()  # the next poll succeeds
        self.assertEqual([r.id for r in records], ["aaa-1"])
        self.assertEqual(call_count, 2)

    def test_transport_error_never_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("simulated connection failure", request=request)

        client = _client_with_handler(handler)
        with mock.patch("display.image_pool.time.sleep", return_value=None):
            try:
                records = client.list_images()
            except Exception as exc:  # noqa: BLE001 - this is the assertion
                self.fail(f"list_images() raised {exc!r} instead of returning []")
        self.assertEqual(records, [])


class ListStatusTests(unittest.TestCase):
    """`last_status` — the diagnosis that lets the display say *why* it
    has nothing to show. `list_images()` still returns `[]` for all of
    these; that is the point of reporting the reason separately."""

    @staticmethod
    def _status_after(handler):
        client = _client_with_handler(handler)
        with mock.patch("display.image_pool.time.sleep", return_value=None):
            records = client.list_images()
        return records, client.last_status

    def test_success_reports_ok_and_the_row_count(self):
        records, status = self._status_after(
            lambda request: httpx.Response(200, json=[GOOD_ROW])
        )
        self.assertEqual(len(records), 1)
        self.assertIs(status.outcome, ListOutcome.OK)
        self.assertEqual(status.rows_returned, 1)
        self.assertTrue(status.ok)

    def test_401_reports_unauthorized_not_unreachable(self):
        """The distinction the whole feature turns on. `raise_for_status`
        collapses 401 and 503 into one exception type, so this has to be
        read before it fires."""
        records, status = self._status_after(
            lambda request: httpx.Response(401, json={"detail": "nope"})
        )
        self.assertEqual(records, [])
        self.assertIs(status.outcome, ListOutcome.UNAUTHORIZED)
        self.assertIn("401", status.detail)

    def test_403_also_reports_unauthorized(self):
        _records, status = self._status_after(
            lambda request: httpx.Response(403, json={"detail": "nope"})
        )
        self.assertIs(status.outcome, ListOutcome.UNAUTHORIZED)

    def test_500_reports_unreachable(self):
        _records, status = self._status_after(
            lambda request: httpx.Response(500, text="boom")
        )
        self.assertIs(status.outcome, ListOutcome.UNREACHABLE)

    def test_connection_failure_reports_unreachable(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("simulated", request=request)

        _records, status = self._status_after(handler)
        self.assertIs(status.outcome, ListOutcome.UNREACHABLE)
        self.assertEqual(status.detail, "ConnectError")

    def test_malformed_json_reports_unreachable(self):
        _records, status = self._status_after(
            lambda request: httpx.Response(200, text="not json at all")
        )
        self.assertIs(status.outcome, ListOutcome.UNREACHABLE)
        self.assertIn("malformed", status.detail)

    def test_empty_list_reports_empty_with_zero_rows(self):
        """The v2.0.0 tier bug's exact signature: 200, valid JSON, no
        rows. Must not read as unreachable."""
        _records, status = self._status_after(
            lambda request: httpx.Response(200, json=[])
        )
        self.assertIs(status.outcome, ListOutcome.EMPTY)
        self.assertEqual(status.rows_returned, 0)

    def test_rows_that_are_all_unusable_report_empty_with_a_row_count(self):
        """Reachable, authorised, 2 rows, none usable — a different
        problem from an empty pool, and the row count is what says so."""
        pending = dict(GOOD_ROW, status="generating")
        _records, status = self._status_after(
            lambda request: httpx.Response(200, json=[pending, pending])
        )
        self.assertIs(status.outcome, ListOutcome.EMPTY)
        self.assertEqual(status.rows_returned, 2)

    def test_status_starts_ok_before_any_poll(self):
        client = _client_with_handler(lambda request: httpx.Response(200, json=[]))
        self.assertIs(client.last_status.outcome, ListOutcome.OK)


if __name__ == "__main__":
    unittest.main()
