import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from foia_requests import directory
from foia_requests.aggregator_contract import load_export
from foia_requests.config import Config
from foia_requests.dryrun import DryRunError, record_review, reserve, run_dry_run
from foia_requests.fake_transport import FakeSender
from foia_requests.ledger import Ledger
from foia_requests.periods import calendar_year, fiscal_year
from foia_requests.templates import RequesterProfile, load_template
from foia_requests.transport import (DisabledSender, TransportDisabled, TransportNotImplemented,
                                     sender_from_env)

ROOT = Path(__file__).resolve().parents[1]
AS_OF = "2026-11-15T00:00:00Z"


class LiveSender(FakeSender):
    live = True


def make_ledger(folder):
    ledger = Ledger(Path(folder) / "state" / "ledger.sqlite", clock=lambda: AS_OF)
    found = directory.discover(directory.load_source(ROOT / "fixtures/directory"),
                               directory.fixture_fetcher(ROOT / "fixtures/directory"))
    directory.persist(ledger, found)
    return ledger


class DryRunTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.ledger = make_ledger(self.folder.name)
        self.export = load_export(ROOT / "fixtures/aggregator_export")
        self.profile = RequesterProfile.from_file(ROOT / "fixtures/profiles/synthetic-requester.json")
        self.template = load_template()
        self.out = Path(self.folder.name) / "packets"

    def tearDown(self):
        self.ledger.close()
        self.folder.cleanup()

    def run_it(self, selection, ledger=None, config=None, sender=None, out=None):
        return run_dry_run(ledger or self.ledger, config or Config(), selection=selection, export=self.export,
                           profile=self.profile, template=self.template, output_dir=out or self.out,
                           as_of=AS_OF, sender=sender)

    def test_packet_contents_and_zero_sends(self):
        sender = DisabledSender()
        packet, folder = self.run_it([("fed:doj/oip", calendar_year(2024)), ("fed:doj/oip", calendar_year(2023)),
                                      ("fed:dhs/foia-unit", calendar_year(2024))], sender=sender)
        self.assertEqual(sender.attempts, 0)
        self.assertEqual(packet["outbound_messages_sent"], 0)
        self.assertFalse(packet["live_dispatch_authorized"])
        decisions = [e["decision"] for e in packet["entries"]]
        self.assertEqual(decisions, ["draft", "suppressed_public_coverage", "draft"])
        oip = packet["entries"][0]
        self.assertEqual(oip["recipient"], "foia-oip@doj.gov.example")
        self.assertEqual(oip["coverage"]["uncovered"], [["2024-02-29", "2024-02-29"]])
        self.assertFalse(oip["live_ready"])
        dhs = packet["entries"][2]
        # Off-namespace e-mail and invalid portal: no verified route is visible to the reviewer.
        self.assertIn("verified submission route", " ".join(dhs["missing_inputs"]))
        self.assertEqual((folder / "packet.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
        self.assertTrue(any(p.suffix == ".txt" for p in folder.iterdir()))

    def test_packet_hash_reproduces_across_fresh_ledgers(self):
        selection = [("fed:doj/oip", fiscal_year(2024)), ("fed:epa/region-x", calendar_year(2022))]
        first, _ = self.run_it(selection)
        again, _ = self.run_it(selection)
        with tempfile.TemporaryDirectory() as other:
            fresh = make_ledger(other)
            try:
                third, _ = self.run_it(selection, ledger=fresh, out=Path(other) / "p")
            finally:
                fresh.close()
        self.assertEqual(first["packet_sha256"], again["packet_sha256"])
        self.assertEqual(first["packet_sha256"], third["packet_sha256"])

    def test_cap_and_live_mode_denied(self):
        with self.assertRaisesRegex(DryRunError, "exceeds dry-run cap"):
            self.run_it([("fed:doj/oip", calendar_year(y)) for y in (2019, 2020, 2021, 2022)])
        with self.assertRaisesRegex(DryRunError, "exceeds dry-run cap 1"):
            self.run_it([("fed:doj/oip", calendar_year(2020)), ("fed:doj/oip", calendar_year(2021))],
                        config=Config(max_dry_run_drafts=1))
        live = SimpleNamespace(mode="live", transport_enabled=True, max_dry_run_drafts=3)
        with self.assertRaisesRegex(DryRunError, "live mode is denied"):
            self.run_it([("fed:doj/oip", calendar_year(2020))], config=live)
        sender = LiveSender()
        with self.assertRaisesRegex(DryRunError, "non-live sender"):
            self.run_it([("fed:doj/oip", calendar_year(2020))], sender=sender)
        self.assertEqual(sender.sent, [])
        with self.assertRaises(DryRunError):
            self.run_it([])

    def test_cap_holds_under_parallel_reservation(self):
        with self.ledger.transaction() as conn:
            conn.execute("INSERT INTO dry_runs VALUES ('race', 3, ?, NULL, 'generating')", (AS_OF,))
        path = self.ledger.path
        outcomes = []

        def worker(i):
            ledger = Ledger(path)
            try:
                outcomes.append(reserve(ledger, "race", f"item-{i}"))
            except DryRunError:
                outcomes.append("capped")
            finally:
                ledger.close()
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(outcomes.count(True), 3)
        self.assertEqual(outcomes.count("capped"), 9)
        self.assertEqual(self.ledger.connection.execute(
            "SELECT COUNT(*) FROM dry_run_items WHERE run_id='race'").fetchone()[0], 3)

    def test_existing_request_and_blocked_components(self):
        self.ledger.import_history([{"requester": "requester:synthetic-1", "jurisdiction": "us-federal",
                                     "component_id": "fed:doj/oip", "record_class": "foia_request_log",
                                     "period_start": "2021-01-01", "period_end": "2021-12-31",
                                     "state": "submitted"}])
        packet, _ = self.run_it([("fed:doj/oip", calendar_year(2021)), ("fed:doe/legacy", calendar_year(2021)),
                                 ("fed:nope/none", calendar_year(2021))])
        self.assertEqual([e["decision"] for e in packet["entries"]],
                         ["duplicate_existing_request", "blocked_component_not_current", "blocked_unknown_component"])
        self.assertEqual(packet["entries"][0]["existing_state"], "submitted")
        self.assertEqual(len(self.ledger.list_requests()), 1)

    def test_renamed_component_selection_resolves_to_successor(self):
        packet, _ = self.run_it([("fed:epa/old-region", calendar_year(2023))])
        entry = packet["entries"][0]
        self.assertEqual(entry["component_id"], "fed:epa/region-x")
        self.assertEqual(entry["requested_component_id"], "fed:epa/old-region")

    def test_review_verifies_packet_and_content(self):
        packet, folder = self.run_it([("fed:doj/oip", calendar_year(2024))])
        rid = packet["entries"][0]["request_id"]
        tampered = Path(self.folder.name) / "tampered.json"
        data = json.loads((folder / "packet.json").read_text())
        data["entries"][0]["recipient"] = "attacker@example.com"
        tampered.write_text(json.dumps(data))
        with self.assertRaisesRegex(DryRunError, "hash mismatch"):
            record_review(self.ledger, tampered, "operator", "x")
        self.assertEqual(record_review(self.ledger, folder / "packet.json", "operator", "looks right"), [rid])
        self.assertEqual(self.ledger.get(rid)["state"], "dry_run_reviewed")
        events = self.ledger.events(rid)
        self.assertIn(packet["packet_sha256"], events[-1]["evidence"])
        # A content change after the packet invalidates the review and blocks re-review.
        self.ledger.add_revision(rid, "annual_foia_log_request", "1", "S", "edited body", None)
        self.assertEqual(self.ledger.get(rid)["state"], "draft")
        with self.assertRaisesRegex(DryRunError, "changed after the packet"):
            record_review(self.ledger, folder / "packet.json", "operator", "again")


class TransportConfigTests(unittest.TestCase):
    def test_fail_closed_provider_selection(self):
        self.assertIsInstance(sender_from_env({}), DisabledSender)
        with self.assertRaises(TransportDisabled):
            sender_from_env({}).send(None)
        with self.assertRaises(TransportDisabled):
            sender_from_env({"FOIA_TRANSPORT_PROVIDER": "fake"})
        self.assertIsInstance(sender_from_env({"FOIA_TRANSPORT_PROVIDER": "fake",
                                               "FOIA_TRANSPORT_FAKE_ALLOWED": "1"}), FakeSender)
        for provider in ("portal", "sendgrid"):
            with self.subTest(provider=provider), self.assertRaises(TransportNotImplemented):
                sender_from_env({"FOIA_TRANSPORT_PROVIDER": provider})
        # Live transports are never handed out directly; only the sending gate builds them.
        for provider in ("smtp", "manual"):
            with self.subTest(provider=provider), self.assertRaisesRegex(TransportDisabled, "sending gate"):
                sender_from_env({"FOIA_TRANSPORT_PROVIDER": provider, "FOIA_SMTP_HOST": "smtp.example.invalid"})


if __name__ == "__main__":
    unittest.main()
