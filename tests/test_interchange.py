import json
import tempfile
import unittest
from pathlib import Path

from foia_requests.authorization import grant_role
from foia_requests.config import Config
from foia_requests.dispatch import RateLimits, run_once
from foia_requests.fake_transport import FakeSender
from foia_requests.interchange import (InterchangeError, delivery_status, export_bundle, import_bundle,
                                      record_ack)
from foia_requests.responses import intake_response
from foia_requests.templates import load_template
from tests.test_dispatch import NOW, prepare
from tests.test_responses import xlsx_bytes

FIELDS = list(load_template().requested_fields)


class InterchangeTests(unittest.TestCase):
    def setUp(self):
        prepare(self)
        grant_role(self.ledger, "rev", "reviewer", "owner:test")
        sender = FakeSender()
        for _ in range(3):
            run_once(self.ledger, sender, Config(), owner="w", now=NOW,
                     limits=RateLimits(per_destination_per_hour=10, global_per_hour=10))
        rid = sender.sent[0][0].request_id
        request = self.ledger.get(rid)
        self.artifacts = Path(self.folder.name) / "artifacts"
        intake_response(self.ledger, operator="rev", kind="final", received_at="2026-12-01T00:00:00Z",
                        artifact_dir=self.artifacts, request_id=rid,
                        attachments=[("log.xlsx", xlsx_bytes()), ("evil.exe", b"MZ\x90")],
                        coverage=[(request["period_start"], request["period_end"], FIELDS)])
        self.out = Path(self.folder.name) / "bundle"

    def tearDown(self):
        self.ledger.close()
        self.folder.cleanup()

    def export(self, **kw):
        return export_bundle(self.ledger, self.out, artifact_dir=self.artifacts,
                             generated_at="2026-12-02T00:00:00Z", **kw)

    def test_round_trip_preserves_provenance_and_excludes_private_data(self):
        result = self.export(blocked_markers=["requester@example.invalid", "Test Operator", "requester:synthetic"])
        manifest, loaded = import_bundle(self.out)
        self.assertEqual(manifest["counts"], {"components": 7, "requests": 3, "fulfilled_logs": 1})
        log = loaded["fulfilled_logs.jsonl"][0]
        self.assertEqual(log["response_kind"], "final")
        self.assertEqual(log["coverage"][0]["fields"], sorted(FIELDS))
        self.assertTrue((self.out / "artifacts" / log["sha256"]).exists())
        self.assertEqual(len(list((self.out / "artifacts").iterdir())), 1)  # quarantined file excluded
        text = "".join(p.read_text(errors="ignore") for p in self.out.glob("*.jsonl"))
        self.assertNotIn("requester", text.replace("request_id", ""))
        self.assertEqual(manifest["request_states"], {"fulfilled": 1, "submitted": 2})
        self.assertEqual(len(result["bundle_sha256"]), 64)

    def test_privacy_guard_aborts_export(self):
        with self.assertRaises(InterchangeError):
            self.export(blocked_markers=["fed:doj/oip"])
        self.assertFalse(self.out.exists())

    def test_tamper_detected(self):
        self.export()
        path = self.out / "requests.jsonl"
        path.write_text(path.read_text().replace("submitted", "fulfilled"))
        with self.assertRaisesRegex(InterchangeError, "SHA-256"):
            import_bundle(self.out)

    def test_idempotent_ack_and_explicit_unavailable_state(self):
        bundle = self.export()["bundle_sha256"]
        self.assertEqual(delivery_status(self.ledger, bundle)["status"], "awaiting_downstream")
        self.assertEqual(record_ack(self.ledger, bundle, consumer="FOIA_Aggregator", ack_id="ack-1",
                                    status="accepted"), "recorded")
        self.assertEqual(record_ack(self.ledger, bundle, consumer="FOIA_Aggregator", ack_id="ack-1",
                                    status="accepted"), "duplicate")
        with self.assertRaises(InterchangeError):
            record_ack(self.ledger, bundle, consumer="FOIA_Aggregator", ack_id="ack-1", status="rejected")
        self.assertEqual(delivery_status(self.ledger, bundle)["status"], "acknowledged")
        with self.assertRaises(InterchangeError):
            record_ack(self.ledger, "0" * 64, consumer="x", ack_id="ack-2", status="accepted")


if __name__ == "__main__":
    unittest.main()
