"""What the glass should say when there is nothing good to show.

**This module is the reason the appliance token was chosen over a hub
JWT.** That JWT carried a 30-day TTL with no long-lived variant, so an
unattended wall display would have gone dark roughly monthly with no way
to say why. The objection was never "30 days is short" — it was that a
blank circle and a broken circle look identical from across a room. A
credential you cannot see fail is a credential you cannot operate.

So the rule here: **the display never fails silently.** Every state that
is not "a picture is up" produces something a person can read and act on,
and the three failures stay distinct because they have different fixes —
power-cycle the server, re-store the token, or go star some images.

AppKit-free on purpose (this project's architecture rule): all of the
decision lives here and is unit-tested without a window, and `window.py`
only knows how to paint the result.

## The grace period, and why it is not applied uniformly

A wall display that swaps a photograph for an error card on one dropped
packet is a display whose owner turns the feature off. So a transient
fault gets `GRACE_POLLS` chances before it takes the screen.

`UNAUTHORIZED` is deliberately exempt. A rejected credential is not
transient — it will still be rejected on the next poll and every poll
after, and waiting an extra 30-minute cycle to say so buys nothing while
the screen shows a stale picture that implies everything is fine. This
asymmetry is the whole design; a single uniform grace period would
reintroduce exactly the silent window this module exists to close.
"""

from __future__ import annotations

import dataclasses

from display.sources.base import ListOutcome, ListStatus

#: Consecutive polls with the same fault before it takes the screen.
#: Applies to UNREACHABLE and EMPTY; see the module docstring for why
#: UNAUTHORIZED bypasses it.
GRACE_POLLS = 2

#: Severity keys. `window.py` maps these to colours; keeping them as
#: strings rather than colour values is what lets this module stay
#: AppKit-free and unit-testable.
SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"


@dataclasses.dataclass(frozen=True)
class Notice:
    """One human-readable notice for the circular display.

    `headline` is sized to be legible on a 2.1-inch circle from a few feet
    away, so it is short by necessity. `detail` is the actionable half and
    is allowed to wrap.
    """

    kind: str
    headline: str
    detail: str
    severity: str

    def as_status_fields(self) -> dict[str, str]:
        """The same notice for status.json, so `tools/status.py` and the
        menu bar report exactly what the glass is showing rather than a
        second, drifting version of it."""
        return {
            "notice_kind": self.kind,
            "notice_headline": self.headline,
            "notice_detail": self.detail,
            "notice_severity": self.severity,
        }


def notice_for(
    status: ListStatus,
    *,
    consecutive: int,
    have_images: bool,
    source_label: str = "",
) -> Notice | None:
    """The notice to show, or None to show pictures as usual.

    `consecutive` counts how many polls in a row have now ended in
    `status.outcome` (1 on the first). `have_images` is whether the
    rotation currently has anything at all to display — it decides
    between "we are showing stale pictures" and "the circle is empty",
    which are different messages even for an identical fault.
    """
    where = source_label.strip() or "the image server"

    if status.outcome is ListOutcome.OK:
        return None

    if status.outcome is ListOutcome.UNAUTHORIZED:
        # No grace period. See the module docstring.
        return Notice(
            kind="unauthorized",
            headline="Not authorised",
            detail=(
                f"{where} rejected this display's access token. "
                f"The token is probably missing, wrong, or rotated — "
                f"re-store it in the keychain."
            ),
            severity=SEVERITY_ERROR,
        )

    # Below here the fault may be transient, so it has to earn the screen.
    if consecutive < GRACE_POLLS and have_images:
        return None

    if status.outcome is ListOutcome.UNREACHABLE:
        return Notice(
            kind="unreachable",
            headline="Can't reach the server",
            detail=(
                f"No answer from {where}. Check that it is powered on and "
                f"that this Mac is on the network."
            ),
            severity=SEVERITY_WARNING,
        )

    if status.outcome is ListOutcome.EMPTY:
        if status.credential_sent and not status.rows_returned:
            # 🔴 Measured, not assumed: a wrong token does NOT come back
            # 401. The server's check fails closed to the anonymous
            # tier, which answers 200 with zero rows — byte-identical to a
            # genuinely empty library. Confirmed against the live server
            # with a corrupted token: HTTP 200, 0 rows.
            #
            # So this message names both possibilities and puts the
            # credential first, because "authenticated and got nothing" is
            # the documented signature of a rejected token here, while a
            # truly empty starred pool is the rarer state on a display
            # that was working yesterday. Claiming either one as certain
            # would be inventing a diagnosis; the previous wording claimed
            # "this display is authorised", which was actively false at
            # the exact moment the token broke.
            return Notice(
                kind="empty_or_rejected",
                headline="No pictures",
                detail=(
                    f"{where} answered, but sent nothing. This display's "
                    f"access token may have been rejected — a rejected "
                    f"token looks exactly like an empty library here. "
                    f"Check the token first, then check that images are "
                    f"starred."
                ),
                severity=SEVERITY_WARNING,
            )
        if status.rows_returned:
            # Reachable, authorised, rows came back -- and every one was
            # unusable. Saying "no pictures" here would send someone to
            # look in the wrong place entirely.
            return Notice(
                kind="empty_unusable",
                headline="No usable pictures",
                detail=(
                    f"{where} returned {status.rows_returned} "
                    f"{'picture' if status.rows_returned == 1 else 'pictures'}, "
                    f"but none of them are finished and error-free yet."
                ),
                severity=SEVERITY_INFO,
            )
        # Reached only when no credential was sent, so "nothing matched"
        # is the whole truth here rather than a guess.
        return Notice(
            kind="empty",
            headline="No pictures yet",
            detail=(
                f"{where} is reachable, but nothing matched. Star some "
                f"images and they will appear here."
            ),
            severity=SEVERITY_INFO,
        )

    return None


__all__ = [
    "GRACE_POLLS",
    "SEVERITY_ERROR",
    "SEVERITY_INFO",
    "SEVERITY_WARNING",
    "Notice",
    "notice_for",
]
