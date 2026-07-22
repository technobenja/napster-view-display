"""Image Server HTTP client — the transport layer beneath
`sources/image_server.py` rather than something the app talks to
directly.

Read-only. `GET /api/images` only — never `/api/images/search` (a
prompt-text search endpoint that returns `[]` on an empty query, not a
lister — a real bug caught in Phase 1) and never Image Server's
generation endpoint (GUARDRAILS.md: the ComfyUI queue behind Image
Studio is shared with Podcast Studio and Music Studio; this client's
surface area does not include it at all, by construction — see
test_no_generate_calls.py for the structural enforcement of that, and
`sources/net.py` for the runtime guard added on top of it).

Confirmed against a live server 2026-07-17: the "done" status this plan
originally assumed does not exist — the real value is "complete".
`_is_usable` is written as an allow-list on that exact value (not a
`!= "complete"` blocklist) so an unknown future status (pending, failed,
generating, ...) is dropped too, not accidentally treated as good.

**No default base URL.** the Image Server adapter must carry
no lab-specific defaults — it ships publicly as the worked example of a
real adapter, and a hardcoded hostname would be both useless to a
stranger and an identity leak. The base URL is now a required
argument with no fallback, and test_sources_image_server.py asserts that
no lab hostname reappears in either module.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from typing import Any

import httpx

from display.sources.base import ImageRecord, is_safe_id, make_display_label

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_S = 5.0

# **Zero in-poll retries.**
#
# This used to be `MAX_RETRIES = 2` with a 0.5s/1.0s backoff slept on the
# main thread. That was invisible for two phases because nothing else
# wanted the main thread; a failed poll is user-facing. The poll runs off an
# NSTimer on the same thread as every menu action and every calibration
# nudge, so the real cost of a retry is not the sleep — it is
# `1 + MAX_RETRIES` sequential HTTP attempts at a 5s timeout each, up to
# ~16.5s during which Next, Previous, Blank and the calibration ring are
# all frozen. "The controls are dead" is a worse failure than "this
# poll found nothing", and the app is *already* built to shrug off the
# latter (an empty list is treated as "no update this poll" and the
# last good frame stays up).
#
# So: one attempt per poll, and the retry is simply the next poll. The
# knob is kept (rather than deleted) because it is part of
# `ImageServerClientConfig`'s public shape and a caller with a genuine
# non-main-thread use may still want it; the *default* is what the poll
# changes. Nothing in the app passes it.
MAX_RETRIES = 0
RETRY_BACKOFF_BASE_S = 0.5  # only reachable via an explicit max_retries > 0

VALID_POOLS = frozenset({"starred", "all"})


def record_from_api(row: dict[str, Any]) -> ImageRecord:
    """Build the shared `ImageRecord` from one Image Server row.

    The shared record dropped `starred`, `width` and `height`: none fed a decision and
    all three were this API's vocabulary leaking into a record that now
    also describes a JPEG in someone's Pictures folder. `prompt` is kept
    for one reason — it is where this source's
    `display_label` comes from, and without it the menu bar would show a
    36-character UUID."""
    prompt = row.get("prompt") or ""
    filename = row["filename"]
    return ImageRecord(
        id=row["id"],
        filename=filename,
        display_label=make_display_label(prompt, fallback=filename),
        locator=filename,
        style=row.get("style") or "",
        prompt=prompt if isinstance(prompt, str) else "",
    )


def _is_usable(row: Any) -> bool:
    """Allow-list, not a blocklist: only rows confirmed complete and
    error-free make it into the pool.

    Also the enforcement point for two defensive checks, both dropping
    the row rather than raising:

    - `row` itself must be a dict. Image Server is trusted infra, but a
      malformed response (an error object, a bare string, ...) must
      degrade to "this row doesn't make the pool," not an AttributeError
      that escapes list_images()'s "never raises" guarantee.
    - `row["id"]` must be a safe, **bounded** id (added the 128-char
      bound to the pre-existing character allow-list) before this row is
      allowed anywhere near an on-disk filename derived from it.
    """
    if not isinstance(row, dict):
        return False
    if not is_safe_id(row.get("id")):
        return False
    return row.get("status") == "complete" and not row.get("error")


@dataclasses.dataclass
class ImageServerClientConfig:
    base_url: str  # required — see module docstring
    pool: str = "starred"  # "starred" | "all"
    timeout_s: float = REQUEST_TIMEOUT_S
    max_retries: int = MAX_RETRIES


class ImageServerClient:
    """Thin, defensive wrapper around Image Server's read-only
    `/api/images`. Never raises — network failure, a bad status code, or
    malformed JSON all just return `[]`, letting the caller fall back to
    its local cache."""

    def __init__(
        self,
        config: ImageServerClientConfig,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._config = config
        self._client = httpx.Client(
            base_url=config.base_url,
            timeout=config.timeout_s,
            # Explicit, not inherited from a library default that a
            # dependency bump could quietly change.
            follow_redirects=False,
            transport=transport,
        )

    @property
    def _params(self) -> dict[str, str]:
        if self._config.pool == "starred":
            return {"starred": "true"}
        return {}

    def list_images(self) -> list[ImageRecord]:
        """`GET /api/images`, filtered to usable rows via `_is_usable`.

        One attempt per call by default (see `MAX_RETRIES`): a
        timeout, 5xx or transport error returns `[]` immediately and the
        next scheduled poll is the retry. Never raises; `[]` also covers
        malformed data."""
        last_exc: Exception | None = None
        for attempt in range(self._config.max_retries + 1):
            try:
                resp = self._client.get("/api/images", params=self._params)
                resp.raise_for_status()
                rows = resp.json()
                return [record_from_api(row) for row in rows if _is_usable(row)]
            except (httpx.TimeoutException, httpx.HTTPStatusError, httpx.TransportError) as exc:
                last_exc = exc
                if attempt < self._config.max_retries:
                    time.sleep(RETRY_BACKOFF_BASE_S * (2**attempt))
            except (ValueError, KeyError, TypeError) as exc:
                # Malformed JSON, a non-iterable top-level body (e.g.
                # `rows` is None), or a row record_from_api() can't
                # coerce - retrying the same response won't help. Note
                # this is deliberately a wholesale failure: one bad row
                # (or a non-list top-level shape _is_usable's isinstance
                # guard already screens most of, see above) discards the
                # *entire* poll rather than returning a partial list, so
                # callers never see an inconsistent partial pool for one
                # malformed response.
                logger.warning("Image Server returned malformed data: %s", exc)
                return []
        logger.warning("Image Server list_images failed after retries: %s", last_exc)
        return []

    @property
    def http_client(self) -> httpx.Client:
        """Exposed so `sources/image_server.py` can reuse the same
        configured client (base URL, timeout) to download image bytes."""
        return self._client

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ImageServerClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


__all__ = [
    "MAX_RETRIES",
    "REQUEST_TIMEOUT_S",
    "RETRY_BACKOFF_BASE_S",
    "VALID_POOLS",
    "ImageRecord",
    "ImageServerClient",
    "ImageServerClientConfig",
    "record_from_api",
]
