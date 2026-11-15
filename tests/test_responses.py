import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from foia_requests.authorization import grant_role
from foia_requests.config import Config
from foia_requests.dispatch import RateLimits, run_once
from foia_requests.fake_transport import FakeSender
from foia_requests.receipts import record_acknowledgment
from foia_requests.responses import ResponseError, fulfillment_report, intake_response, open_tasks, sniff
from foia_requests.templates import load_template
from tests.test_dispatch import NOW, prepare

FIELDS = list(load_template().requested_fields)
LIMITS = RateLimits(per_destination_per_hour=10, global_per_hour=10)


def xlsx_bytes():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", "<workbook/>")
    return buffer.getvalue()


def zip_bomb():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("big.txt", b"0" * (5 * 1024 * 1024))
    return buffer.getvalue()


class ResponseTests(unittest.TestCase):
    def setUp(self):
        prepare(self)
        grant_role(self.ledger, "rev", "reviewer", "owner:test")
        sender = FakeSender()
        for _ in range(3):
            run_once(self.ledger, sender, Config(), owner="w", now=NOW, limits=LIMITS)
        self.sent = [m.request_id for m, _ in sender.sent]
        self.artifacts = Path(self.folder.name) / "artifacts"

    def tearDown(self):
        self.ledger.close()
        self.folder.cleanup()

    def period(self, rid):
        r = self.ledger.get(rid)
        return r["period_start"], r["period_end"]

    def intake(self, **kw):
        kw.setdefault("operator", "rev")
        kw.setdefault("received_at", "2026-12-01T00:00:00Z")
        kw.setdefault("artifact_dir", self.artifacts)
        return intake_response(self.ledger, **kw)

    def test_full_final_response_fulfills_and_duplicate_is_idempotent(self):
        rid = self.sent[0]
        start, end = self.period(rid)
        args = dict(request_id=rid, kind="final", attachments=[("log.xlsx", xlsx_bytes())],
                    coverage=[(start, end, FIELDS)], evidence=b"synthetic letter")
        result = self.intake(**args)
        self.assertEqual(result["request_state"], "fulfilled")
        self.assertEqual(self.intake(**args)["status"], "duplicate")
        self.assertEqual(self.ledger.get(rid)["state"], "fulfilled")
        stored = list(self.artifacts.iterdir())
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].stat().st_mode & 0o777, 0o600)

    def test_partial_coverage_does_not_close_all_periods(self):
        rid = self.sent[0]
        start, end = self.period(rid)
        result = self.intake(request_id=rid, kind="partial", coverage=[(start, start[:4] + "-06-30", FIELDS)])
        self.assertEqual(result["request_state"], "partially_fulfilled")
        self.assertEqual(result["fulfillment"]["uncovered_periods"], [[start[:4] + "-07-01", end]])
        self.assertEqual([t["kind"] for t in open_tasks(self.ledger, rid)], ["missing_coverage"])
        later = self.intake(request_id=rid, kind="final", received_at="2027-01-01T00:00:00Z",
                            coverage=[(start[:4] + "-07-01", end, FIELDS)])
        self.assertEqual(later["request_state"], "fulfilled")

    def test_missing_fields_keep_request_open(self):
        rid = self.sent[1]
        start, end = self.period(rid)
        result = self.intake(request_id=rid, kind="final", coverage=[(start, end, FIELDS[:3])])
        self.assertEqual(result["request_state"], "partially_fulfilled")
        self.assertEqual(result["fulfillment"]["fields_never_delivered"], sorted(FIELDS[3:]))

    def test_wrong_request_and_unmatched_responses_never_overwrite(self):
        a, b = self.sent[0], self.sent[1]
        record_acknowledgment(self.ledger, a, "rev", tracking_number="FOIA-A", evidence_sha256="e" * 64,
                              source="agency_email")
        with self.assertRaisesRegex(ResponseError, "different request"):
            self.intake(request_id=b, tracking_number="FOIA-A", kind="final")
        unmatched = self.intake(tracking_number="UNKNOWN-9", kind="final")
        self.assertEqual(unmatched["status"], "unmatched")
        self.assertEqual(self.ledger.get(b)["state"], "submitted")
        via_tracking = self.intake(tracking_number="FOIA-A", kind="interim")
        self.assertEqual(via_tracking["request_id"], a)
        self.assertEqual(self.ledger.get(a)["state"], "response_received")

    def test_malicious_attachments_quarantined(self):
        self.assertEqual(sniff(b"MZ\x90\x00rest")[1], "quarantined")
        self.assertEqual(sniff(b"#!/bin/sh\nrm -rf /")[1], "quarantined")
        self.assertEqual(sniff(zip_bomb())[2], "excessive expansion (possible zip bomb)")
        self.assertEqual(sniff(b"%PDF-1.7 ...")[1], "accepted")
        self.assertEqual(sniff(b"\x00\x01\x02binary")[1], "quarantined")
        rid = self.sent[0]
        self.intake(request_id=rid, kind="interim", attachments=[("x.exe", b"MZ\x90\x00")])
        self.assertEqual([t["kind"] for t in open_tasks(self.ledger, rid)], ["quarantined_attachment"])

    def test_referral_fee_denial_and_no_records(self):
        a, b, c = self.sent
        self.intake(request_id=a, kind="referral", referral_to=["fed:dhs/foia-unit"])
        self.assertEqual(self.ledger.connection.execute(
            "SELECT request_id, to_component_id FROM referrals").fetchall()[0][:], (a, "fed:dhs/foia-unit"))
        self.assertEqual(self.ledger.get(a)["state"], "response_received")
        with self.assertRaises(ResponseError):
            self.intake(request_id=a, kind="referral", received_at="2027-01-02T00:00:00Z")
        self.intake(request_id=b, kind="fee_notice", evidence=b"fee estimate $40")
        self.assertIn({"flag": "fee_pause", "detail": open_tasks(self.ledger, b)[0]["response_id"]},
                      self.ledger.open_flags(b))
        self.assertEqual(self.ledger.get(b)["state"], "response_received")
        self.assertEqual(self.intake(request_id=c, kind="denial")["request_state"], "closed_unfulfilled")
        self.assertIn("no appeal is filed automatically", open_tasks(self.ledger, c)[0]["detail"])

    def test_response_requires_sent_request_and_role(self):
        with self.assertRaises(Exception):
            self.intake(request_id=self.sent[0], kind="final", operator="nobody")
        report = fulfillment_report(self.ledger, self.sent[0])
        self.assertFalse(report["complete"])


if __name__ == "__main__":
    unittest.main()
