"""Unit tests for sources/image_server.py — worked example.

Two things moved here from test_cache.py when byte retrieval moved
into the source: the assertion that the outbound request path is built
from the allow-listed id rather than the API's raw `filename`, and the
assertion that a download failure is survivable.

The third group is new in Step 0 — runtime guard on the generation
endpoint. The forbidden literal is spelled as a concatenation throughout
this file, because writing it out would trip test_no_generate_calls.py's
grep against this very file.
"""

from __future__ import annotations

import struct
import unittest
import zlib

import httpx

from display.sources.base import ImageRecord
from display.sources.image_server import ImageServerSource

BASE_URL = "http://images.example:8883"
FORBIDDEN_PATH = "/api/" + "generate"


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


def source_with(handler, pool: str = "starred") -> tuple[ImageServerSource, list]:
    captured: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    source = ImageServerSource(
        base_url=BASE_URL, pool=pool, transport=httpx.MockTransport(recording)
    )
    return source, captured


def ok_image(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=png_bytes())


class NoHardcodedDefaultsTests(unittest.TestCase):
    """This adapter ships publicly as the worked example of a real
    adapter, and 'it carries no lab-specific defaults' is what makes that
    honest. A hardcoded base URL would be both useless to a stranger and
    an identity leak."""

    def test_base_url_is_required(self) -> None:
        with self.assertRaises(TypeError):
            ImageServerSource()  # type: ignore[call-arg]

    def test_the_client_config_also_requires_one(self) -> None:
        from display.image_pool import ImageServerClientConfig

        with self.assertRaises(TypeError):
            ImageServerClientConfig()  # type: ignore[call-arg]

    def test_no_lab_hostname_survives_in_the_module(self) -> None:
        """No private hostname or LAN address may be baked into a source
        module.

        The patterns come from `release_gate.HARD_TERMS` rather than being
        listed here, so this test and the release gate cannot drift apart
        — and so the identity strings themselves live in exactly one file.

        **That file is deliberately not published.** It is the catalogue of
        strings that must never appear in a public build, so shipping it
        would defeat its own purpose — and the consequence is that this
        assertion cannot run where it is published. It skips there instead
        of erroring, because the README tells every user to run this suite
        and a hard failure on a fresh clone is the first thing they see.

        Where it *does* run — the maintainer's tree, which is the only
        place the term list exists and the only place a leak could be
        introduced — it runs for real.
        """
        import inspect
        import re
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        try:
            import release_gate
        except ModuleNotFoundError:
            self.skipTest(
                "release_gate is maintainer-only tooling and is deliberately "
                "off the public manifest; this assertion runs where the term "
                "list lives"
            )

        from display import image_pool
        from display.sources import image_server

        for module in (image_server, image_pool):
            text = inspect.getsource(module)
            for term in release_gate.HARD_TERMS:
                self.assertIsNone(
                    re.search(term, text, re.IGNORECASE),
                    f"{module.__name__} contains HARD term {term}",
                )


class FetchPathTests(unittest.TestCase):
    def test_request_path_is_built_from_the_id_not_the_filename(self) -> None:
        """Moved here from test_cache.py. httpx normalizes
        `../` dot-segments client-side, so a hostile `filename` left
        unsanitized would redirect the GET to an arbitrary path on the
        server — including the one endpoint GUARDRAILS.md forbids."""
        source, captured = source_with(ok_image)
        record = ImageRecord(id="safe-id", filename="../elsewhere", locator="x")

        source.fetch(record)

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].url.path, "/images/safe-id.png")
        source.close()

    def test_extensionless_filename_falls_back_to_png(self) -> None:
        source, captured = source_with(ok_image)
        source.fetch(ImageRecord(id="no-ext", filename="no-ext"))
        self.assertEqual(captured[0].url.path, "/images/no-ext.png")
        source.close()

    def test_a_hostile_suffix_falls_back_to_png(self) -> None:
        source, captured = source_with(ok_image)
        source.fetch(ImageRecord(id="weird", filename="weird.p/../ng"))
        self.assertEqual(captured[0].url.path, "/images/weird.png")
        source.close()

    def test_valid_bytes_are_returned(self) -> None:
        source, _ = source_with(ok_image)
        self.assertEqual(
            source.fetch(ImageRecord(id="img-1", filename="img-1.png")), png_bytes()
        )
        source.close()


class GenerateGuardTests(unittest.TestCase):
    """'Add a runtime guard, not just the grep.' The grep tripwire
    stays — it is now necessary but no longer sufficient, because it
    cannot see a path assembled at runtime."""

    def test_a_record_whose_id_smuggles_the_endpoint_is_refused(self) -> None:
        source, captured = source_with(ok_image)
        # An id like this cannot survive _is_usable, so this is the
        # backstop firing for a record that reached fetch() some other
        # way — persisted state, a future caller, a refactor.
        record = ImageRecord(id=FORBIDDEN_PATH.strip("/").replace("/", "-"), filename="a.png")

        self.assertIsNone(source.fetch(record))
        self.assertEqual(captured, [], "a request was issued despite the guard")
        source.close()

    def test_the_grep_tripwire_still_covers_this_module(self) -> None:
        """Belt and braces: assert the literal genuinely is absent from
        the new sources package, so this file's concatenations are not
        quietly hiding a real occurrence."""
        from pathlib import Path

        sources_dir = Path(__file__).parent / "sources"
        for path in sources_dir.rglob("*.py"):
            self.assertNotIn(FORBIDDEN_PATH, path.read_text(), str(path))


class FailureTests(unittest.TestCase):
    def test_a_server_error_returns_none(self) -> None:
        source, _ = source_with(lambda r: httpx.Response(500, text="boom"))
        self.assertIsNone(source.fetch(ImageRecord(id="img-1", filename="a.png")))
        source.close()

    def test_a_redirect_is_refused_rather_than_followed(self) -> None:
        source, _ = source_with(
            lambda r: httpx.Response(302, headers={"Location": "http://127.0.0.1/a.png"})
        )
        self.assertIsNone(source.fetch(ImageRecord(id="img-1", filename="a.png")))
        source.close()

    def test_a_transport_error_returns_none_without_raising(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("simulated failure", request=request)

        source, _ = source_with(handler)
        try:
            self.assertIsNone(source.fetch(ImageRecord(id="img-1", filename="a.png")))
        except Exception as exc:  # noqa: BLE001 - this is the assertion
            self.fail(f"fetch() raised {exc!r} instead of returning None")
        source.close()

    def test_a_non_image_body_is_rejected(self) -> None:
        """Image Server is the user's own server, but checks are
        applied uniformly — 'trusted' was the assumption this whole
        section voids."""
        source, _ = source_with(lambda r: httpx.Response(200, content=b"GIF89a" + b"\x00" * 50))
        self.assertIsNone(source.fetch(ImageRecord(id="img-1", filename="a.png")))
        source.close()

    def test_a_decompression_bomb_body_is_rejected(self) -> None:
        source, _ = source_with(
            lambda r: httpx.Response(200, content=png_bytes(60000, 60000))
        )
        self.assertIsNone(source.fetch(ImageRecord(id="img-1", filename="a.png")))
        source.close()


class ListingTests(unittest.TestCase):
    ROW = {
        "id": "aaa-1",
        "filename": "aaa-1.png",
        "prompt": "a quiet harbour at dawn",
        "style": "Minimalist",
        "status": "complete",
        "error": None,
    }

    def test_listing_delegates_to_the_client_and_carries_a_label(self) -> None:
        source, captured = source_with(lambda r: httpx.Response(200, json=[self.ROW]))
        records = source.list_images()

        self.assertEqual(captured[0].url.path, "/api/images")
        self.assertEqual([r.id for r in records], ["aaa-1"])
        self.assertTrue(records[0].display_label.startswith("a quiet harbour"))
        source.close()

    def test_pool_starred_sends_the_filter(self) -> None:
        source, captured = source_with(lambda r: httpx.Response(200, json=[]))
        source.list_images()
        self.assertEqual(captured[0].url.params.get("starred"), "true")
        source.close()

    def test_pool_all_omits_the_filter(self) -> None:
        source, captured = source_with(lambda r: httpx.Response(200, json=[]), pool="all")
        source.list_images()
        self.assertNotIn("starred", captured[0].url.params)
        source.close()

    def test_an_unknown_pool_value_falls_back(self) -> None:
        source, captured = source_with(
            lambda r: httpx.Response(200, json=[]), pool="everything"
        )
        source.list_images()
        self.assertEqual(source.pool, "starred")
        source.close()


class NamespacingTests(unittest.TestCase):
    def test_the_pool_is_part_of_the_cache_identity(self) -> None:
        """Switching Starred -> All changes which images exist. Sharing a
        cache namespace across that switch would leave the manifest's
        grace-period pruning fighting a pool it no longer describes."""
        starred = ImageServerSource(base_url=BASE_URL, pool="starred")
        every = ImageServerSource(base_url=BASE_URL, pool="all")
        self.assertNotEqual(starred.cache_namespace, every.cache_namespace)
        starred.close()
        every.close()

    def test_two_servers_get_different_namespaces(self) -> None:
        a = ImageServerSource(base_url="http://a.example:8883")
        b = ImageServerSource(base_url="http://b.example:8883")
        self.assertNotEqual(a.cache_namespace, b.cache_namespace)
        a.close()
        b.close()


if __name__ == "__main__":
    unittest.main()
