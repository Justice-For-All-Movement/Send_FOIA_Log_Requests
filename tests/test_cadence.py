import json
import stat
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from foia_requests import cadence, directory
from foia_requests.cadence import (CadenceError, latest_ended, load_cadence_overrides, load_priority,
                                   load_published_logs, next_unrequested, period_containing, plan_logs)
from foia_requests.config import Config
from foia_requests.ledger import Ledger
from foia_requests.periods import Period
from foia_requests.templates import RequesterProfile, load_template

ROOT = Path(__file__).resolve().parents[1]
AS_OF = "2026-11-17T00:00:00Z"
TODAY = date(2026, 11, 17)


class PeriodMathTests(unittest.TestCase):
    def test_months_and_quarters(self):
        self.assertEqual(period_containing(date(2024, 2, 10), "monthly"),
                         Period(date(2024, 2, 1), date(2024, 2, 29), "custom"))
        self.assertEqual(period_containing(date(2026, 12, 31), "quarterly"),
                         Period(date(2026, 10, 1), date(2026, 12, 31), "custom"))
        self.assertEqual(latest_ended(TODAY, "monthly").iso(), ("2026-10-01", "2026-10-31"))
        self.assertEqual(latest_ended(TODAY, "quarterly").iso(), ("2026-07-01", "2026-09-30"))
        # On the first day of a month the previous month has just fully ended.
        self.assertEqual(latest_ended(date(2026, 11, 1), "monthly").iso(), ("2026-10-01", "2026-10-31"))
        self.assertEqual(latest_ended(date(2026, 1, 1), "quarterly").iso(), ("2025-10-01", "2025-12-31"))
        with self.assertRaises(CadenceError):
            period_containing(TODAY, "hourly")

    def test_next_unrequested_skips_overlaps_and_reports_gap(self):
        existing = [{"period_start": "2026-04-01", "period_end": "2026-04-30"}]
        period, notes = next_unrequested(existing, "monthly", TODAY)
        self.assertEqual(period.iso(), ("2026-05-01", "2026-05-31"))
        self.assertEqual(notes, [])
        period, notes = next_unrequested(existing, "quarterly", TODAY)  # Apr-Jun overlaps April
        self.assertEqual(period.iso(), ("2026-07-01", "2026-09-30"))
        self.assertTrue(any("2026-05-01..2026-06-30" in n for n in notes))
        period, _ = next_unrequested([], "monthly", TODAY, since=date(2025, 1, 15))
        self.assertEqual(period.iso(), ("2025-01-01", "2025-01-31"))


class InputFileTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.folder.cleanup()

    def write(self, data):
        path = Path(self.folder.name) / "f.json"
        path.write_text(json.dumps(data))
        return path

    def test_published_logs_validation(self):
        logs = load_published_logs(ROOT / "fixtures/published_logs.synthetic.json")
        self.assertEqual([(p["agency"], p["component"]) for p in logs], [("epa", None), ("doj", "foia-unit")])
        good = {"agency_abbr": "DOJ", "component_abbr": None, "index_url": "https://x.example/logs",
                "cadence": "monthly"}
        for bad, message in ((dict(good, index_url="http://x.example"), "https"),
                             (dict(good, cadence="hourly-ish"), "cadence"),
                             (dict(good, extra=1), "fields"), ({"agency_abbr": "DOJ"}, "fields")):
            with self.assertRaisesRegex(CadenceError, message):
                load_published_logs(self.write([bad]))

    def test_overrides_validation(self):
        entry = {"agency_abbr": "DOJ", "component_abbr": None, "cadence": "quarterly"}
        self.assertEqual(load_cadence_overrides(self.write([entry]))[0]["cadence"], "quarterly")
        with self.assertRaisesRegex(CadenceError, "cadence"):
            load_cadence_overrides(self.write([dict(entry, cadence="hourly")]))
        with self.assertRaisesRegex(CadenceError, "duplicate"):
            load_cadence_overrides(self.write([entry, entry]))

    def test_packaged_priority_list(self):
        priority = load_priority()
        for key in ("cia", "nsa", "odni"):
            self.assertEqual(priority[(key, None)]["tier"], 1)
        for agency, component in (("doj", "fbi"), ("dhs", "ice"), ("dhs", "cbp"), ("dhs", "uscis"), ("dhs", "ia"),
                                  ("dhs", "tsa"), ("doj", "dea"), ("doj", "atf"), ("doj", "bop"), ("doj", "usms"),
                                  ("doj", "crt"), ("doj", "nsd"), ("dod", "dia"), ("dod", "nga"), ("dod", "nro"),
                                  ("dod", "army-cid"), ("dod", "inscom")):
            self.assertEqual(priority[(agency, component)]["tier"], 1, (agency, component))
        for agency, component in (("dos", None), ("treas", "irs"), ("treas", "fincen"), ("usps", "uspis"),
                                  ("fcc", None), ("ed", "ocr"), ("hhs", None), ("ssa", None)):
            self.assertEqual(priority[(agency, component)]["tier"], 2, (agency, component))
        self.assertTrue(priority[("cia", None)]["always_request"])
        self.assertEqual(cadence.priority_for("fed:nasa/hq", priority)["tier"], 3)
        self.assertEqual(cadence.priority_for("fed:doj/fbi", priority)["tier"], 1)
        bad = {"format": "sfl-priority/1", "note": "", "entries": {"CIA": {"tier": 4, "reason": "x"}}}
        with self.assertRaisesRegex(CadenceError, "tier"):
            load_priority(self.write(bad))


class PlanLogsTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.folder.name) / "state" / "ledger.sqlite", clock=lambda: AS_OF)
        found = directory.discover(directory.load_source(ROOT / "fixtures/directory"),
                                   directory.fixture_fetcher(ROOT / "fixtures/directory"))
        directory.persist(self.ledger, found)
        self.profile = RequesterProfile.from_file(ROOT / "fixtures/profiles/synthetic-journalist.json")
        self.template = load_template("periodic_closed_request_log", "1")
        self.runs = 0

    def tearDown(self):
        self.ledger.close()
        self.folder.cleanup()

    def plan(self, today=TODAY, **kwargs):
        self.runs += 1
        kwargs.setdefault("config", Config())
        config = kwargs.pop("config")
        return plan_logs(self.ledger, config, today=today, profile=self.profile, template=self.template,
                         output_dir=Path(self.folder.name) / f"run{self.runs}", as_of=AS_OF, **kwargs)

    def decisions(self, plan):
        return {e["component_id"]: (e["decision"], tuple(e.get("period") or ())) for e in plan["entries"]}

    def test_all_agencies_tier_order_and_tier_cadence(self):
        plan, folder = self.plan()
        order = [e["component_id"] for e in plan["entries"]]
        self.assertEqual(order[0], "fed:dhs/foia-unit")  # DHS is tier 1
        self.assertEqual([e["tier"] for e in plan["entries"]], sorted(e["tier"] for e in plan["entries"]))
        decisions = self.decisions(plan)
        self.assertEqual(decisions["fed:dhs/foia-unit"], ("draft", ("2026-10-01", "2026-10-31")))  # monthly
        self.assertEqual(decisions["fed:doj/oip"], ("draft", ("2026-07-01", "2026-09-30")))  # quarterly
        self.assertEqual(decisions["fed:epa/old-region"][0], "excluded_alias")
        self.assertEqual(decisions["fed:doe/legacy"][0], "excluded_not_current")
        self.assertEqual(plan["drafts"], 4)
        self.assertEqual((plan["outbound_messages_sent"], plan["live_dispatch_authorized"]), (0, False))
        self.assertEqual(plan["tiers"], {"1": 1, "2": 0, "3": 6})
        markdown = (folder / "plan.md").read_text(encoding="utf-8")
        self.assertIn("**DRY RUN. Nothing was sent", markdown)
        self.assertIn("| 1 | 1 | Department of Homeland Security |", markdown)
        self.assertIn("FOIA requests closed between October 1, 2026 and October 31, 2026", markdown)
        for name in ("plan.md", "plan.json"):
            self.assertEqual(stat.S_IMODE((folder / name).stat().st_mode), 0o600)
        stored = json.loads((folder / "plan.json").read_text())
        self.assertEqual(stored["plan_sha256"], plan["plan_sha256"])
        rows = self.ledger.connection.execute(
            "SELECT state, record_class, period_basis FROM requests").fetchall()
        self.assertEqual({tuple(r) for r in rows}, {("draft", "foia_closed_request_log", "custom")})
        self.assertEqual(len(rows), 4)

    def test_never_prospective_and_one_request_per_period(self):
        first, _ = self.plan()
        second, _ = self.plan()  # same day: the next periods have not ended
        for cid, (decision, period) in self.decisions(second).items():
            self.assertNotEqual(decision, "draft", cid)
        dhs = next(e for e in second["entries"] if e["component_id"] == "fed:dhs/foia-unit")
        self.assertEqual(dhs["period"], ["2026-11-01", "2026-11-30"])
        self.assertIn("no prospective requests", dhs["why"][-1])
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 4)
        third, _ = self.plan(today=date(2026, 12, 1))  # November ended yesterday
        self.assertEqual(self.decisions(third)["fed:dhs/foia-unit"], ("draft", ("2026-11-01", "2026-11-30")))
        dhs = next(e for e in third["entries"] if e["component_id"] == "fed:dhs/foia-unit")
        self.assertTrue(any("ended 1 day(s) ago" in w for w in dhs["warnings"]))
        self.assertEqual(self.decisions(third)["fed:doj/oip"][0], "not_due")  # Q4 ends 2026-12-31

    def test_month_end_today_is_not_planned(self):
        plan, _ = self.plan(today=date(2026, 10, 31), components=["fed:dhs/foia-unit"])
        self.assertEqual(self.decisions(plan)["fed:dhs/foia-unit"], ("draft", ("2026-09-01", "2026-09-30")))

    def test_published_logs_skip_and_overrides(self):
        published = load_published_logs(ROOT / "fixtures/published_logs.synthetic.json")
        overrides = [{"agency": "dhs", "component": None, "cadence": "quarterly"},
                     {"agency": "doj", "component": "oip", "cadence": "monthly"}]
        plan, _ = self.plan(published=published, overrides=overrides)
        decisions = self.decisions(plan)
        self.assertEqual(decisions["fed:epa/region-x"][0], "excluded_publishes_logs")
        self.assertEqual(decisions["fed:doj/foia-unit"][0], "excluded_publishes_logs")
        self.assertEqual(decisions["fed:dhs/foia-unit"], ("draft", ("2026-07-01", "2026-09-30")))
        self.assertEqual(decisions["fed:doj/oip"], ("draft", ("2026-10-01", "2026-10-31")))
        run, _ = self.plan(cadence="quarterly", components=["fed:dhs/foia-unit"], record=False)
        self.assertEqual(self.decisions(run)["fed:dhs/foia-unit"][0], "not_due")

    def test_always_request_overrides_published_logs(self):
        priority = {("dhs", None): {"tier": 1, "reason": "test", "always_request": True, "key": "DHS"}}
        published = [{"agency": "dhs", "component": None, "index_url": "https://x.example", "cadence": "monthly"}]
        plan, _ = self.plan(priority=priority, published=published, components=["fed:dhs/foia-unit"])
        entry = plan["entries"][0]
        self.assertEqual(entry["decision"], "draft")
        self.assertTrue(any("always_request" in w for w in entry["why"]))

    def test_max_no_record_since_and_guards(self):
        plan, _ = self.plan(max_drafts=1, record=False)
        self.assertEqual(plan["drafts"], 1)
        self.assertEqual(sum(e["decision"] == "deferred_max" for e in plan["entries"]), 3)
        self.assertEqual(plan["entries"][0]["decision"], "draft")  # highest tier first
        self.assertEqual(self.ledger.connection.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 0)
        plan, _ = self.plan(since=date(2025, 1, 10), components=["fed:doj/oip"], cadence="monthly")
        self.assertEqual(self.decisions(plan)["fed:doj/oip"], ("draft", ("2025-01-01", "2025-01-31")))
        plan, _ = self.plan(since=date(2025, 1, 10), components=["fed:doj/oip"], cadence="monthly")
        self.assertEqual(self.decisions(plan)["fed:doj/oip"], ("draft", ("2025-02-01", "2025-02-28")))
        with self.assertRaisesRegex(CadenceError, "--since"):
            self.plan(since=TODAY)
        with self.assertRaisesRegex(CadenceError, "--max"):
            self.plan(max_drafts=0)
        with self.assertRaisesRegex(CadenceError, "dry_run"):
            self.plan(config=SimpleNamespace(mode="live", transport_enabled=False))
        with self.assertRaisesRegex(CadenceError, "periodic_closed_request_log"):
            plan_logs(self.ledger, Config(), today=TODAY, profile=self.profile, template=load_template(),
                      output_dir=Path(self.folder.name) / "x", as_of=AS_OF)

    def test_annual_requests_do_not_block_closed_logs(self):
        from foia_requests.ledger import RequestScope
        self.ledger.create_draft(RequestScope(self.profile.requester_principal, "us-federal", "fed:dhs/foia-unit",
                                              "foia_request_log", "2026-01-01", "2026-10-31", "custom"))
        plan, _ = self.plan(components=["fed:dhs/foia-unit"])
        self.assertEqual(self.decisions(plan)["fed:dhs/foia-unit"], ("draft", ("2026-10-01", "2026-10-31")))


if __name__ == "__main__":
    unittest.main()
