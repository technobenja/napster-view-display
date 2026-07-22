"""Unit tests for sources/net.py — URL checks.

Everything here is offline: hostnames are resolved by an injected fake
resolver, so no test depends on DNS, on this machine's network, or on
anything actually listening on the ports it names.
"""

from __future__ import annotations

import unittest

from display.sources import net

# Stand-ins with no relationship to any real host.
PUBLIC_IP = "93.184.216.34"
PRIVATE_IP = "192.168.50.30"
LOOPBACK_IP = "127.0.0.1"


def resolver(mapping: dict[str, list[str]]) -> net.Resolver:
    """A resolver that answers from a table and refuses anything else, so
    a test can never silently fall through to real DNS."""

    def resolve(host: str) -> list[str]:
        if host in mapping:
            return mapping[host]
        raise net.UrlRejected(f"unmapped host {host!r} in this test")

    return resolve


PUBLIC_ONLY = resolver({"pictures.example": [PUBLIC_IP]})


class SchemeTests(unittest.TestCase):
    def test_http_and_https_are_allowed(self) -> None:
        for url in (
            "http://pictures.example/a.png",
            "https://pictures.example/a.png",
        ):
            self.assertIsNotNone(net.check_url(url, resolver=PUBLIC_ONLY))

    def test_every_other_scheme_is_refused(self) -> None:
        """An explicit allow-list, not a blocklist — `file://` and
        `data:` are the obvious ones, but the point is that a scheme
        nobody anticipated is refused by default."""
        for url in (
            "file:///etc/passwd",
            "ftp://pictures.example/a.png",
            "data:image/png;base64,iVBORw0KGgo=",
            "gopher://pictures.example/",
            "jar:http://pictures.example/a.zip!/a.png",
            "//pictures.example/a.png",  # scheme-relative
        ):
            with self.assertRaises(net.UrlRejected, msg=url) as ctx:
                net.check_url(url, resolver=PUBLIC_ONLY)
            self.assertIn("scheme", ctx.exception.reason)


class CredentialTests(unittest.TestCase):
    def test_a_url_carrying_credentials_is_refused(self) -> None:
        """Never send credentials. Refused outright rather than
        stripped — a list that *wants* to authenticate is asking for
        something this app does not do."""
        with self.assertRaises(net.UrlRejected) as ctx:
            net.check_url(
                "http://user:secret@pictures.example/a.png", resolver=PUBLIC_ONLY
            )
        self.assertIn("credentials", ctx.exception.reason)


class GenerateGuardTests(unittest.TestCase):
    """Runtime guard. GUARDRAILS.md forbids the generation
    endpoint because the ComfyUI queue behind Image Server is shared with
    Podcast Studio and Music Studio; test_no_generate_calls.py enforces
    that by grepping source, which by construction cannot see a URL
    assembled at runtime from a network response.

    The forbidden literal is spelled here as a concatenation on purpose —
    writing it out would trip that same grep against this file."""

    FORBIDDEN_PATH = "/api/" + "generate"

    def test_a_generation_url_is_refused_before_any_request(self) -> None:
        with self.assertRaises(net.UrlRejected) as ctx:
            net.check_url(
                f"http://pictures.example{self.FORBIDDEN_PATH}", resolver=PUBLIC_ONLY
            )
        self.assertIn("generation endpoint", ctx.exception.reason)

    def test_the_guard_is_not_fooled_by_case_or_position(self) -> None:
        for path in (
            "/api/GENERATE",
            "/v2/Generate/image",
            "/regenerate",  # substring match is deliberately broad
        ):
            with self.assertRaises(net.UrlRejected, msg=path):
                net.check_url(f"http://pictures.example{path}", resolver=PUBLIC_ONLY)

    def test_the_guard_also_covers_the_query_string(self) -> None:
        with self.assertRaises(net.UrlRejected):
            net.check_url(
                "http://pictures.example/api?action=generate", resolver=PUBLIC_ONLY
            )


class AddressClassificationTests(unittest.TestCase):
    def test_each_class_is_identified(self) -> None:
        cases = {
            "127.0.0.1": "loopback",
            "127.99.1.2": "loopback",
            "::1": "loopback",
            "169.254.1.1": "link_local",
            "fe80::1": "link_local",
            "192.168.50.30": "private",
            "192.168.1.5": "private",
            "172.16.0.9": "private",
            "fd00::1": "private",
            "93.184.216.34": "public",
            "0.0.0.0": "unusable",
            "224.0.0.1": "unusable",
            "not-an-address": "unusable",
        }
        for address, expected in cases.items():
            self.assertEqual(net.classify_address(address), expected, address)

    def test_ipv4_mapped_loopback_is_still_loopback(self) -> None:
        """A classifier that misses `::ffff:127.0.0.1` is no classifier —
        it is the exact shape that gets used to slip past one."""
        self.assertEqual(net.classify_address("::ffff:127.0.0.1"), "loopback")

    def test_a_scoped_link_local_literal_is_still_link_local(self) -> None:
        self.assertEqual(net.classify_address("fe80::1%en0"), "link_local")


class AddressPolicyTests(unittest.TestCase):
    def test_loopback_is_refused_with_no_exception(self) -> None:
        """Loopback gets no equivalent of the
        RFC1918 exception: a list served from 127.0.0.1 has no legitimate
        need to pull images from *other* localhost ports, and those other
        ports are where the interesting things listen."""
        r = resolver({"localhost": [LOOPBACK_IP]})
        for allow_private in (False, True):
            with self.assertRaises(net.UrlRejected) as ctx:
                net.check_url(
                    "http://localhost:11434/a.png",
                    allow_private=allow_private,
                    resolver=r,
                )
            self.assertIn("loopback", ctx.exception.reason)

    def test_link_local_is_refused(self) -> None:
        r = resolver({"metadata.example": ["169.254.169.254"]})
        with self.assertRaises(net.UrlRejected) as ctx:
            net.check_url(
                "http://metadata.example/latest/meta-data/",
                allow_private=True,
                resolver=r,
            )
        self.assertIn("link-local", ctx.exception.reason)

    def test_private_is_refused_when_the_list_is_not_private(self) -> None:
        r = resolver({"lan.example": [PRIVATE_IP]})
        with self.assertRaises(net.UrlRejected) as ctx:
            net.check_url("http://lan.example/a.png", allow_private=False, resolver=r)
        self.assertIn("private", ctx.exception.reason)

    def test_private_is_allowed_when_the_list_is_itself_private(self) -> None:
        """The scoped exception that preserves the LAN use case."""
        r = resolver({"lan.example": [PRIVATE_IP]})
        checked = net.check_url("http://lan.example/a.png", allow_private=True, resolver=r)
        self.assertTrue(checked.is_private)

    def test_a_mixed_answer_is_refused_even_though_one_address_is_fine(self) -> None:
        """A DNS answer containing one public and one loopback address is
        a rebinding attempt, not a fallback — so every address is checked,
        not just the one that would be connected to."""
        r = resolver({"rebind.example": [PUBLIC_IP, LOOPBACK_IP]})
        with self.assertRaises(net.UrlRejected) as ctx:
            net.check_url("http://rebind.example/a.png", resolver=r)
        self.assertIn("loopback", ctx.exception.reason)

    def test_an_unresolvable_host_is_refused_without_raising_gaierror(self) -> None:
        def failing(host: str) -> list[str]:
            raise OSError("simulated resolver failure")

        with self.assertRaises(net.UrlRejected) as ctx:
            net.check_url("http://nope.example/a.png", resolver=failing)
        self.assertIn("resolve", ctx.exception.reason)

    def test_a_host_resolving_to_nothing_is_refused(self) -> None:
        with self.assertRaises(net.UrlRejected):
            net.check_url("http://empty.example/a.png", resolver=resolver({"empty.example": []}))


class AddressPinningTests(unittest.TestCase):
    def test_plaintext_http_is_pinned_to_the_validated_address(self) -> None:
        """The check-then-connect window is what makes DNS rebinding
        work: validate a name, then let the stack re-resolve it at
        connect time to something else. Pinning the request to the
        address that was actually validated closes that window for
        plaintext HTTP."""
        checked = net.check_url("http://pictures.example/a.png", resolver=PUBLIC_ONLY)
        self.assertEqual(checked.request_url, f"http://{PUBLIC_IP}/a.png")
        self.assertEqual(checked.host_header, "pictures.example")

    def test_the_port_survives_pinning(self) -> None:
        checked = net.check_url("http://pictures.example:8080/a.png", resolver=PUBLIC_ONLY)
        self.assertEqual(checked.request_url, f"http://{PUBLIC_IP}:8080/a.png")
        self.assertEqual(checked.host_header, "pictures.example:8080")

    def test_https_keeps_the_hostname_so_tls_still_validates(self) -> None:
        """Pinning an HTTPS request to an address would break SNI and
        certificate validation — trading a rebinding window for a
        man-in-the-middle. The hostname stays; the mitigation there is
        re-checking immediately before the request."""
        checked = net.check_url("https://pictures.example/a.png", resolver=PUBLIC_ONLY)
        self.assertEqual(checked.request_url, "https://pictures.example/a.png")
        self.assertIsNone(checked.host_header)

    def test_the_fragment_is_dropped_from_the_request_url(self) -> None:
        checked = net.check_url("http://pictures.example/a.png#frag", resolver=PUBLIC_ONLY)
        self.assertNotIn("#", checked.request_url)


class SafeUrlTests(unittest.TestCase):
    def test_safe_url_returns_none_instead_of_raising(self) -> None:
        self.assertIsNone(net.safe_url("file:///etc/passwd", resolver=PUBLIC_ONLY))
        self.assertIsNotNone(
            net.safe_url("http://pictures.example/a.png", resolver=PUBLIC_ONLY)
        )

    def test_empty_and_malformed_input_never_raises(self) -> None:
        for junk in ("", "   ", "not a url", "http://", None):
            self.assertIsNone(net.safe_url(junk, resolver=PUBLIC_ONLY))


class ClientPolicyTests(unittest.TestCase):
    def test_redirects_are_disabled_explicitly(self) -> None:
        """The safety layer calls this out because it is httpx's default *today* —
        exactly the kind of security property that evaporates in a
        dependency bump nobody reads. A 302 to a loopback address would
        otherwise walk straight past every check in this module."""
        client = net.make_client()
        try:
            self.assertFalse(client.follow_redirects)
        finally:
            client.close()

    def test_ambient_proxy_environment_is_not_trusted(self) -> None:
        client = net.make_client()
        try:
            self.assertFalse(client.trust_env)
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
