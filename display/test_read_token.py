"""The appliance read token: retrieval, where it may be sent, and — the
one that actually protects something — that it reaches the listing call
and *only* the listing call.

`ImageServerClient.http_client` is shared with the image byte fetches by
design, so "the header is configured" and "the header is on the right
request" are different claims. These tests assert the second one against
recorded requests rather than against config, because the config-level
version of this test passes just as happily when the header is a client
default that leaks onto all 50 image GETs.

Pure stdlib unittest + httpx MockTransport, no live network and no
keychain, matching test_image_pool.py.
"""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import httpx

from display import read_token
from display.sources.image_server import ImageServerSource
from display.test_sources_image_server import png_bytes

TOKEN = "a" * 64

ROW = {
    "id": "img1",
    "filename": "img1.png",
    "prompt": "a test",
    "style": "Cinematic",
    "status": "complete",
    "error": None,
}

PNG = png_bytes()


class Recorder:
    """Captures every request so assertions can be made per-URL."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def _handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/api/images":
            return httpx.Response(200, json=[ROW])
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handler)

    def by_path(self, path: str) -> httpx.Request:
        matches = [r for r in self.requests if r.url.path == path]
        assert matches, f"no request for {path}: {[str(r.url) for r in self.requests]}"
        return matches[0]


def _source(recorder: Recorder, token: str | None = None, base_url: str = "http://server.invalid:8883"):
    return ImageServerSource(
        base_url=base_url, transport=recorder.transport(), read_token=token
    )


class HeaderPlacementTests(unittest.TestCase):
    """`may_send_to` is pinned True here so these exercise header
    placement rather than name resolution; DestinationGuardTests covers
    the real function."""

    def setUp(self) -> None:
        patcher = mock.patch.object(read_token, "may_send_to", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.rec = Recorder()

    def test_listing_call_carries_the_header(self):
        records = _source(self.rec, TOKEN).list_images()
        self.assertEqual(len(records), 1)
        self.assertEqual(
            self.rec.by_path("/api/images").headers[read_token.READ_TOKEN_HEADER],
            TOKEN,
        )

    def test_image_fetch_does_not_carry_the_header(self):
        """The regression that matters. The byte fetch reuses the same
        httpx client; a client-level default header would put the
        credential on every image GET, and this is the only test here
        that would notice."""
        source = _source(self.rec, TOKEN)
        records = source.list_images()
        self.assertIsNotNone(source.fetch(records[0]))

        image_request = self.rec.by_path("/images/img1.png")
        self.assertNotIn(read_token.READ_TOKEN_HEADER, image_request.headers)
        # The token must not appear under any header name at all.
        self.assertFalse(
            any(TOKEN in value for value in image_request.headers.values())
        )

    def test_no_header_when_token_absent(self):
        _source(self.rec).list_images()
        self.assertNotIn(
            read_token.READ_TOKEN_HEADER, self.rec.by_path("/api/images").headers
        )

    def test_empty_token_is_never_sent(self):
        """A present-but-empty header is the case the server's both-empty
        short-circuit exists to refuse; don't manufacture it."""
        for empty in ("", None):
            with self.subTest(token=empty):
                rec = Recorder()
                _source(rec, empty).list_images()
                self.assertNotIn(
                    read_token.READ_TOKEN_HEADER, rec.by_path("/api/images").headers
                )


class CredentialSentTests(unittest.TestCase):
    """`credential_sent` must track the header that was actually put on
    the wire, not merely whether a token was configured.

    The distinction is the whole point: when the guard withholds the
    header, the request really was anonymous, and reporting otherwise
    would make the display blame a token it never sent."""

    def _status(self, token, may_send):
        rec = Recorder()
        with mock.patch.object(read_token, "may_send_to", return_value=may_send):
            source = _source(rec, token)
            source.list_images()
            return source.last_status, rec.by_path("/api/images")

    def test_true_when_the_header_was_sent(self):
        status, request = self._status(TOKEN, True)
        self.assertIn(read_token.READ_TOKEN_HEADER, request.headers)
        self.assertTrue(status.credential_sent)

    def test_false_when_no_token_is_configured(self):
        status, request = self._status(None, True)
        self.assertNotIn(read_token.READ_TOKEN_HEADER, request.headers)
        self.assertFalse(status.credential_sent)

    def test_false_when_the_destination_guard_withheld_it(self):
        """Configured but not sent. Reporting True here would have the
        display accuse a token that never left the machine."""
        status, request = self._status(TOKEN, False)
        self.assertNotIn(read_token.READ_TOKEN_HEADER, request.headers)
        self.assertFalse(status.credential_sent)


class DestinationGuardTests(unittest.TestCase):
    RESOLUTIONS = {
        "lan.invalid": ["192.168.50.10"],
        "local.invalid": ["127.0.0.1"],
        "public.invalid": ["93.184.216.34"],
        "mixed.invalid": ["192.168.50.10", "93.184.216.34"],
    }

    def setUp(self) -> None:
        patcher = mock.patch.object(
            read_token.net,
            "_default_resolver",
            side_effect=lambda host: self.RESOLUTIONS[host],
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_private_and_loopback_are_allowed(self):
        self.assertTrue(read_token.may_send_to("http://lan.invalid:8883"))
        self.assertTrue(read_token.may_send_to("http://local.invalid:8883"))

    def test_public_is_refused(self):
        self.assertFalse(read_token.may_send_to("https://public.invalid"))

    def test_mixed_answer_is_refused(self):
        """A DNS answer mixing private and public is a rebinding shape,
        not a private host."""
        self.assertFalse(read_token.may_send_to("http://mixed.invalid:8883"))

    def test_empty_base_url_is_refused(self):
        self.assertFalse(read_token.may_send_to(""))

    def test_header_withheld_when_guard_refuses(self):
        rec = Recorder()
        with mock.patch.object(read_token, "may_send_to", return_value=False):
            _source(rec, TOKEN, base_url="https://public.example.com").list_images()
        self.assertNotIn(
            read_token.READ_TOKEN_HEADER, rec.by_path("/api/images").headers
        )


def _completed(stdout: str, returncode: int = 0):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=""
    )


class KeychainRetrievalTests(unittest.TestCase):
    def test_returns_the_secret(self):
        with mock.patch.object(subprocess, "run", return_value=_completed(TOKEN + "\n")):
            self.assertEqual(read_token.load_read_token(), TOKEN)

    def test_missing_item_is_none(self):
        with mock.patch.object(subprocess, "run", return_value=_completed("", 44)):
            self.assertIsNone(read_token.load_read_token())

    def test_empty_secret_is_none(self):
        """`security add-generic-password -w` prompts twice; feeding it
        once stores a zero-length secret and still exits 0. Observed, not
        hypothetical — it happened while provisioning this token."""
        with mock.patch.object(subprocess, "run", return_value=_completed("\n", 0)):
            self.assertIsNone(read_token.load_read_token())

    def test_survives_a_missing_security_binary(self):
        with mock.patch.object(subprocess, "run", side_effect=OSError("no such file")):
            self.assertIsNone(read_token.load_read_token())

    def test_survives_a_hung_lookup(self):
        with mock.patch.object(
            subprocess, "run", side_effect=subprocess.TimeoutExpired("security", 5.0)
        ):
            self.assertIsNone(read_token.load_read_token())

    def test_odd_shaped_secret_is_still_returned(self):
        """The server is the authority on its own token. A 401 is a
        clearer diagnosis than quietly declining to authenticate."""
        with mock.patch.object(
            subprocess, "run", return_value=_completed("not-hex-but-present")
        ):
            self.assertEqual(read_token.load_read_token(), "not-hex-but-present")


class NoTokenInTreeTests(unittest.TestCase):
    """The release gate enforces this on the published tree; this keeps
    it true in the dev tree, where the gate does not run."""

    def test_no_64_hex_literal_in_python_sources(self):
        root = Path(__file__).resolve().parent.parent
        pattern = re.compile(rb"(?<![0-9a-fA-F])[0-9a-f]{64}(?![0-9a-fA-F])")
        offenders = []
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts or ".venv" in path.parts:
                continue
            for match in pattern.finditer(path.read_bytes()):
                literal = match.group()
                # Fixtures built from one repeated character are not
                # secrets; a real token will not be.
                if len(set(literal)) <= 2:
                    continue
                offenders.append(f"{path.name}: {literal[:8].decode()}...")
        self.assertEqual(offenders, [], f"64-hex literals in the tree: {offenders}")


if __name__ == "__main__":
    unittest.main()
