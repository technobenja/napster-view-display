"""What the glass says when there is nothing good to show.

The property under test throughout is **no silent failure**: for every
non-OK outcome there is eventually a notice, and the three failures stay
distinguishable from each other. A test that only checked "a notice
appears" would pass on an implementation that showed the same useless
"something went wrong" for all three, which is the state this replaced.
"""

from __future__ import annotations

import unittest

from display import notice
from display.app import _poll_error_text
from display.notice import GRACE_POLLS, notice_for
from display.sources.base import ListOutcome, ListStatus

OK = ListStatus(outcome=ListOutcome.OK, rows_returned=50)
UNREACHABLE = ListStatus(outcome=ListOutcome.UNREACHABLE, detail="ConnectError")
UNAUTHORIZED = ListStatus(outcome=ListOutcome.UNAUTHORIZED, detail="HTTP 401")
EMPTY = ListStatus(outcome=ListOutcome.EMPTY, rows_returned=0)
EMPTY_UNUSABLE = ListStatus(outcome=ListOutcome.EMPTY, rows_returned=50)
#: The case that actually happens when the token is wrong — see
#: RejectedTokenTests.
EMPTY_WITH_CREDENTIAL = ListStatus(
    outcome=ListOutcome.EMPTY, rows_returned=0, credential_sent=True
)

ALL_FAILURES = (
    UNREACHABLE,
    UNAUTHORIZED,
    EMPTY,
    EMPTY_UNUSABLE,
    EMPTY_WITH_CREDENTIAL,
)


class HealthyTests(unittest.TestCase):
    def test_ok_shows_no_notice(self):
        self.assertIsNone(
            notice_for(OK, consecutive=1, have_images=True, source_label="the picture server")
        )

    def test_ok_shows_no_notice_even_with_an_empty_rotation(self):
        """A successful poll whose images have not finished downloading
        yet is not a fault; the existing charcoal empty-fill covers it."""
        self.assertIsNone(
            notice_for(OK, consecutive=9, have_images=False, source_label="the picture server")
        )


class NoSilentFailureTests(unittest.TestCase):
    """The core guarantee, asserted over every failure mode rather than
    one at a time — a new outcome added without a message would fail
    here."""

    def test_every_failure_eventually_produces_a_notice(self):
        for status in ALL_FAILURES:
            with self.subTest(outcome=status.outcome, rows=status.rows_returned):
                result = notice_for(
                    status,
                    consecutive=GRACE_POLLS,
                    have_images=True,
                    source_label="the picture server",
                )
                self.assertIsNotNone(result)
                self.assertTrue(result.headline.strip())
                self.assertTrue(result.detail.strip())

    def test_a_blank_rotation_never_waits_for_grace(self):
        """Nothing on screen plus a fault is the exact case that reads as
        'no images'. It must speak on the very first poll."""
        for status in ALL_FAILURES:
            with self.subTest(outcome=status.outcome, rows=status.rows_returned):
                self.assertIsNotNone(
                    notice_for(
                        status,
                        consecutive=1,
                        have_images=False,
                        source_label="the picture server",
                    )
                )

    def test_the_three_cases_are_distinguishable(self):
        """Different fixes, so different text. Identical wording would
        make the diagnosis worthless."""
        kinds = {
            notice_for(
                s, consecutive=GRACE_POLLS, have_images=True, source_label="the picture server"
            ).kind
            for s in ALL_FAILURES
        }
        self.assertEqual(len(kinds), len(ALL_FAILURES))


class GracePeriodTests(unittest.TestCase):
    def test_a_transient_fault_does_not_take_the_screen_immediately(self):
        """One dropped poll must not replace a photograph with an error
        card — that is how this feature gets switched off."""
        self.assertIsNone(
            notice_for(
                UNREACHABLE, consecutive=1, have_images=True, source_label="the picture server"
            )
        )

    def test_a_persistent_fault_does_take_the_screen(self):
        result = notice_for(
            UNREACHABLE,
            consecutive=GRACE_POLLS,
            have_images=True,
            source_label="the picture server",
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.kind, "unreachable")

    def test_unauthorized_bypasses_the_grace_period(self):
        """A rejected credential is not transient. It will be rejected
        again next poll, so waiting only prolongs a stale picture that
        implies everything is fine."""
        result = notice_for(
            UNAUTHORIZED, consecutive=1, have_images=True, source_label="the picture server"
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.kind, "unauthorized")
        self.assertEqual(result.severity, notice.SEVERITY_ERROR)


class WordingTests(unittest.TestCase):
    def _detail(self, status, **kw):
        kw.setdefault("consecutive", GRACE_POLLS)
        kw.setdefault("have_images", True)
        kw.setdefault("source_label", "Image Server (pictures.example, starred)")
        return notice_for(status, **kw).detail

    def test_unauthorized_points_at_the_token(self):
        self.assertIn("token", self._detail(UNAUTHORIZED).lower())

    def test_unreachable_points_at_the_network(self):
        text = self._detail(UNREACHABLE).lower()
        self.assertTrue("network" in text or "powered on" in text)

    def test_empty_points_at_starring_images(self):
        self.assertIn("star", self._detail(EMPTY).lower())

    def test_unusable_rows_do_not_claim_there_are_no_pictures(self):
        """Reachable + authorised + 50 rows, none usable. Saying 'no
        pictures' would send someone to look in entirely the wrong
        place."""
        result = notice_for(
            EMPTY_UNUSABLE,
            consecutive=GRACE_POLLS,
            have_images=False,
            source_label="the picture server",
        )
        self.assertEqual(result.kind, "empty_unusable")
        self.assertIn("50", result.detail)

    def test_source_label_is_used_when_present(self):
        self.assertIn("pictures.example", self._detail(UNREACHABLE))

    def test_missing_source_label_still_reads_as_english(self):
        for status in ALL_FAILURES:
            with self.subTest(outcome=status.outcome):
                detail = self._detail(status, source_label="")
                self.assertIn("the image server", detail)
                self.assertNotIn("  ", detail)


class RejectedTokenTests(unittest.TestCase):
    """🔴 The case the plan got wrong, pinned so it cannot regress.

    The plan's table said a bad credential shows up as `401`. Measured
    against the live server with a deliberately corrupted token: **HTTP
    200, zero rows**. The server's token check fails closed to the
    anonymous tier, and anonymous sees only public rows — of which there
    are none. So the response is byte-identical to a genuinely empty
    library, and the *only* thing separating the two is that this client
    knows it sent a token.

    Before this was caught, corrupting the token displayed "this display
    is authorised, but nothing matched" — confidently wrong, and it would
    send someone to go star images that were already starred.
    """

    def _notice(self, status):
        return notice_for(
            status,
            consecutive=GRACE_POLLS,
            have_images=False,
            source_label="the picture server",
        )

    def test_empty_with_a_credential_does_not_claim_authorisation(self):
        detail = self._notice(EMPTY_WITH_CREDENTIAL).detail.lower()
        self.assertNotIn("authorised", detail)
        self.assertNotIn("authorized", detail)

    def test_empty_with_a_credential_points_at_the_token_first(self):
        result = self._notice(EMPTY_WITH_CREDENTIAL)
        self.assertEqual(result.kind, "empty_or_rejected")
        self.assertIn("token", result.detail.lower())
        # It must still admit the other possibility rather than
        # over-claiming in the opposite direction.
        self.assertIn("starred", result.detail.lower())

    def test_it_is_not_merely_informational(self):
        """An `info` severity would read as 'nothing to do here'. A
        probably-broken credential is not that."""
        self.assertEqual(
            self._notice(EMPTY_WITH_CREDENTIAL).severity, notice.SEVERITY_WARNING
        )

    def test_anonymous_empty_still_reads_as_a_plain_empty_library(self):
        """With no credential sent there is no ambiguity to report, and
        hedging would be its own kind of noise."""
        result = self._notice(EMPTY)
        self.assertEqual(result.kind, "empty")
        self.assertNotIn("token", result.detail.lower())

    def test_the_two_empties_are_different_messages(self):
        self.assertNotEqual(
            self._notice(EMPTY).detail, self._notice(EMPTY_WITH_CREDENTIAL).detail
        )

    def test_poll_error_text_also_distinguishes_them(self):
        with_cred = _poll_error_text(EMPTY_WITH_CREDENTIAL, "the picture server")
        without = _poll_error_text(EMPTY, "the picture server")
        self.assertIn("token", with_cred.lower())
        self.assertNotIn("token", without.lower())


class StatusFieldTests(unittest.TestCase):
    def test_notice_serialises_for_status_json(self):
        result = notice_for(
            UNAUTHORIZED, consecutive=1, have_images=True, source_label="the picture server"
        )
        fields = result.as_status_fields()
        self.assertEqual(fields["notice_kind"], "unauthorized")
        self.assertEqual(fields["notice_severity"], notice.SEVERITY_ERROR)
        self.assertEqual(fields["notice_headline"], result.headline)
        self.assertEqual(fields["notice_detail"], result.detail)


class PollErrorTextTests(unittest.TestCase):
    """`last_error` in status.json — the menu bar's channel, distinct
    from the glass."""

    def test_each_outcome_gets_its_own_sentence(self):
        texts = {
            _poll_error_text(s, "the picture server") for s in ALL_FAILURES
        }
        self.assertEqual(len(texts), len(ALL_FAILURES))

    def test_unauthorized_mentions_the_token(self):
        self.assertIn("token", _poll_error_text(UNAUTHORIZED, "the picture server").lower())

    def test_a_source_without_an_outcome_keeps_the_old_hedge(self):
        """A third-party source that reports nothing must not have a
        diagnosis invented for it."""

        class Bare:
            pass

        text = _poll_error_text(Bare(), "somewhere")
        self.assertIn("may be unreachable", text)


if __name__ == "__main__":
    unittest.main()
