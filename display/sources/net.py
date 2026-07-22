"""Outbound URL checks for the HTTP sources.

Phase 2's hardening assumed Image Server was trusted same-lab
infrastructure. The hardening voids that assumption, and the gap it names is
sharper than "the user chose the URL":

**The list URL is chosen by the user. The image URLs inside its response
are not.** A trusted-looking `example.com/pictures.json` can answer with
`["http://localhost:11434/api/tags", "http://192.0.2.10:8883/..."]`. On the
machine this was written for that is pointed rather than theoretical:
Ollama listens on 11434, OB2 on 7423, seven dashboards on 5050-5057. That
distinction — user-chosen outer URL, attacker-chosen inner URLs — is the
entire reason this module exists.

What is enforced here:

- **Scheme allow-list**, `http`/`https`, stated explicitly rather than
  inherited from whatever a URL parser happens to tolerate.
- **No credentials, ever.** A URL carrying userinfo is rejected outright
  rather than stripped, because a list that *wants* to authenticate is a
  list doing something this app does not do.
- **`follow_redirects=False`, set explicitly.** It is httpx's default
  today, which makes it exactly the kind of security property that
  evaporates in a dependency bump nobody reads.
- **Address filtering after resolution.** Loopback and link-local are
  rejected with no exception; RFC1918 is rejected *unless* the list URL is
  itself on a private network, which preserves the LAN use case without
  handing an internet-hosted list a key to the user's subnet. Loopback
  gets no equivalent exception on purpose: a list served from 127.0.0.1
  has no legitimate need to pull images from *other* localhost ports, and
  those other ports are where the interesting things listen.
- **Every** resolved address must pass, not just the first — a DNS answer
  containing one public and one loopback address is a rebinding attempt,
  not a fallback.
- **Connection pinned to the validated address** for plaintext HTTP, which
  closes the check-then-connect window that makes DNS rebinding work at
  all. HTTPS keeps the hostname (pinning would break SNI and certificate
  validation); there the mitigation is re-resolution immediately before
  the request rather than a true pin.
- **A runtime guard on the generation endpoint.** GUARDRAILS.md forbids
  it because the ComfyUI queue behind Image Server is shared with Podcast
  Studio and Music Studio, and `test_no_generate_calls.py` enforces that by
  grepping source. A URL assembled at runtime out of a *network response*
  is invisible to a grep over source files. So the same rule is enforced
  again here, against the actual outbound URL, at the only moment it
  exists.
"""

from __future__ import annotations

import dataclasses
import ipaddress
import logging
import socket
from collections.abc import Callable, Sequence
from urllib.parse import urlsplit, urlunsplit

import httpx

from display import image_safety

logger = logging.getLogger(__name__)

ALLOWED_SCHEMES = frozenset({"http", "https"})

# GUARDRAILS.md endpoint, as a bare path token. The full literal is
# deliberately not spelled anywhere in display/*.py — that exact string is
# what test_no_generate_calls.py's structural tripwire greps for, and this
# module is the *runtime* half of the same guard, not a second copy of the
# string it is protecting.
FORBIDDEN_PATH_TOKEN = "generate"

DEFAULT_TIMEOUT_S = 10.0

#: host -> list of IP address strings.
Resolver = Callable[[str], Sequence[str]]


class UrlRejected(Exception):
    """A URL failed a check. Carries a human-readable reason so the
    the Test button can report the concrete failure rather than a
    generic one."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclasses.dataclass(frozen=True)
class CheckedUrl:
    """A URL that has passed every check, together with how to
    actually issue the request for it."""

    original: str
    scheme: str
    host: str
    port: int | None
    #: What to hand httpx. For plaintext HTTP this is the address-pinned
    #: form; for HTTPS it is the original URL (see module docstring).
    request_url: str
    #: Set when `request_url` is address-pinned, so the origin server
    #: still sees the name it was asked for.
    host_header: str | None
    addresses: tuple[str, ...]
    #: True when the resolved addresses are RFC1918/ULA.
    is_private: bool


# -- address classification -------------------------------------------


def _default_resolver(host: str) -> list[str]:
    """Resolve `host` to every address it maps to. Raises UrlRejected
    rather than socket.gaierror so callers have one failure type."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        raise UrlRejected(f"cannot resolve host {host!r} ({exc})") from exc
    addresses = []
    for info in infos:
        sockaddr = info[4]
        if sockaddr and isinstance(sockaddr[0], str):
            addresses.append(sockaddr[0])
    if not addresses:
        raise UrlRejected(f"host {host!r} resolved to no addresses")
    return addresses


def _normalize(address: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Parse an address, unwrapping IPv4-mapped IPv6 (`::ffff:127.0.0.1`
    is loopback, and a classifier that misses that is no classifier)."""
    # A scoped literal like fe80::1%en0 is still link-local; drop the
    # zone id so it parses rather than falling through as "unusable".
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def classify_address(address: str) -> str:
    """One of: loopback, link_local, private, public, unusable.

    Order matters — `ipaddress.is_private` is True for loopback and
    link-local as well, so those must be tested first or the RFC1918
    exception would silently cover them."""
    try:
        ip = _normalize(address)
    except ValueError:
        return "unusable"
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link_local"
    if ip.is_unspecified or ip.is_multicast or ip.is_reserved:
        return "unusable"
    if ip.is_private:
        return "private"
    return "public"


def addresses_are_private(addresses: Sequence[str]) -> bool:
    """True when every resolved address is RFC1918/ULA — the condition
    under which the list URL is 'itself on that network' and its images
    may also be private. Deliberately `all`, not `any`: a mixed
    answer is a rebinding shape, not a private host."""
    return bool(addresses) and all(
        classify_address(a) == "private" for a in addresses
    )


# -- the check ---------------------------------------------------------


def check_url(
    raw: str,
    *,
    allow_private: bool = False,
    allow_loopback: bool = False,
    resolver: Resolver | None = None,
) -> CheckedUrl:
    """Run every check against `raw`. Returns a `CheckedUrl` or
    raises `UrlRejected` with a concrete reason.

    `allow_private` is the narrowly-scoped RFC1918 exception and should
    only ever be True when the *list* URL itself resolved private.

    `allow_loopback` applies **only to the list URL itself**, which the
    user typed and may legitimately point at something they run locally.
    It must never be set for an image URL drawn out of a list response —
    that is precisely the case that gets no exception."""
    if not isinstance(raw, str) or not raw.strip():
        raise UrlRejected("empty URL")

    try:
        parts = urlsplit(raw.strip())
    except ValueError as exc:
        raise UrlRejected(f"unparseable URL ({exc})") from exc

    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UrlRejected(
            f"scheme {scheme!r} is not allowed (only {sorted(ALLOWED_SCHEMES)})"
        )

    if parts.username or parts.password:
        raise UrlRejected("URL carries credentials; this app never sends any")

    try:
        host = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise UrlRejected(f"invalid host or port ({exc})") from exc
    if not host:
        raise UrlRejected("URL has no host")

    # The runtime half of GUARDRAILS.md (see module docstring). Checked
    # against the *path*, and against the query too: a query
    # string is just as capable of triggering the endpoint, and there is
    # no legitimate image URL that needs the word there.
    if FORBIDDEN_PATH_TOKEN in parts.path.lower() or (
        FORBIDDEN_PATH_TOKEN in (parts.query or "").lower()
    ):
        raise UrlRejected(
            "URL targets a generation endpoint, which this app must never "
            "call (GUARDRAILS.md section 2: shared ComfyUI queue)"
        )

    resolve = resolver or _default_resolver
    try:
        addresses = list(resolve(host))
    except UrlRejected:
        raise
    except Exception as exc:  # noqa: BLE001 - an injected resolver is arbitrary
        raise UrlRejected(f"cannot resolve host {host!r} ({exc})") from exc
    if not addresses:
        raise UrlRejected(f"host {host!r} resolved to no addresses")

    # Every address, not just the one we would connect to: an answer
    # mixing a public and a loopback address is a rebinding attempt.
    for address in addresses:
        kind = classify_address(address)
        if kind == "loopback" and not allow_loopback:
            raise UrlRejected(
                f"{host!r} resolves to loopback ({address}); loopback is never "
                f"allowed for an image URL, even from a loopback list"
            )
        if kind == "link_local":
            raise UrlRejected(f"{host!r} resolves to a link-local address ({address})")
        if kind == "unusable":
            raise UrlRejected(f"{host!r} resolves to an unusable address ({address})")
        if kind == "private" and not allow_private:
            raise UrlRejected(
                f"{host!r} resolves to a private address ({address}); private "
                f"addresses are only allowed when the list URL is itself on a "
                f"private network"
            )

    is_private = addresses_are_private(addresses)

    # Pin plaintext HTTP to the address we just validated, closing the
    # check-then-connect window. HTTPS cannot be pinned this way without
    # breaking SNI and certificate validation, so it keeps the hostname
    # and relies on this check running immediately before the request.
    if scheme == "http":
        pinned_host = addresses[0]
        if ":" in pinned_host:  # IPv6 literal needs brackets in a URL
            pinned_host = f"[{pinned_host}]"
        netloc = pinned_host if port is None else f"{pinned_host}:{port}"
        request_url = urlunsplit(
            (scheme, netloc, parts.path, parts.query, "")
        )
        host_header = host if port is None else f"{host}:{port}"
    else:
        request_url = urlunsplit((scheme, parts.netloc, parts.path, parts.query, ""))
        host_header = None

    return CheckedUrl(
        original=raw,
        scheme=scheme,
        host=host,
        port=port,
        request_url=request_url,
        host_header=host_header,
        addresses=tuple(addresses),
        is_private=is_private,
    )


def safe_url(
    raw: str,
    *,
    allow_private: bool = False,
    allow_loopback: bool = False,
    resolver: Resolver | None = None,
) -> CheckedUrl | None:
    """`check_url` in this project's never-raises idiom: logs the reason
    and returns None instead of raising."""
    try:
        return check_url(
            raw,
            allow_private=allow_private,
            allow_loopback=allow_loopback,
            resolver=resolver,
        )
    except UrlRejected as exc:
        logger.warning("net: refusing %r — %s", raw, exc.reason)
        return None


# -- fetching ----------------------------------------------------------


def make_client(
    timeout_s: float = DEFAULT_TIMEOUT_S,
    transport: httpx.BaseTransport | None = None,
) -> httpx.Client:
    """The only place an httpx.Client is built for a stranger's URL.

    `follow_redirects=False` is set explicitly — a 302 to
    `http://127.0.0.1:11434/` would otherwise walk straight past every
    check above. `trust_env=False` keeps ambient proxy environment
    variables from redirecting or observing these requests."""
    return httpx.Client(
        timeout=timeout_s,
        follow_redirects=False,
        trust_env=False,
        transport=transport,
    )


def fetch_checked(
    client: httpx.Client, checked: CheckedUrl, label: str | None = None
) -> bytes | None:
    """Stream a validated URL through `image_safety.read_stream`, which
    applies the incremental 25MB cap, the first-chunk magic-byte check,
    and the pre-decode dimension bound. Returns None on any failure —
    never raises."""
    label = label or checked.original
    headers = {"Host": checked.host_header} if checked.host_header else {}
    try:
        with client.stream("GET", checked.request_url, headers=headers) as resp:
            if resp.status_code >= 300:
                # >= 300, not raise_for_status(): with follow_redirects
                # off, a 3xx is a redirect we are deliberately refusing to
                # follow, and it is not an error httpx would raise on.
                logger.warning(
                    "net: %s returned HTTP %d; not following (redirects are "
                    "disabled by policy).",
                    label,
                    resp.status_code,
                )
                return None
            return image_safety.read_stream(resp.iter_bytes(), label=label)
    except (httpx.TimeoutException, httpx.HTTPError, OSError) as exc:
        logger.warning("net: failed to fetch %s (%s).", label, exc)
        return None


__all__ = [
    "ALLOWED_SCHEMES",
    "DEFAULT_TIMEOUT_S",
    "CheckedUrl",
    "UrlRejected",
    "addresses_are_private",
    "check_url",
    "classify_address",
    "fetch_checked",
    "make_client",
    "safe_url",
]
