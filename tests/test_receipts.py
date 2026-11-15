import unittest

from foia_requests.authorization import grant_role, outbox_rows
from foia_requests.config import Config
from foia_requests.dispatch import RateLimits, run_once
from foia_requests.fake_transport import FakeSender
from foia_requests.receipts import ReceiptError, reconcile_unknown, record_acknowledgment, record_provider_event
from tests.test_dispatch import NOW, prepare

LIMITS = RateLimits(per_destination_per_hour=10, global_per_hour=10)
EVIDENCE = "e" * 64


class ReceiptTests(unittest.TestCase):
    setUp = prepare

    def tearDown(self):
        self.ledger.close()
        self.folder.cleanup()

    def test_unknown_outcome_needs_evidence_and_is_not_resent(self):
        sender = FakeSender(["timeout"])
        attempt = run_once(self.ledger, sender, Config(), owner="w", now=NOW, limits=LIMITS)["attempt_id"]
        rid = sender.sent[0][0].request_id
        with self.assertRaises(ReceiptError):
            reconcile_unknown(self.ledger, attempt, "disp", "submitted", {"source": "provider_status"})
        with self.assertRaises(Exception):
            reconcile_unknown(self.ledger, attempt, "nobody", "submitted", {"source": "x", "reference": "y"})
        reconcile_unknown(self.ledger, attempt, "disp", "submitted",
                          {"source": "provider_status", "reference": "synthetic-status-1",
                           "provider_message_id": "fake-reconciled"})
        self.assertEqual(self.ledger.get(rid)["state"], "submitted")
        with self.assertRaises(ReceiptError):
            reconcile_unknown(self.ledger, attempt, "disp", "submitted", {"source": "x", "reference": "y"})
        self.assertEqual([m.request_id for m, _ in sender.sent].count(rid), 1)

    def test_crash_after_remote_acceptance_leaves_submitting_then_unknown(self):
        # Simulate a worker that committed `submitting` and died before recording the outcome.
        from foia_requests.authorization import begin_submission, claim_next
        attempt = claim_next(self.ledger, "w", NOW)
        message = begin_submission(self.ledger, attempt, "w", NOW)
        self.assertEqual(self.ledger.get(message.request_id)["state"], "submitting")
        self.assertNotEqual(claim_next(self.ledger, "w2", "2026-11-16T00:00:00Z"), attempt)
        reconcile_unknown(self.ledger, attempt, "disp", "delivery_failed",
                          {"source": "provider_status", "reference": "not found"})
        events = [e["to_state"] for e in self.ledger.events(message.request_id)][-2:]
        self.assertEqual(events, ["submission_unknown", "delivery_failed"])

    def test_duplicate_callbacks_are_idempotent_and_bounce_flags(self):
        sender = FakeSender()
        result = run_once(self.ledger, sender, Config(), owner="w", now=NOW, limits=LIMITS)
        provider_id = self.ledger.connection.execute("SELECT provider_message_id FROM receipts").fetchone()[0]
        rid = sender.sent[0][0].request_id
        args = dict(event_id="evt-1", provider_message_id=provider_id, event_type="delivered", payload={"x": 1})
        self.assertEqual(record_provider_event(self.ledger, **args), "recorded")
        self.assertEqual(record_provider_event(self.ledger, **args), "duplicate")
        with self.assertRaises(ReceiptError):
            record_provider_event(self.ledger, **dict(args, payload={"x": 2}))
        self.assertEqual(record_provider_event(self.ledger, event_id="evt-2", provider_message_id="other",
                                               event_type="delivered", payload={}), "unmatched")
        record_provider_event(self.ledger, event_id="evt-3", provider_message_id=provider_id, event_type="bounced",
                              payload={"reason": "synthetic"})
        self.assertEqual(self.ledger.get(rid)["state"], "submitted")  # transport event is not acknowledgment
        self.assertEqual(self.ledger.open_flags(rid)[0]["flag"], "provider_bounced_after_acceptance")
        self.assertEqual(result["status"], "submitted")

    def test_acknowledgment_requires_agency_evidence(self):
        sender = FakeSender()
        run_once(self.ledger, sender, Config(), owner="w", now=NOW, limits=LIMITS)
        rid = sender.sent[0][0].request_id
        grant_role(self.ledger, "rev", "reviewer", "owner:test")
        with self.assertRaises(ReceiptError):
            record_acknowledgment(self.ledger, rid, "rev", tracking_number="", evidence_sha256=EVIDENCE,
                                  source="agency_email")
        with self.assertRaises(ReceiptError):
            record_acknowledgment(self.ledger, rid, "rev", tracking_number="FOIA-1", evidence_sha256="nope",
                                  source="agency_email")
        self.assertEqual(record_acknowledgment(self.ledger, rid, "rev", tracking_number="FOIA-1",
                                               evidence_sha256=EVIDENCE, source="agency_email"), "recorded")
        self.assertEqual(record_acknowledgment(self.ledger, rid, "rev", tracking_number="FOIA-1",
                                               evidence_sha256=EVIDENCE, source="agency_email"), "duplicate")
        self.assertEqual(self.ledger.get(rid)["state"], "acknowledged")
        record_acknowledgment(self.ledger, rid, "rev", tracking_number="FOIA-2", evidence_sha256=EVIDENCE,
                              source="agency_portal")
        self.assertIn({"flag": "multiple_tracking_numbers", "detail": "FOIA-2"}, self.ledger.open_flags(rid))
        unsent = [r["request_id"] for r in outbox_rows(self.ledger) if r["state"] == "queued"][0]
        with self.assertRaises(ReceiptError):
            record_acknowledgment(self.ledger, unsent, "rev", tracking_number="X", evidence_sha256=EVIDENCE,
                                  source="agency_email")


if __name__ == "__main__":
    unittest.main()
