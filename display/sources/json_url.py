"""`JsonUrlSource` — a web address that lists pictures.

**Contract** (stated in the settings panel's sub-label, because a contract
nobody is told about is a bug report waiting to happen): the URL must
return a JSON array of image URLs, either as bare strings

    ["https://example.com/a.png", "https://example.com/b.jpg"]

or as objects carrying a `url` field, so a list can also supply a label

    [{"url": "https://example.com/a.png", "label": "Sunset"}]

**The threat model is the whole design.** The user chose the list URL. The
user did *not* choose the URLs inside its response, and those go through
`sources.net.check_url()` one at a time with `allow_loopback=False` — see
that module's docstring for what each check is for and why. The RFC1918
exception is computed here, once, from whether the list URL itself
resolved private: a list on the LAN may reference LAN images; a list on
the public internet may not.

Everything else that comes back is untrusted too, so the list body is size
capped, the entry count is capped, and every image URL's bytes go through
`image_safety` before anything is written.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import PurePosixPath
from urllib.parse import urlsplit

import httpx

from display.sources import net
from display.sources.base import ImageRecord, ImageSource, make_display_label

logger = logging.getLogger(__name__)

POLL_INTERVAL_S = 1800.0
DEFAULT_TIMEOUT_S = 10.0

# The list body is JSON, not an image, so image_safety's cap does not
# apply to it. A few thousand URLs is well under a megabyte; 4MB is
# generous and still bounds a hostile endless-JSON response.
MAX_LIST_BYTES = 4 * 1024 * 1024

# Bound on entries taken from one response. Without it a list of a million
# URLs turns one poll into a million SSRF checks and downloads.
MAX_ENTRIES = 1000

_HEX = set("0123456789abcdefABCDEF")


def _label_for(url: str, supplied: object) -> str:
    """URL basename, "falling back when it is a hash".

    Content-addressed stores serve `f4c1...9a2.png`, and putting 40 hex
    characters in a menu bar is no better than the UUID this field exists
    to replace — so a stem that is long and entirely hexadecimal is
    treated as no label at all, and the host name is used instead."""
    if isinstance(supplied, str) and supplied.strip():
        return make_display_label(supplied)
    parts = urlsplit(url)
    stem = PurePosixPath(parts.path).stem
    looks_like_a_hash = len(stem) >= 16 and all(c in _HEX for c in stem)
    if not stem or looks_like_a_hash:
        return make_display_label(parts.hostname or "", fallback="Picture")
    return make_display_label(stem, fallback="Picture")


def _entry_url(entry: object) -> tuple[str, object] | None:
    """Normalize one list entry to (url, supplied_label). None if the
    entry is neither a string nor an object with a usable `url`."""
    if isinstance(entry, str):
        return entry, None
    if isinstance(entry, dict):
        url = entry.get("url")
        if isinstance(url, str) and url.strip():
            return url, entry.get("label")
    return None


class JsonUrlSource(ImageSource):
    kind = "json_url"
    caches = True
    poll_interval_s = POLL_INTERVAL_S

    def __init__(
        self,
        list_url: str,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        transport: httpx.BaseTransport | None = None,
        resolver: net.Resolver | None = None,
    ) -> None:
        self._list_url = list_url
        self._resolver = resolver
        self._client = net.make_client(timeout_s=timeout_s, transport=transport)

    def identity(self) -> str:
        return f"{self.kind}:{self._list_url}"

    @property
    def label(self) -> str:
        """The list's host, not the whole URL — a list URL can
        carry a long path and a query string, neither of which helps
        anyone identify it at a glance."""
        return urlsplit(self._list_url).hostname or self._list_url

    @property
    def list_url(self) -> str:
        return self._list_url

    # -- listing ---------------------------------------------------------

    def _fetch_list_body(self, checked: net.CheckedUrl) -> bytes | None:
        headers = {"Host": checked.host_header} if checked.host_header else {}
        try:
            with self._client.stream(
                "GET", checked.request_url, headers=headers
            ) as resp:
                if resp.status_code >= 300:
                    logger.warning(
                        "json_url: list URL returned HTTP %d (redirects are "
                        "disabled by policy).",
                        resp.status_code,
                    )
                    return None
                buffer = bytearray()
                for chunk in resp.iter_bytes():
                    buffer.extend(chunk)
                    if len(buffer) > MAX_LIST_BYTES:
                        logger.warning(
                            "json_url: list body exceeded %d bytes; abandoning.",
                            MAX_LIST_BYTES,
                        )
                        return None
                return bytes(buffer)
        except (httpx.TimeoutException, httpx.HTTPError, OSError) as exc:
            logger.warning("json_url: cannot fetch %s (%s).", self._list_url, exc)
            return None

    def list_images(self) -> list[ImageRecord]:
        """Fetch the list, then admit only those image URLs that survive
        every check. `[]` on any failure — never raises."""
        # The list URL is the user's own choice, so it may be on their LAN
        # or on their own machine. What it points *at* gets no such
        # latitude (see module docstring).
        checked_list = net.safe_url(
            self._list_url,
            allow_private=True,
            allow_loopback=True,
            resolver=self._resolver,
        )
        if checked_list is None:
            return []

        body = self._fetch_list_body(checked_list)
        if body is None:
            return []

        try:
            data = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.warning(
                "json_url: %s did not return valid JSON (%s). The contract is "
                "a JSON array of image URLs.",
                self._list_url,
                exc,
            )
            return []

        if not isinstance(data, list):
            logger.warning(
                "json_url: %s returned %s, not a JSON array of image URLs.",
                self._list_url,
                type(data).__name__,
            )
            return []

        # narrowly-scoped exception: private image addresses are
        # tolerated only because the list itself lives on a private
        # network. A list fetched from the public internet gets False here
        # and cannot reach a single RFC1918 address.
        allow_private = checked_list.is_private

        records: list[ImageRecord] = []
        seen: set[str] = set()
        for entry in data[:MAX_ENTRIES]:
            normalized = _entry_url(entry)
            if normalized is None:
                continue
            url, supplied_label = normalized
            checked = net.safe_url(
                url,
                allow_private=allow_private,
                allow_loopback=False,
                resolver=self._resolver,
            )
            if checked is None:
                continue  # safe_url already logged the concrete reason
            image_id = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
            if image_id in seen:
                continue
            seen.add(image_id)
            suffix = PurePosixPath(urlsplit(url).path).suffix.lower()
            if suffix not in (".png", ".jpg", ".jpeg"):
                suffix = ".png"
            records.append(
                ImageRecord(
                    id=image_id,
                    filename=f"{image_id}{suffix}",
                    display_label=_label_for(url, supplied_label),
                    locator=url,
                )
            )
        if len(data) > MAX_ENTRIES:
            logger.warning(
                "json_url: %s listed %d entries; using the first %d.",
                self._list_url,
                len(data),
                MAX_ENTRIES,
            )
        return records

    # -- bytes -----------------------------------------------------------

    def fetch(self, record: ImageRecord) -> bytes | None:
        """Re-check the URL immediately before fetching it, then stream
        it under `image_safety`'s cap and magic-byte checks.

        The re-check is not redundant with `list_images`: resolution
        happened there, the connection happens here, and re-resolving in
        between is what makes a DNS rebinding attack have to win twice.
        For plaintext HTTP it must win at a moment that no longer exists,
        because `check_url` pins the request to the address it validated."""
        if not record.locator:
            return None
        checked_list = net.safe_url(
            self._list_url,
            allow_private=True,
            allow_loopback=True,
            resolver=self._resolver,
        )
        allow_private = bool(checked_list and checked_list.is_private)
        checked = net.safe_url(
            record.locator,
            allow_private=allow_private,
            allow_loopback=False,
            resolver=self._resolver,
        )
        if checked is None:
            return None
        return net.fetch_checked(self._client, checked, label=record.locator)

    def close(self) -> None:
        try:
            self._client.close()
        except Exception as exc:  # noqa: BLE001 - close must never raise
            logger.warning("json_url: error closing client (%s).", exc)


__all__ = ["MAX_ENTRIES", "MAX_LIST_BYTES", "POLL_INTERVAL_S", "JsonUrlSource"]
