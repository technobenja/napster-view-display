"""`ImageServerSource` — a server that lists images over HTTP.

Kept in the public tree deliberately: it is the worked example of a real
adapter, which teaches more about writing one than an interface
description does. That only holds if it carries **no lab-specific
defaults**, so `base_url` is a required argument here and in
`image_pool.ImageServerClient` beneath it.

The settings panel's sub-label says the quiet part out loud: *"A server
that lists images over HTTP (advanced). Most people won't have one — pick
'A folder on this Mac' instead."*

Byte retrieval lives here rather than in `cache.py`. Two things that used
to be implicit are now explicit at this boundary:

- The requested filename is derived from the record's **id**, never from
  the API's raw `filename` field. httpx normalizes `../` client-side, so
  an unsanitized filename could turn an image GET into a request against
  an arbitrary path on the server — including the one generation endpoint
  GUARDRAILS.md forbids.
- That URL is then checked *again* at runtime by `sources.net`, a guard
  `test_no_generate_calls.py`'s grep structurally cannot provide: a grep
  over source files cannot see a path assembled from a network response.
"""

from __future__ import annotations

import logging
from pathlib import PurePosixPath
from urllib.parse import urlsplit

import httpx

from display import image_safety
from display.image_pool import (
    REQUEST_TIMEOUT_S,
    VALID_POOLS,
    ImageServerClient,
    ImageServerClientConfig,
)
from display.sources import net
from display.sources.base import ImageRecord, ImageSource, ListStatus

logger = logging.getLogger(__name__)

POLL_INTERVAL_S = 1800.0
DEFAULT_POOL = "starred"
IMAGES_PATH_PREFIX = "/images"


class ImageServerSource(ImageSource):
    kind = "image_server"
    caches = True
    poll_interval_s = POLL_INTERVAL_S

    def __init__(
        self,
        base_url: str,
        pool: str = DEFAULT_POOL,
        timeout_s: float = REQUEST_TIMEOUT_S,
        transport: httpx.BaseTransport | None = None,
        read_token: str | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._pool = pool if pool in VALID_POOLS else DEFAULT_POOL
        self._client = ImageServerClient(
            config=ImageServerClientConfig(
                base_url=self._base_url,
                pool=self._pool,
                timeout_s=timeout_s,
                # Injected, not read from the keychain in here: this class
                # ships publicly as the worked example of an adapter, and
                # a source that reaches for a credential on its own is a
                # worse example than one handed what it needs.
                read_token=read_token,
            ),
            transport=transport,
        )

    def identity(self) -> str:
        # The pool filter is part of the identity: switching Starred -> All
        # changes which images exist, and sharing a cache namespace across
        # that switch would leave the manifest's grace-period pruning
        # fighting a pool it no longer describes.
        return f"{self.kind}:{self._base_url}:{self._pool}"

    @property
    def label(self) -> str:
        """Host and pool — enough to tell two configured servers apart
        without putting a full URL in the menu bar."""
        host = urlsplit(self._base_url).hostname or self._base_url
        return f"Image Server ({host}, {self._pool})"

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def pool(self) -> str:
        return self._pool

    def list_images(self) -> list[ImageRecord]:
        return self._client.list_images()

    @property
    def last_status(self) -> ListStatus:
        """Delegated to the client, which is where the HTTP status code
        that distinguishes 401 from 503 actually exists."""
        return self._client.last_status

    def _image_path(self, record: ImageRecord) -> str:
        """`/images/<id><ext>` — the id-derived, safe-by-construction
        name, never the API's raw `filename`. The only variable component
        beyond the already-allow-listed id is a suffix, which by
        construction contains no path separator."""
        suffix = PurePosixPath(record.filename or "").suffix or ".png"
        if not suffix.replace(".", "").isalnum():
            suffix = ".png"
        return f"{IMAGES_PATH_PREFIX}/{record.id}{suffix}"

    def fetch(self, record: ImageRecord) -> bytes | None:
        """Download and validate one image. Never raises; returns None on
        a network failure, a non-2xx status, a redirect, a non-PNG/JPEG
        body, an oversized body, or oversized dimensions."""
        path = self._image_path(record)
        url = f"{self._base_url}{path}"

        # runtime guard. Image Server is the user's own server and
        # the path above is built from an allow-listed id, so this should
        # never fire — which is exactly why it is cheap to keep. It is the
        # backstop for the case the grep cannot see.
        if net.FORBIDDEN_PATH_TOKEN in path.lower():
            logger.error(
                "image_server: refusing to request %s — it targets a "
                "generation endpoint (GUARDRAILS.md section 2).",
                url,
            )
            return None

        try:
            with self._client.http_client.stream("GET", path) as resp:
                if resp.status_code >= 300:
                    logger.warning(
                        "image_server: %s returned HTTP %d.", url, resp.status_code
                    )
                    return None
                return image_safety.read_stream(resp.iter_bytes(), label=record.id)
        except (httpx.TimeoutException, httpx.HTTPError, OSError) as exc:
            logger.warning("image_server: failed to download %s (%s).", record.id, exc)
            return None

    def close(self) -> None:
        try:
            self._client.close()
        except Exception as exc:  # noqa: BLE001 - close must never raise
            logger.warning("image_server: error closing client (%s).", exc)


__all__ = ["DEFAULT_POOL", "IMAGES_PATH_PREFIX", "POLL_INTERVAL_S", "ImageServerSource"]
