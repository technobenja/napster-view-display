"""The appliance read token — retrieval, and where it may be sent.

Some image servers restrict their listing to authenticated callers while
leaving the image bytes themselves open. That is the case this exists for:
an anonymous caller sees an empty list, so the *listing* call needs a
credential where it previously needed none, and the byte fetches do not
change at all. See `ImageServerClient.list_images`.

Three properties of this module are load-bearing:

**The token lives in the login keychain, never in either repo.** The
release repo is public. Reading it via `/usr/bin/security` (rather than a
file the app ships) means there is no path by which packaging can sweep it
into an artifact. The display agent runs in the `Aqua` launchd domain,
where the login keychain is unlocked `no-timeout` — verified on this
machine rather than assumed, because a background/SSH session in the same
account *cannot* read it and would have drawn the opposite conclusion.

**An empty token is never sent.** `""` is not a credential, and a header
present-but-empty is exactly the case the server's `hmac.compare_digest`
short-circuit exists to refuse. Returning None keeps the request
anonymous, which Phase 2's error state then reports honestly.

**The token is only ever sent to a private address.** The public origin
strips this header on all nine proxy locations, so sending it there would
not even work — but the reason to refuse is that a credential must not be
handed to a public host at all. This is the same runtime-guard idiom as
`net.FORBIDDEN_PATH_TOKEN`: cheap, and it fires on the case a reviewer
cannot see by reading the config.
"""

from __future__ import annotations

import logging
import re
import subprocess

from display.sources import net

logger = logging.getLogger(__name__)

#: The header the appliance token travels in. The server reads exactly
#: this name. Deliberately a read-only credential: a server may also have
#: a far more powerful service token, and a display appliance must never
#: carry one.
READ_TOKEN_HEADER = "X-OpenLab-Read"

KEYCHAIN_SERVICE = "viewlab-openlab-read"
KEYCHAIN_ACCOUNT = "openlab-read"

#: `openssl rand -hex 32`. Shape is checked to catch a mis-stored secret
#: early, but a mismatch is a warning rather than a refusal: the server is
#: the authority on its own token, and a 401 is a far clearer diagnosis
#: than this module silently declining to authenticate.
TOKEN_SHAPE_RE = re.compile(r"^[0-9a-fA-F]{64}$")

_LOOKUP_TIMEOUT_S = 5.0


def load_read_token(
    service: str = KEYCHAIN_SERVICE, account: str = KEYCHAIN_ACCOUNT
) -> str | None:
    """The token from the login keychain, or None if there isn't one.

    Never raises. A missing item is a normal, supported state — it means
    this install talks to a server that needs no credential — so it logs
    at debug, not warning.
    """
    try:
        completed = subprocess.run(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-a",
                account,
                "-s",
                service,
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=_LOOKUP_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("read_token: keychain lookup failed (%s).", exc)
        return None

    if completed.returncode != 0:
        logger.debug(
            "read_token: no keychain item %s/%s; requests stay anonymous.",
            service,
            account,
        )
        return None

    token = (completed.stdout or "").strip()
    if not token:
        # An empty secret is a real, observed failure mode: feeding
        # `security add-generic-password -w` a single line on stdin (it
        # prompts twice) stores a zero-length password and still exits 0.
        logger.warning(
            "read_token: keychain item %s/%s holds an empty secret; "
            "treating as absent.",
            service,
            account,
        )
        return None

    if not TOKEN_SHAPE_RE.match(token):
        logger.warning(
            "read_token: keychain secret is not the expected 64 hex "
            "characters (got %d chars); sending it anyway so the server "
            "can reject it explicitly.",
            len(token),
        )
    return token


def may_send_to(base_url: str) -> bool:
    """True when `base_url` resolves entirely to private addresses.

    A credential goes to the LAN appliance server or nowhere. `all`, not
    `any`, via `net.addresses_are_private`: a DNS answer mixing a private
    and a public address is a rebinding shape, not a private host.

    Resolution happens per call rather than once at construction. The
    display agent starts at login and may well come up before the network
    does; a construction-time answer would latch "public, don't send" for
    the life of the process and degrade into precisely the silent
    zero-row failure this work exists to remove.
    """
    # `safe_url` is the module's never-raises entry point and already
    # resolves every address, rejects credentials in the URL, and sets
    # `is_private` with the same `all`-not-`any` rule this needs.
    # `allow_private=True` because a private base URL is the expected,
    # correct case here — it is what we are testing *for*.
    checked = net.safe_url(base_url, allow_private=True, allow_loopback=True)
    if checked is None:
        return False
    # private OR loopback: a server the user runs on this Mac is as
    # entitled to the token as one on the LAN. `all`, so a mixed answer
    # (the rebinding shape) fails.
    local = bool(checked.addresses) and all(
        net.classify_address(a) in ("private", "loopback")
        for a in checked.addresses
    )
    if not local:
        logger.warning(
            "read_token: refusing to send %s to %r — it does not resolve "
            "to a private address. The appliance token is for the LAN "
            "server only; the public origin strips this header anyway.",
            READ_TOKEN_HEADER,
            checked.host,
        )
        return False
    return True


def listing_headers(base_url: str, token: str | None = None) -> dict[str, str]:
    """Auth headers for a **listing** request to `base_url`, or `{}`.

    The single decision point for "does the credential go out". It exists
    because there was briefly more than one: `ImageServerClient` sent the
    token and the Settings *Test* button did not, so the display showed 50
    pictures while Test reported "Connected, but no starred pictures" —
    two code paths asking the same server the same question and getting
    different answers, which is worse than either answer alone.

    Anything that lists images goes through here. Anything that fetches
    image bytes must not.
    """
    if token is None:
        token = load_read_token()
    if not token:
        return {}
    if not may_send_to(base_url):
        return {}
    return {READ_TOKEN_HEADER: token}


__all__ = [
    "KEYCHAIN_ACCOUNT",
    "KEYCHAIN_SERVICE",
    "READ_TOKEN_HEADER",
    "TOKEN_SHAPE_RE",
    "listing_headers",
    "load_read_token",
    "may_send_to",
]
