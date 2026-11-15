import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from foia_requests import directory
from foia_requests.aggregator_contract import load_export
from foia_requests.authorization import AuthorizationError, grant_role
from foia_requests.continuation import (due_periods, enable_schedule, next_period, prepare_cycle,
                                        scheduled_tick)
from foia_requests.ledger import Ledger
from foia_requests.periods import PeriodError, calendar_year, custom, fiscal_year
from foia_requests.templates import RequesterProfile, load_template

ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = ["fed:doj/oip", "fed:epa/old-region", "fed:doe/legacy", "fed:dhs/foia-unit"]


class ContinuationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "s" / "ledger.sqlite"
        self.ledger = Ledger(self.path, clock=lambda: "2026-11-15T00:00:00Z")
        directory.persist(self.ledger, directory.discover(directory.load_source(ROOT / "fixtures/directory"),
                                                          directory.fixture_fetcher(ROOT / "fixtures/directory")))
        self.kw = dict(export=load_export(ROOT / "fixtures/aggregator_export"),
                       profile=RequesterProfile.from_file(ROOT / "fixtures/profiles/synthetic-requester.json"),
                       template=load_template(), actor="continuation")

    def tearDown(self):
        self.ledger.close()
        self.folder.cleanup()

    def test_period_rollover_leap_and_missed(self):
        self.assertEqual(next_period(calendar_year(2023)), calendar_year(2024))
        self.assertEqual(next_period(fiscal_year(2024)).iso(), ("2024-10-01", "2025-09-30"))
        self.assertEqual(next_period(fiscal_year(2024, 7)).iso(), ("2024-07-01", "2025-06-30"))
        self.assertEqual(next_period(calendar_year(2023)).days, 366)
        with self.assertRaises(PeriodError):
            next_period(custom("2024-01-01", "2024-03-31"))
        self.assertEqual(due_periods(calendar_year(2022), date(2025, 3, 1)), [calendar_year(2023), calendar_year(2024)])
        self.assertEqual(due_periods(calendar_year(2024), date(2024, 12, 31)), [])

    def test_cycle_rechecks_components_and_never_authorizes(self):
        result = prepare_cycle(self.ledger, campaign_id="c1", components=COMPONENTS, period=calendar_year(2023),
                               **self.kw)
        decisions = {i["component_id"]: i["decision"] for i in result["items"]}
        self.assertEqual(decisions, {"fed:doj/oip": "suppressed_public_coverage", "fed:epa/region-x": "draft",
                                     "fed:doe/legacy": "blocked_component", "fed:dhs/foia-unit": "draft"})
        self.assertEqual((result["sends"], result["authorizations_created"]), (0, 0))
        states = {self.ledger.get(i["request_id"])["state"] for i in result["items"] if i["request_id"]}
        self.assertEqual(states, {"draft"})
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM authorizations").fetchone()[0], 0)

    def test_repeated_trigger_and_crash_restart_yield_one_set(self):
        code = ("import os, sys\n"
                "from pathlib import Path\n"
                "from foia_requests import directory\n"
                "from foia_requests.ledger import Ledger\n"
                "from foia_requests.aggregator_contract import load_export\n"
                "from foia_requests.continuation import prepare_cycle\n"
                "from foia_requests.periods import calendar_year\n"
                "from foia_requests.templates import RequesterProfile, load_template\n"
                "root = Path(sys.argv[2])\n"
                "l = Ledger(sys.argv[1])\n"
                "def die(c):\n"
                "    if c == 'fed:doj/oip': os._exit(9)\n"
                "prepare_cycle(l, campaign_id='c1', components=['fed:dhs/foia-unit','fed:doj/oip','fed:epa/region-x'],"
                " period=calendar_year(2024), export=load_export(root/'fixtures/aggregator_export'),"
                " profile=RequesterProfile.from_file(root/'fixtures/profiles/synthetic-requester.json'),"
                " template=load_template(), actor='c', hooks={'after_item': die})\n")
        self.ledger.close()
        result = subprocess.run([sys.executable, "-c", code, str(self.path), str(ROOT)], cwd=ROOT,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 9, result.stderr)
        self.ledger = Ledger(self.path, clock=lambda: "2026-11-15T00:00:00Z")
        components = ["fed:dhs/foia-unit", "fed:doj/oip", "fed:epa/region-x"]
        first = prepare_cycle(self.ledger, campaign_id="c1", components=components, period=calendar_year(2024),
                              **self.kw)
        again = prepare_cycle(self.ledger, campaign_id="c1", components=components, period=calendar_year(2024),
                              **self.kw)
        self.assertEqual(first, again)
        self.assertEqual(first["status"], "prepared")
        self.assertEqual(len(first["items"]), 3)
        self.assertEqual(len(self.ledger.list_requests()), 3)

    def test_prior_cycle_requests_are_preserved_and_distinct(self):
        a = prepare_cycle(self.ledger, campaign_id="c1", components=["fed:dhs/foia-unit"], period=calendar_year(2023),
                          **self.kw)
        b = prepare_cycle(self.ledger, campaign_id="c1", components=["fed:dhs/foia-unit"], period=calendar_year(2024),
                          prior_cycle_id=a["cycle_id"], **self.kw)
        self.assertNotEqual(a["items"][0]["request_id"], b["items"][0]["request_id"])
        self.assertEqual(len(self.ledger.list_requests()), 2)

    def test_scheduler_disabled_by_default_and_explicitly_enabled(self):
        now = datetime(2025, 1, 1, 6, tzinfo=timezone.utc)  # still Dec 31 in Los Angeles
        self.assertEqual(scheduled_tick(self.ledger, "c1", now, calendar_year(2023))["status"], "disabled")
        with self.assertRaises(AuthorizationError):
            enable_schedule(self.ledger, "c1", "nobody", timezone="America/Los_Angeles", basis="calendar",
                            expires_at="2027-01-01T00:00:00Z")
        grant_role(self.ledger, "auth", "authorizer", "owner:test")
        enable_schedule(self.ledger, "c1", "auth", timezone="America/Los_Angeles", basis="calendar",
                        expires_at="2027-01-01T00:00:00Z")
        self.assertEqual(scheduled_tick(self.ledger, "c1", now, calendar_year(2023))["status"], "idle")
        later = scheduled_tick(self.ledger, "c1", datetime(2025, 1, 1, 9, tzinfo=timezone.utc), calendar_year(2023))
        self.assertEqual(later["due"], [["2024-01-01", "2024-12-31", "calendar"]])
        missed = scheduled_tick(self.ledger, "c1", datetime(2026, 6, 1, tzinfo=timezone.utc), calendar_year(2023))
        self.assertTrue(missed["missed"])
        self.assertEqual(scheduled_tick(self.ledger, "c1", datetime(2027, 6, 1, tzinfo=timezone.utc),
                                        calendar_year(2023))["status"], "expired")


if __name__ == "__main__":
    unittest.main()
