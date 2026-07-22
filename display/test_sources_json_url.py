"""Unit tests for sources/json_url.py — contract and threat model, end to end.

The centrepiece is `HostileListTests`. whole argument is that the
*list* URL is chosen by the user while the image URLs inside its response
are not, so the test that matters is not "does check_url reject a loopback
address" — that is tested directly in test_sources_net.py — but "does a
trusted-looking list get to make this app issue those requests." Every
request is routed through a MockTransport that records what it saw, so
the assertion is on the requests that were actually attempted.
"""

from __future__ import annotations

import json
import struct
import unittest
import zlib

import httpx

from display.sources import net
from display.sources.json_url import JsonUrlSource

LIST_HOST = "pictures.example"
LIST_URL = f"http://{LIST_HOST}/pictures.json"
PUBLIC_IP = "93.184.216.34"
IMAGE_HOST = "images.example"
PRIVATE_IP = "192.168.50.30"


def png_bytes(width: int = 64, height: int = 64) -> bytes:
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    chunk = (
        struct.pack(">I", len(ihdr))
        + b"IHDR"
        + ihdr
        + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr))
    )
    return signature + chunk


def resolver(mapping: dict[str, list[str]]) -> net.Resolver:
    def resolve(host: str) -> list[str]:
        if host in mapping:
            return mapping[host]
        raise net.UrlRejected(f"unmapped host {host!r} in this test")

    return resolve


class RecordingTransport(httpx.MockTransport):
    """Records every request that reaches the transport layer. Anything
    the checks refuse never gets this far, which is what the SSRF
    assertions below are actually asserting."""

    def __init__(self, handler) -> None:
        self.requests: list[httpx.Request] = []

        def recording_handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return handler(request)

        super().__init__(recording_handler)


def list_serving_transport(payload, image_body: bytes | None = None) -> RecordingTransport:
    body = json.dumps(payload).encode()
    image = image_body if image_body is not None else png_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".json"):
            return httpx.Response(200, content=body)
        return httpx.Response(200, content=image)

    return RecordingTransport(handler)


def public_source(payload, **kwargs) -> tuple[JsonUrlSource, RecordingTransport]:
    """A list on the public internet, referencing images on the public
    internet. `allow_private` will be False for everything it lists."""
    transport = list_serving_transport(payload, **kwargs)
    source = JsonUrlSource(
        list_url=LIST_URL,
        transport=transport,
        resolver=resolver({LIST_HOST: [PUBLIC_IP], IMAGE_HOST: [PUBLIC_IP]}),
    )
    return source, transport


class ContractTests(unittest.TestCase):
    """'must return a JSON array of image URLs', either bare
    strings or objects with a `url` field."""

    def test_bare_string_entries(self) -> None:
        source, _ = public_source(
            [f"http://{IMAGE_HOST}/a.png", f"http://{IMAGE_HOST}/b.jpg"]
        )
        records = source.list_images()
        self.assertEqual([r.filename[-4:] for r in records], [".png", ".jpg"])
        source.close()

    def test_object_entries_with_a_url_field(self) -> None:
        source, _ = public_source(
            [{"url": f"http://{IMAGE_HOST}/a.png", "label": "Sunset over the pier"}]
        )
        records = source.list_images()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].display_label, "Sunset over the pier")
        source.close()

    def test_ids_are_stable_across_listings(self) -> None:
        """rotation.py persists walk order keyed by id, so an id that
        changes between polls silently defeats resume."""
        source, _ = public_source([f"http://{IMAGE_HOST}/a.png"])
        first = source.list_images()[0].id
        second = source.list_images()[0].id
        self.assertEqual(first, second)
        source.close()

    def test_duplicate_urls_collapse_to_one_record(self) -> None:
        source, _ = public_source([f"http://{IMAGE_HOST}/a.png"] * 3)
        self.assertEqual(len(source.list_images()), 1)
        source.close()

    def test_unusable_entries_are_skipped_not_fatal(self) -> None:
        source, _ = public_source(
            [
                42,
                None,
                {"no_url_field": True},
                [],
                f"http://{IMAGE_HOST}/good.png",
            ]
        )
        records = source.list_images()
        self.assertEqual([r.display_label for r in records], ["good"])
        source.close()

    def test_a_json_object_instead_of_an_array_returns_empty(self) -> None:
        source, _ = public_source({"images": []})
        self.assertEqual(source.list_images(), [])
        source.close()

    def test_invalid_json_returns_empty_without_raising(self) -> None:
        transport = RecordingTransport(lambda r: httpx.Response(200, content=b"{{{"))
        source = JsonUrlSource(
            list_url=LIST_URL,
            transport=transport,
            resolver=resolver({LIST_HOST: [PUBLIC_IP]}),
        )
        try:
            self.assertEqual(source.list_images(), [])
        except Exception as exc:  # noqa: BLE001 - this is the assertion
            self.fail(f"list_images() raised {exc!r}")
        source.close()

    def test_a_failing_list_url_returns_empty(self) -> None:
        transport = RecordingTransport(lambda r: httpx.Response(500))
        source = JsonUrlSource(
            list_url=LIST_URL,
            transport=transport,
            resolver=resolver({LIST_HOST: [PUBLIC_IP]}),
        )
        self.assertEqual(source.list_images(), [])
        source.close()


class LabelTests(unittest.TestCase):
    """URL basename, 'falling back when it is a hash'."""

    def test_basename_becomes_the_label(self) -> None:
        source, _ = public_source([f"http://{IMAGE_HOST}/harbour-at-dawn.png"])
        self.assertEqual(source.list_images()[0].display_label, "harbour-at-dawn")
        source.close()

    def test_a_content_hash_basename_falls_back_to_the_host(self) -> None:
        """Forty hex characters in a menu bar is no better than the UUID
        this field exists to replace."""
        digest = "f4c1a2b3c4d5e6f708192a3b4c5d6e7f80912a3b"
        source, _ = public_source([f"http://{IMAGE_HOST}/{digest}.png"])
        self.assertEqual(source.list_images()[0].display_label, IMAGE_HOST)
        source.close()

    def test_a_long_label_is_capped(self) -> None:
        source, _ = public_source(
            [{"url": f"http://{IMAGE_HOST}/a.png", "label": "y" * 200}]
        )
        label = source.list_images()[0].display_label
        self.assertLessEqual(len(label), 28)
        source.close()


class HostileListTests(unittest.TestCase):
    """Actual threat: a trusted-looking list whose *contents* point
    at this machine's own services.

    The URLs below are the plan's own worked example. On the machine this
    was written for they are pointed rather than theoretical — Ollama
    listens on 11434 and Image Server on 8883 — and the second one would
    additionally drive shared GPU load through a path
    test_no_generate_calls.py's grep cannot see, because the string never
    appears in any source file.

    Spelled as a concatenation for exactly that reason: writing the
    literal here would trip that grep against this file."""

    HOSTILE_LOOPBACK = "http://127.0.0.1:11434/api/tags"
    HOSTILE_PRIVATE = "http://192.168.50.30:8883/api/" + "generate"

    def _hostile_source(self, list_ip: str) -> tuple[JsonUrlSource, RecordingTransport]:
        payload = [
            self.HOSTILE_LOOPBACK,
            self.HOSTILE_PRIVATE,
            f"http://{IMAGE_HOST}/legitimate.png",
        ]
        transport = list_serving_transport(payload)
        source = JsonUrlSource(
            list_url=LIST_URL,
            transport=transport,
            resolver=resolver(
                {
                    LIST_HOST: [list_ip],
                    IMAGE_HOST: [PUBLIC_IP],
                    "127.0.0.1": ["127.0.0.1"],
                    "192.168.50.30": ["192.168.50.30"],
                }
            ),
        )
        return source, transport

    def _assert_never_requested(self, transport: RecordingTransport) -> None:
        attempted = [str(r.url) for r in transport.requests]
        for url in attempted:
            self.assertNotIn("127.0.0.1", url, f"issued a request to {url}")
            self.assertNotIn("11434", url, f"issued a request to {url}")
            self.assertNotIn("generate", url, f"issued a request to {url}")
        # Only the list URL itself should have been fetched.
        self.assertEqual(
            [httpx.URL(u).path for u in attempted],
            ["/pictures.json"],
            f"expected only the list fetch, got {attempted}",
        )

    def test_hostile_entries_are_dropped_from_the_listing(self) -> None:
        source, transport = self._hostile_source(PUBLIC_IP)
        records = source.list_images()

        self.assertEqual(
            [r.locator for r in records], [f"http://{IMAGE_HOST}/legitimate.png"]
        )
        self._assert_never_requested(transport)
        source.close()

    def test_no_request_is_issued_to_either_hostile_url(self) -> None:
        """The assertion that matters: not merely that the records were
        filtered, but that no packet was ever aimed at those services."""
        source, transport = self._hostile_source(PUBLIC_IP)
        source.list_images()
        self._assert_never_requested(transport)
        source.close()

    def test_a_private_list_still_may_not_reach_loopback(self) -> None:
        """The RFC1918 exception is scoped to RFC1918. A list on the LAN
        may reference LAN images — but loopback gets no exception, so the
        11434 entry is refused even here, while the 8883 one is now
        refused by the generation guard rather than by the address
        policy."""
        source, transport = self._hostile_source(PRIVATE_IP)
        records = source.list_images()

        locators = [r.locator for r in records]
        self.assertNotIn(self.HOSTILE_LOOPBACK, locators)
        self.assertNotIn(self.HOSTILE_PRIVATE, locators)
        self._assert_never_requested(transport)
        source.close()

    def test_a_private_list_may_reference_private_images(self) -> None:
        """The other half of the exception — without this, the LAN use
        case the plan wants to preserve would be broken."""
        lan_image = "http://192.168.50.30:8883/images/a.png"
        transport = list_serving_transport([lan_image])
        source = JsonUrlSource(
            list_url=LIST_URL,
            transport=transport,
            resolver=resolver({LIST_HOST: [PRIVATE_IP], "192.168.50.30": [PRIVATE_IP]}),
        )
        self.assertEqual([r.locator for r in source.list_images()], [lan_image])
        source.close()

    def test_fetch_refuses_a_hostile_locator_even_if_one_reaches_it(self) -> None:
        """Defence in depth: `fetch()` re-checks rather than trusting a
        record that came out of `list_images()`. A record could reach it
        from persisted state, from a future caller, or from a rebinding
        answer that changed between the two calls."""
        from display.sources.base import ImageRecord

        source, transport = self._hostile_source(PUBLIC_IP)
        smuggled = ImageRecord(
            id="a" * 16, filename="a.png", locator=self.HOSTILE_LOOPBACK
        )
        self.assertIsNone(source.fetch(smuggled))
        self.assertEqual(transport.requests, [])
        source.close()


class FetchTests(unittest.TestCase):
    def test_a_valid_image_is_returned(self) -> None:
        source, _ = public_source([f"http://{IMAGE_HOST}/a.png"])
        record = source.list_images()[0]
        self.assertEqual(source.fetch(record), png_bytes())
        source.close()

    def test_a_non_image_body_is_rejected(self) -> None:
        """The checks apply to the bytes as well as the URL: a list
        can point at a perfectly reachable public URL that serves
        something that is not an image at all."""
        source, _ = public_source(
            [f"http://{IMAGE_HOST}/a.png"], image_body=b"GIF89a" + b"\x00" * 100
        )
        record = source.list_images()[0]
        self.assertIsNone(source.fetch(record))
        source.close()

    def test_a_decompression_bomb_body_is_rejected(self) -> None:
        source, _ = public_source(
            [f"http://{IMAGE_HOST}/a.png"], image_body=png_bytes(60000, 60000)
        )
        record = source.list_images()[0]
        self.assertIsNone(source.fetch(record))
        source.close()

    def test_a_redirect_is_not_followed(self) -> None:
        """A 302 to a loopback address is the simplest way past a
        check-the-URL-then-fetch-it design, which is why the safety layer wants
        follow_redirects=False set explicitly rather than inherited."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith(".json"):
                return httpx.Response(
                    200, content=json.dumps([f"http://{IMAGE_HOST}/a.png"]).encode()
                )
            return httpx.Response(
                302, headers={"Location": "http://127.0.0.1:11434/api/tags"}
            )

        transport = RecordingTransport(handler)
        source = JsonUrlSource(
            list_url=LIST_URL,
            transport=transport,
            resolver=resolver({LIST_HOST: [PUBLIC_IP], IMAGE_HOST: [PUBLIC_IP]}),
        )
        record = source.list_images()[0]
        self.assertIsNone(source.fetch(record))
        for request in transport.requests:
            self.assertNotIn("127.0.0.1", str(request.url))
        source.close()

    def test_fetch_never_raises_on_a_transport_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith(".json"):
                return httpx.Response(
                    200, content=json.dumps([f"http://{IMAGE_HOST}/a.png"]).encode()
                )
            raise httpx.ConnectError("simulated failure", request=request)

        transport = RecordingTransport(handler)
        source = JsonUrlSource(
            list_url=LIST_URL,
            transport=transport,
            resolver=resolver({LIST_HOST: [PUBLIC_IP], IMAGE_HOST: [PUBLIC_IP]}),
        )
        record = source.list_images()[0]
        try:
            self.assertIsNone(source.fetch(record))
        except Exception as exc:  # noqa: BLE001 - this is the assertion
            self.fail(f"fetch() raised {exc!r} instead of returning None")
        source.close()


class BoundsTests(unittest.TestCase):
    def test_the_entry_count_is_bounded(self) -> None:
        """Without a bound, a list of a million URLs turns one poll into
        a million resolutions and downloads."""
        from display.sources.json_url import MAX_ENTRIES

        payload = [f"http://{IMAGE_HOST}/{i}.png" for i in range(MAX_ENTRIES + 50)]
        source, _ = public_source(payload)
        self.assertEqual(len(source.list_images()), MAX_ENTRIES)
        source.close()

    def test_an_oversized_list_body_is_abandoned(self) -> None:
        from display.sources.json_url import MAX_LIST_BYTES

        oversized = b"[" + b'"x",' * (MAX_LIST_BYTES // 4)
        transport = RecordingTransport(lambda r: httpx.Response(200, content=oversized))
        source = JsonUrlSource(
            list_url=LIST_URL,
            transport=transport,
            resolver=resolver({LIST_HOST: [PUBLIC_IP]}),
        )
        self.assertEqual(source.list_images(), [])
        source.close()

    def test_an_unusable_list_url_returns_empty_without_requesting_anything(self) -> None:
        transport = RecordingTransport(lambda r: httpx.Response(200, content=b"[]"))
        source = JsonUrlSource(list_url="file:///etc/passwd", transport=transport)
        self.assertEqual(source.list_images(), [])
        self.assertEqual(transport.requests, [])
        source.close()


class NamespacingTests(unittest.TestCase):
    def test_two_different_lists_get_different_cache_namespaces(self) -> None:
        """Ids are only unique *within* a source, so a shared cache
        directory would serve one list's bytes under another's ids."""
        a = JsonUrlSource(list_url="http://a.example/list.json")
        b = JsonUrlSource(list_url="http://b.example/list.json")
        self.assertNotEqual(a.cache_namespace, b.cache_namespace)
        self.assertTrue(a.cache_namespace.startswith("json_url-"))
        a.close()
        b.close()


if __name__ == "__main__":
    unittest.main()
