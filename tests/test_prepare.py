import json
import stat
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from foia_requests import directory
from foia_requests.aggregator_contract import load_export
from foia_requests.config import Config
from foia_requests.ledger import Ledger
from foia_requests.periods import custom, select_period
from foia_requests.prepare import PrepareError, _md, prepare_packet
from foia_requests.templates import RequesterProfile, load_template

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 11, 16)
AS_OF = "2026-11-16T00:00:00Z"


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.folder.name) / "state" / "ledger.sqlite", clock=lambda: AS_OF)
        found = directory.discover(directory.load_source(ROOT / "fixtures/directory"),
                                   directory.fixture_fetcher(ROOT / "fixtures/directory"))
        directory.persist(self.ledger, found)
        self.profile = RequesterProfile.from_file(ROOT / "fixtures/profiles/synthetic-requester.json")
        self.export = load_export(ROOT / "fixtures/aggregator_export")

    def tearDown(self):
        self.ledger.close()
        self.folder.cleanup()

    def prepare(self, period=None, warnings=(), out="packet", config=None, **filters):
        if period is None:
            period, warnings = select_period(last=7, today=TODAY)
        return prepare_packet(self.ledger, config or Config(), period=period, warnings=warnings,
                              export=self.export, profile=self.profile, template=load_template(),
                              output_dir=Path(self.folder.name) / out, as_of=AS_OF, today=TODAY, **filters)

    def requests(self):
        return self.ledger.connection.execute("SELECT COUNT(*) FROM requests").fetchone()[0]

    def test_last_week_all_agencies_packet(self):
        manifest, folder = self.prepare()
        decisions = {e["component_id"]: e["decision"] for e in manifest["entries"]}
        self.assertEqual(decisions, {
            "fed:dhs/foia-unit": "draft", "fed:doe/legacy": "excluded_not_current",
            "fed:doj/foia-unit": "draft", "fed:doj/oip": "draft", "fed:dot/mystery": "excluded_not_current",
            "fed:epa/old-region": "excluded_alias", "fed:epa/region-x": "draft"})
        self.assertEqual((manifest["outbound_messages_sent"], manifest["live_dispatch_authorized"]), (0, False))
        self.assertEqual(self.requests(), 0)  # preview only: nothing stored, no dry-run cap consumed
        self.assertEqual(len(manifest["period"]["warnings"]), 1)
        markdown = (folder / "review-packet.md").read_text(encoding="utf-8")
        self.assertIn("**DRY RUN. Nothing was sent", markdown)
        self.assertIn("received from November 9, 2026 through November 15, 2026", markdown)
        self.assertIn("route: verified email", markdown)
        self.assertIn("manual handling required", markdown)
        self.assertIn("approved template (status: engineering_draft_pending_legal_review)", markdown)
        for name in ("review-packet.md", "prepare-manifest.json"):
            self.assertEqual(stat.S_IMODE((folder / name).stat().st_mode), 0o600)
        stored = json.loads((folder / "prepare-manifest.json").read_text())
        self.assertEqual(stored["manifest_sha256"], manifest["manifest_sha256"])
        again, again_folder = self.prepare(out="packet2")
        self.assertEqual(again["manifest_sha256"], manifest["manifest_sha256"])
        self.assertEqual((again_folder / "review-packet.md").read_text(encoding="utf-8"), markdown)

    def test_filters(self):
        manifest, _ = self.prepare(agencies=["DOJ"])
        self.assertEqual([e["component_id"] for e in manifest["entries"]], ["fed:doj/foia-unit", "fed:doj/oip"])
        manifest, _ = self.prepare(components=["fed:dhs/foia-unit"], agencies=["doj"])
        self.assertEqual(len(manifest["entries"]), 3)
        with self.assertRaisesRegex(PrepareError, "agencies not in the directory"):
            self.prepare(agencies=["nasa"])
        with self.assertRaisesRegex(PrepareError, "components not in the directory"):
            self.prepare(components=["fed:nasa/hq"])

    def test_public_coverage_and_existing_requests(self):
        manifest, _ = self.prepare(custom("2023-03-01", "2023-03-07"), components=["fed:doj/oip"])
        self.assertEqual(manifest["entries"][0]["decision"], "excluded_public_coverage")
        base = {"requester": self.profile.requester_principal, "jurisdiction": "us-federal",
                "component_id": "fed:doj/oip", "record_class": "foia_request_log", "period_basis": "custom"}
        self.ledger.import_history([dict(base, period_start="2026-11-09", period_end="2026-11-15",
                                         state="submitted")])
        self.ledger.import_history([dict(base, component_id="fed:doj/foia-unit", period_start="2026-11-01",
                                         period_end="2026-11-10", state="submitted")])
        manifest, _ = self.prepare(agencies=["doj"])
        by_id = {e["component_id"]: e for e in manifest["entries"]}
        self.assertEqual(by_id["fed:doj/oip"]["decision"], "excluded_duplicate")
        self.assertEqual(by_id["fed:doj/foia-unit"]["decision"], "draft")
        self.assertTrue(any("overlapping ledger request" in w for w in by_id["fed:doj/foia-unit"]["why"]))

    def test_live_configuration_denied_and_markdown_escaped(self):
        with self.assertRaises(PrepareError):
            self.prepare(config=SimpleNamespace(mode="live", transport_enabled=True))
        self.assertEqual(_md("<img src=x>[a](b)|`c`"), "\\<img src=x\\>\\[a\\](b)\\|\\`c\\`")


if __name__ == "__main__":
    unittest.main()
