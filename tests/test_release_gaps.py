import json
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

from foia_requests import directory
from foia_requests.config import Config
from foia_requests.ledger import Ledger
from foia_requests.release_gaps import GapsError, load_gaps, normalize_tracking, plan_followups
from foia_requests.templates import RequesterProfile, load_template

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures/release_gaps"
AS_OF = "2026-11-17T00:00:00Z"


def gap_lines():
    return [json.loads(line) for line in (FIXTURE / "gaps.jsonl").read_text().splitlines()]


class LoadGapsTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.folder.cleanup()

    def export(self, gaps=None, **manifest_changes):
        target = Path(self.folder.name) / "export"
        shutil.rmtree(target, ignore_errors=True)
        target.mkdir()
        gaps = gap_lines() if gaps is None else gaps
        manifest = json.loads((FIXTURE / "manifest.json").read_text())
        manifest["count"] = len(gaps)
        manifest.update(manifest_changes)
        (target / "manifest.json").write_text(json.dumps(manifest))
        (target / "gaps.jsonl").write_text("".join(json.dumps(g) + "\n" for g in gaps))
        return load_gaps(target)

    def test_fixture_loads(self):
        export = load_gaps(FIXTURE)
        self.assertEqual(len(export.gaps), 5)
        self.assertEqual(export.manifest["format"], "foia-release-gaps/1")
        self.assertEqual(len(export.manifest_sha256), 64)
        self.assertEqual(normalize_tracking(" doj-2026-000117 "), "DOJ-2026-000117")

    def test_manifest_rejections(self):
        for changes, message in (({"format": "foia-release-gaps/2"}, "unsupported"),
                                 ({"extra": 1}, "manifest fields"),
                                 ({"generated_at": "yesterday"}, "generated_at"),
                                 ({"producer": {"commit": 5}}, "producer"),
                                 ({"period": {"start": "2026-07-01", "end": "2026-01-01"}}, "start is after"),
                                 ({"period": {"start": "2026-1-1", "end": "2026-02-01"}}, "YYYY-MM-DD")):
            with self.assertRaisesRegex(GapsError, message, msg=str(changes)):
                self.export(**changes)
        with self.assertRaisesRegex(GapsError, "count 9"):
            self.export(count=9)

    def test_line_rejections(self):
        base = gap_lines()[0]
        cases = (
            ({k: v for k, v in base.items() if k != "log_id"}, "missing fields log_id"),
            (dict(base, requester_name="x"), "unknown fields requester_name"),
            (dict(base, disposition="full_denial"), "releases no records"),
            (dict(base, disposition="no_records"), "releases no records"),
            (dict(base, gap_reason="other"), "gap_reason"),
            (dict(base, pages_released=-1), "pages_released"),
            (dict(base, pages_released=True), "pages_released"),
            (dict(base, closed_date="06/15/2026"), "closed_date"),
            (dict(base, received_date="2026-07-01"), "after closed_date"),
            (dict(base, agency_slug="DOJ"), "agency_slug"),
            (dict(base, tracking_number="a\nb"), "control"),
            (dict(base, log_source_url="ftp://x"), "log_source_url"),
            (dict(base, tracking_number=""), "tracking_number"),
        )
        for gap, message in cases:
            with self.assertRaisesRegex(GapsError, "line 1: .*" + message, msg=message):
                self.export([gap])
        with self.assertRaisesRegex(GapsError, "duplicate request_key"):
            self.export([base, dict(base)])


class PlanFollowupsTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.folder.name) / "state" / "ledger.sqlite", clock=lambda: AS_OF)
        found = directory.discover(directory.load_source(ROOT / "fixtures/directory"),
                                   directory.fixture_fetcher(ROOT / "fixtures/directory"))
        directory.persist(self.ledger, found)
        self.profile = RequesterProfile.from_file(ROOT / "fixtures/profiles/synthetic-journalist.json")
        self.template = load_template("released_records_followup", "1")
        self.runs = 0

    def tearDown(self):
        self.ledger.close()
        self.folder.cleanup()

    def plan(self, export=None, **kwargs):
        self.runs += 1
        return plan_followups(self.ledger, Config(), export=export or load_gaps(FIXTURE), profile=self.profile,
                              template=self.template, output_dir=Path(self.folder.name) / f"run{self.runs}",
                              as_of=AS_OF, **kwargs)

    def recorded(self):
        return self.ledger.connection.execute("SELECT COUNT(*) FROM release_followups").fetchone()[0]

    def test_one_letter_per_record_with_dedupe(self):
        plan, folder = self.plan()
        self.assertEqual(len(plan["letters"]), 4)
        self.assertEqual([s["decision"] for s in plan["skipped"]], ["duplicate_in_export"])
        numbers = [i["tracking_number"] for letter in plan["letters"] for i in letter["items"]]
        self.assertEqual(len(numbers), len(set(numbers)))
        oip = next(letter for letter in plan["letters"] if letter["component"] == "OIP")
        self.assertEqual(oip["directory_component"], "fed:doj/oip")
        self.assertEqual(oip["route"]["kind"], "email")
        dhs = next(letter for letter in plan["letters"] if letter["agency_slug"] == "dhs")
        self.assertIsNone(dhs["route"])
        self.assertIn("official submission route", dhs["missing_inputs"])
        self.assertEqual((plan["outbound_messages_sent"], plan["live_dispatch_authorized"]), (0, False))
        self.assertEqual(self.recorded(), 4)
        markdown = (folder / "followups.md").read_text(encoding="utf-8")
        self.assertIn("**DRY RUN. Nothing was sent", markdown)
        self.assertIn("a copy of the records released in response to FOIA request DOJ-2026-000117", markdown)
        for name in ("followups.md", "followups.json"):
            self.assertEqual(stat.S_IMODE((folder / name).stat().st_mode), 0o600)
        again, _ = self.plan()  # every tracking number is already drafted
        self.assertEqual(again["letters"], [])
        self.assertEqual(sorted({s["decision"] for s in again["skipped"]}), ["already_drafted",
                                                                            "duplicate_in_export"])
        self.assertEqual(self.recorded(), 4)

    def test_group_filters_and_max(self):
        plan, _ = self.plan(group=True, record=False)
        sizes = sorted(len(letter["items"]) for letter in plan["letters"])
        self.assertEqual(sizes, [1, 1, 2])
        oip = next(letter for letter in plan["letters"] if letter["component"] == "OIP")
        self.assertIn("2 closed FOIA requests", oip["subject"])
        self.assertEqual(self.recorded(), 0)
        plan, _ = self.plan(agencies=["DHS"], record=False)
        self.assertEqual([letter["agency_slug"] for letter in plan["letters"]], ["dhs"])
        plan, _ = self.plan(max_letters=2, record=False)
        self.assertEqual(len(plan["letters"]), 2)
        self.assertEqual(sum(s["decision"] == "deferred_max" for s in plan["skipped"]), 2)
        with self.assertRaisesRegex(GapsError, "--max"):
            self.plan(max_letters=0)

    def test_group_cap_is_25(self):
        base = gap_lines()[0]
        gaps = [dict(base, request_key=f"k{n}", tracking_number=f"DOJ-2026-{n:06d}") for n in range(30)]
        folder = Path(self.folder.name) / "big"
        folder.mkdir()
        manifest = json.loads((FIXTURE / "manifest.json").read_text())
        manifest["count"] = len(gaps)
        (folder / "manifest.json").write_text(json.dumps(manifest))
        (folder / "gaps.jsonl").write_text("".join(json.dumps(g) + "\n" for g in gaps))
        plan, _ = self.plan(load_gaps(folder), group=True)
        self.assertEqual([len(letter["items"]) for letter in plan["letters"]], [25, 5])
        self.assertEqual(self.recorded(), 30)
        rows = self.ledger.connection.execute("SELECT COUNT(*) FROM release_followup_letters").fetchone()[0]
        self.assertEqual(rows, 2)
        with self.assertRaises(Exception):
            self.ledger.connection.execute("DELETE FROM release_followups")


if __name__ == "__main__":
    unittest.main()
