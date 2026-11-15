import json
import shutil
import tempfile
import unittest
from pathlib import Path

from foia_requests.directory import (DEFAULT_OFFICIAL_SUFFIXES, DirectoryError, apply_staleness, check_url,
                                     component_record, discover, fixture_fetcher, load_source, persist,
                                     routes_for)
from foia_requests.ledger import Ledger, RequestScope

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "directory"


def run(directory=FIXTURE):
    return discover(load_source(directory), fixture_fetcher(directory))


class DirectoryTests(unittest.TestCase):
    def test_stable_distinct_ids_for_duplicate_names(self):
        result = run()
        self.assertIn("fed:doj/foia-unit", result.components)
        self.assertIn("fed:dhs/foia-unit", result.components)
        self.assertEqual(result.components["fed:doj/foia-unit"].name, result.components["fed:dhs/foia-unit"].name)
        self.assertEqual(sorted(result.components), sorted(run().components))
        self.assertEqual(result.snapshot_sha256, run().snapshot_sha256)

    def test_multi_page_merge_aliases_and_statuses(self):
        result = run()
        oip = result.components["fed:doj/oip"]
        self.assertEqual(oip.aliases, {"OIP FOIA"})
        self.assertEqual(sorted(r.kind for r in oip.routes.values()), ["email", "portal", "postal"])
        self.assertEqual(result.components["fed:epa/old-region"].renamed_to, "fed:epa/region-x")
        self.assertEqual(result.components["fed:doe/legacy"].status, "retired")
        self.assertEqual(result.components["fed:dot/mystery"].status, "unknown")

    def test_route_validation(self):
        states = {(r.component_id, r.kind, r.destination): (r.state, r.reason)
                  for c in run().components.values() for r in c.routes.values()}
        get = lambda cid, kind, dest: states[(cid, kind, dest)][0]
        self.assertEqual(get("fed:doj/oip", "email", "foia-oip@doj.gov.example"), "verified")
        self.assertEqual(get("fed:doj/oip", "portal", "https://portal.agency.gov.example/doj/oip"), "verified")
        self.assertEqual(get("fed:doj/oip", "postal", "Synthetic postal address, Washington DC"), "manual")
        self.assertEqual(get("fed:dhs/foia-unit", "email", "dhs-foia@mail.example.com"), "unverified")
        dhs_portal = states[("fed:dhs/foia-unit", "portal", "https://portal.agency.gov.example/dhs")]
        self.assertEqual(dhs_portal[0], "invalid")
        self.assertIn("unsafe redirect", dhs_portal[1])
        self.assertEqual(get("fed:epa/region-x", "portal", "http://portal.agency.gov.example/epa/region-x"), "invalid")
        self.assertEqual(get("fed:epa/region-x", "email", "not-an-email"), "invalid")
        self.assertEqual(get("fed:dot/mystery", "portal", "https://user:pw@portal.agency.gov.example/dot"), "invalid")
        self.assertEqual(get("fed:dot/mystery", "portal", "https://192.0.2.10/dot"), "invalid")

    def test_default_official_namespace(self):
        self.assertIsNone(check_url("https://www.foia.gov/", DEFAULT_OFFICIAL_SUFFIXES))
        self.assertIsNone(check_url("https://www.example.mil/foia", DEFAULT_OFFICIAL_SUFFIXES))
        for bad in ("https://foia.gov.evil.example/", "https://evilgov/", "https://www.foia.gov:8443/",
                    "ftp://www.foia.gov/", "https://[2001:db8::1]/"):
            with self.subTest(bad=bad):
                self.assertIsNotNone(check_url(bad, DEFAULT_OFFICIAL_SUFFIXES))

    def test_rate_limit_leaves_actionable_queue(self):
        result = run()
        self.assertFalse(result.complete)
        self.assertEqual(result.pages, 3)
        self.assertIn({"page": "page-3.json", "status": 429, "action": "retry_after", "retry_after": 120,
                       "reason": "rate limited"}, result.actions)

    def test_forbidden_loop_and_unofficial_page(self):
        with tempfile.TemporaryDirectory() as folder:
            copy = Path(folder) / "dir"
            shutil.copytree(FIXTURE, copy)
            (copy / "page-2.json").write_text(json.dumps({"status": 403, "url": "x", "next": None, "items": []}))
            result = run(copy)
            self.assertEqual(result.actions[0]["action"], "manual_review")
            self.assertEqual(result.actions[0]["reason"], "access denied")
            (copy / "page-2.json").write_text(json.dumps({"status": 200, "url":
                                                          "https://directory.agency.gov.example/p2",
                                                          "next": "page-1.json", "items": []}))
            self.assertIn("pagination loop", [a.get("reason") for a in run(copy).actions])
            (copy / "page-2.json").write_text(json.dumps({"status": 200, "url": "https://mirror.example.com/p2",
                                                          "next": None, "items": []}))
            self.assertFalse(run(copy).complete)
            with self.assertRaises(DirectoryError):
                fixture_fetcher(copy)("../source.json")

    def test_staleness(self):
        route = next(r for r in run().components["fed:doj/oip"].routes.values() if r.kind == "email").as_dict()
        self.assertEqual(apply_staleness(route, "2027-01-01T00:00:00Z")["state"], "verified")
        self.assertEqual(apply_staleness(route, "2028-01-01T00:00:00Z")["state"], "stale")

    def test_persist_is_idempotent_and_records_rename_alias(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = Ledger(Path(folder) / "l" / "ledger.sqlite")
            try:
                old = ledger.create_draft(RequestScope("requester:synthetic-1", "us-federal", "fed:epa/old-region",
                                                       "foia_request_log", "2024-01-01", "2024-12-31"))
                result = run()
                self.assertTrue(persist(ledger, result))
                self.assertFalse(persist(ledger, result))
                self.assertEqual(ledger.canonical_component("fed:epa/old-region"), "fed:epa/region-x")
                self.assertEqual(ledger.get(old.request_id)["component_id"], "fed:epa/region-x")
                self.assertEqual(component_record(ledger, "fed:doj/oip")["aliases"], ["OIP FOIA"])
                self.assertEqual(len(routes_for(ledger, "fed:doj/oip")), 3)
                # An incomplete listing never retires routes it did not see.
                self.assertEqual(ledger.connection.execute(
                    "SELECT COUNT(*) FROM contact_routes WHERE superseded_at IS NOT NULL").fetchone()[0], 0)
            finally:
                ledger.close()

    def test_complete_relisting_supersedes_routes_that_disappeared(self):
        with tempfile.TemporaryDirectory() as folder:
            copy = Path(folder) / "dir"
            shutil.copytree(FIXTURE, copy)
            page2 = json.loads((copy / "page-2.json").read_text())
            page2["next"] = None
            (copy / "page-2.json").write_text(json.dumps(page2))
            ledger = Ledger(Path(folder) / "l" / "ledger.sqlite")
            try:
                first = run(copy)
                self.assertTrue(first.complete)
                persist(ledger, first)
                page2["items"][0]["routes"] = []  # OIP postal route no longer listed
                page2["url"] = page2["url"] + "&v=2"
                (copy / "page-2.json").write_text(json.dumps(page2))
                persist(ledger, run(copy))
                kinds = sorted(r["kind"] for r in routes_for(ledger, "fed:doj/oip"))
                self.assertEqual(kinds, ["email", "portal"])
                self.assertEqual(len(routes_for(ledger, "fed:doj/oip", include_superseded=True)), 3)
            finally:
                ledger.close()


if __name__ == "__main__":
    unittest.main()
