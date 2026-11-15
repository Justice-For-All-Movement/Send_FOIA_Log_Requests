import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from foia_requests import directory
from foia_requests.aggregator_contract import load_export
from foia_requests.authorization import (authorize_batch, grant_role, outbox_rows, queue_authorized, stopped)
from foia_requests.config import Config
from foia_requests.dispatch import RateLimits, destination_host, run_once
from foia_requests.dryrun import record_review, run_dry_run
from foia_requests.fake_transport import FakeSender
from foia_requests.ledger import Ledger
from foia_requests.periods import calendar_year
from foia_requests.transport import DisabledSender, TransportDisabled
from tests.test_authorization import approved_template, test_profile

ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-11-15T00:00:00Z"


class LiveFake(FakeSender):
    live = True


def prepare(self):
    """Shared fixture: three reviewed, authorized and queued fixture requests."""
    self.folder = tempfile.TemporaryDirectory()
    tmp = Path(self.folder.name)
    self.ledger = Ledger(tmp / "state" / "ledger.sqlite", clock=lambda: NOW)
    directory.persist(self.ledger, directory.discover(directory.load_source(ROOT / "fixtures/directory"),
                                                      directory.fixture_fetcher(ROOT / "fixtures/directory")))
    template, profile = approved_template(tmp), test_profile()
    packet, folder = run_dry_run(self.ledger, Config(), selection=[
        ("fed:doj/oip", calendar_year(y)) for y in (2020, 2021, 2022)],
        export=load_export(ROOT / "fixtures/aggregator_export"), profile=profile, template=template,
        output_dir=tmp / "packets", as_of=NOW)
    record_review(self.ledger, folder / "packet.json", "reviewer", "ok")
    for operator, role in (("auth", "authorizer"), ("disp", "dispatcher"), ("safety", "safety")):
        grant_role(self.ledger, operator, role, "owner:test")
    self.ids = [e["request_id"] for e in packet["entries"] if e["decision"] == "draft"]
    self.auth_id, _ = authorize_batch(self.ledger, request_ids=self.ids, authorizer="auth", reference="TEST-1",
                                      expires_at="2026-11-30T00:00:00Z", profile=profile, template=template,
                                      transport="fake", max_messages=3, now=NOW)
    queue_authorized(self.ledger, self.auth_id, "disp", now=NOW)


class DispatchTests(unittest.TestCase):
    setUp = prepare

    def tearDown(self):
        self.ledger.close()
        self.folder.cleanup()

    def states(self):
        return sorted(self.ledger.get(r)["state"] for r in self.ids)

    def test_disabled_and_live_senders_never_touch_the_outbox(self):
        self.assertEqual(run_once(self.ledger, DisabledSender(), Config(), owner="w", now=NOW)["status"],
                         "transport_disabled")
        with self.assertRaises(TransportDisabled):
            run_once(self.ledger, LiveFake(), Config(), owner="w", now=NOW)
        self.assertTrue(all(r["state"] == "queued" for r in outbox_rows(self.ledger)))

    def test_success_sends_exact_frozen_content_once_with_receipt(self):
        sender = FakeSender()
        for _ in range(5):
            run_once(self.ledger, sender, Config(), owner="w", now=NOW,
                     limits=RateLimits(per_destination_per_hour=10, global_per_hour=10))
        self.assertEqual(len(sender.sent), 3)
        for message, _ in sender.sent:
            revision = self.ledger.latest_revision(message.request_id)
            self.assertEqual((message.body, message.content_sha256), (revision["body"], revision["content_sha256"]))
        self.assertEqual(self.states(), ["submitted"] * 3)
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM receipts").fetchone()[0], 3)
        # Provider acceptance is not agency acknowledgment.
        self.assertTrue(all(self.ledger.get(r)["state"] != "acknowledged" for r in self.ids))

    def test_rate_limits_hold_before_any_transport_call(self):
        sender = FakeSender()
        results = [run_once(self.ledger, sender, Config(), owner="w", now=NOW,
                            limits=RateLimits(per_destination_per_hour=2, global_per_hour=10))["status"]
                   for _ in range(3)]
        self.assertEqual(results, ["submitted", "submitted", "rate_limited_local"])
        self.assertEqual(len(sender.sent), 2)
        later = run_once(self.ledger, sender, Config(), owner="w", now="2026-11-15T01:00:01Z",
                         limits=RateLimits(per_destination_per_hour=2, global_per_hour=10))
        self.assertEqual(later["status"], "submitted")

    def test_provider_429_schedules_retry_after(self):
        sender = FakeSender(["rate_limited"])
        result = run_once(self.ledger, sender, Config(), owner="w", now=NOW)
        self.assertEqual((result["status"], result["retry_after"]), ("retry_scheduled", 60))
        self.assertEqual(self.ledger.get(sender.sent[0][0].request_id)["state"], "queued")
        self.assertEqual(run_once(self.ledger, FakeSender(), Config(), owner="w", now=NOW)["attempt_id"] !=
                         result["attempt_id"], True)

    def test_timeout_is_unknown_and_never_resent(self):
        sender = FakeSender(["timeout"])
        result = run_once(self.ledger, sender, Config(), owner="w", now=NOW)
        self.assertEqual(result["status"], "submission_unknown")
        rid = sender.sent[0][0].request_id
        self.assertEqual(self.ledger.get(rid)["state"], "submission_unknown")
        for _ in range(4):
            run_once(self.ledger, sender, Config(), owner="w", now="2026-11-16T00:00:00Z",
                     limits=RateLimits(per_destination_per_hour=10))
        self.assertEqual([m.request_id for m, _ in sender.sent].count(rid), 1)

    def test_rejection_bounce_and_auth_failure(self):
        sender = FakeSender(["rejected", "auth_failed"])
        self.assertEqual(run_once(self.ledger, sender, Config(), owner="w", now=NOW)["status"], "failed")
        self.assertEqual(run_once(self.ledger, sender, Config(), owner="w", now=NOW)["outcome"], "auth_failed")
        self.assertTrue(stopped(self.ledger))
        self.assertEqual(run_once(self.ledger, sender, Config(), owner="w", now=NOW)["status"], "idle")
        self.assertEqual(self.states().count("delivery_failed"), 2)

    def test_receipt_hash_mismatch_never_claims_submission(self):
        class Liar(FakeSender):
            def send(self, message):
                result = super().send(message)
                return SimpleNamespace(**{**result.__dict__, "receipt": {"content_sha256": "0" * 64}})
        result = run_once(self.ledger, Liar(), Config(), owner="w", now=NOW)
        self.assertEqual(result["status"], "submission_unknown")
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM receipts").fetchone()[0], 0)

    def test_destination_host(self):
        self.assertEqual(destination_host("email", "a@Doj.Gov.Example"), "doj.gov.example")
        self.assertEqual(destination_host("portal", "https://portal.agency.gov.example/x"), "portal.agency.gov.example")


if __name__ == "__main__":
    unittest.main()
