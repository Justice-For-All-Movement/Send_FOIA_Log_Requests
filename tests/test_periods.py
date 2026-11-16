import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path

from foia_requests.aggregator_contract import ContractError, load_export
from foia_requests.ledger import Ledger
from foia_requests.periods import (Period, PeriodError, calendar_year, coverage_decision, custom,
                                   explicit_periods, fiscal_year, overlapping, scope_manifest)

ROOT = Path(__file__).resolve().parents[1]
EXPORT_DIR = ROOT / "fixtures" / "aggregator_export"


class PeriodTests(unittest.TestCase):
    def test_calendar_fiscal_and_leap_years(self):
        self.assertEqual(calendar_year(2024).days, 366)
        self.assertEqual(calendar_year(2023).days, 365)
        fy = fiscal_year(2024)
        self.assertEqual((fy.start, fy.end, fy.basis), (date(2023, 10, 1), date(2024, 9, 30), "fiscal"))
        self.assertEqual(fy.days, 366)  # includes 2024-02-29
        self.assertEqual(fiscal_year(2025, start_month=7).iso(), ("2024-07-01", "2025-06-30"))

    def test_no_implicit_year_range(self):
        with self.assertRaises(PeriodError):
            explicit_periods([])
        with self.assertRaises(PeriodError):
            explicit_periods([2024], basis="custom")
        self.assertEqual(explicit_periods([2024, 2023, 2024]), [calendar_year(2023), calendar_year(2024)])
        with self.assertRaises(PeriodError):
            Period(date(2024, 2, 1), date(2024, 1, 1))

    def test_overlap_and_backfill_detection(self):
        pairs = overlapping({calendar_year(2023), fiscal_year(2024), calendar_year(2021)})
        self.assertEqual(pairs, [(calendar_year(2023), fiscal_year(2024))])


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.export = load_export(EXPORT_DIR)

    def decide(self, component, period, resolve=lambda c: c):
        return coverage_decision(component, period, self.export, resolve)

    def test_full_log_suppresses_draft(self):
        result = self.decide("fed:doj/oip", calendar_year(2023))
        self.assertEqual(result["decision"], "suppress_full_public_coverage")
        self.assertEqual(result["uncovered"], [])

    def test_leap_day_gap_keeps_draft(self):
        result = self.decide("fed:doj/oip", calendar_year(2024))
        self.assertEqual(result["decision"], "draft")
        self.assertEqual(result["uncovered"], [["2024-02-29", "2024-02-29"]])

    def test_cross_year_fiscal_partially_covered_by_two_records(self):
        result = self.decide("fed:doj/oip", fiscal_year(2023))  # 2022-10-01..2023-09-30
        self.assertEqual(result["decision"], "draft")
        self.assertEqual(result["uncovered"], [["2022-10-01", "2022-12-31"]])
        self.assertTrue(any("unknown availability" in r for r in result["reasons"]))
        full = self.decide("fed:doj/oip", custom("2021-10-01", "2022-09-30"))
        self.assertEqual(full["decision"], "suppress_full_public_coverage")

    def test_annual_report_link_only_partial_and_missing_fields_never_suppress(self):
        cases = {("fed:dhs/foia-unit", calendar_year(2024)): "annual_report does not satisfy",
                 ("fed:doj/oip", calendar_year(2020)): "log_link_only does not satisfy",
                 ("fed:doj/foia-unit", fiscal_year(2024)): "partial availability",
                 ("fed:doj/oip", calendar_year(2019)): "missing fields disposition"}
        for (component, period), reason in cases.items():
            with self.subTest(component=component, period=period):
                result = self.decide(component, period)
                self.assertEqual(result["decision"], "draft")
                self.assertTrue(any(reason in r for r in result["reasons"]), result["reasons"])

    def test_unknown_component_has_no_public_log(self):
        result = self.decide("fed:doe/legacy", calendar_year(2023))
        self.assertEqual((result["decision"], result["reasons"]), ("draft", ["no public log found in export"]))

    def test_component_merger_resolves_through_ledger_aliases(self):
        self.assertEqual(self.decide("fed:epa/region-x", calendar_year(2022))["decision"], "draft")
        with tempfile.TemporaryDirectory() as folder:
            ledger = Ledger(Path(folder) / "l" / "ledger.sqlite")
            try:
                ledger.add_component_alias("fed:epa/old-region", "fed:epa/region-x", "merger")
                result = self.decide("fed:epa/region-x", calendar_year(2022), ledger.canonical_component)
                self.assertEqual(result["decision"], "suppress_full_public_coverage")
            finally:
                ledger.close()

    def test_scope_manifest_is_reproducible_and_reports_corpus_gate(self):
        components = ["fed:doj/oip", "fed:dhs/foia-unit"]
        periods = [calendar_year(2023), calendar_year(2024), fiscal_year(2024)]
        first = scope_manifest(components, periods, self.export)
        second = scope_manifest(list(reversed(components)), list(reversed(periods)), self.export)
        self.assertEqual(first["manifest_sha256"], second["manifest_sha256"])
        self.assertEqual(len(first["entries"]), 6)
        self.assertFalse(first["corpus_gate"]["ext_corpus_satisfied"])
        self.assertTrue(first["corpus_gate"]["fixture_mode"])
        self.assertEqual(len(first["period_overlaps"]), 2)


class ContractTests(unittest.TestCase):
    def mutate_and_load(self, mutate_manifest=None, mutate_rows=None):
        with tempfile.TemporaryDirectory() as folder:
            copy_dir = Path(folder) / "export"
            shutil.copytree(EXPORT_DIR, copy_dir)
            manifest = json.loads((copy_dir / "manifest.json").read_text())
            rows = [json.loads(l) for l in (copy_dir / "coverage.jsonl").read_text().splitlines()]
            if mutate_rows:
                mutate_rows(rows)
                data = "".join(json.dumps(r) + "\n" for r in rows)
                (copy_dir / "coverage.jsonl").write_text(data)
                manifest["records"]["sha256"] = hashlib.sha256(data.encode()).hexdigest()
                manifest["records"]["count"] = len(rows)
            if mutate_manifest:
                mutate_manifest(manifest)
            (copy_dir / "manifest.json").write_text(json.dumps(manifest))
            return load_export(copy_dir)

    def test_rejections(self):
        cases = [
            (lambda m: m.update(fixture_mode=False), None),
            (lambda m: m.update(contract_version="fc13-v9"), None),
            (lambda m: m["records"].update(count=1), None),
            (lambda m: m["records"].update(sha256="0" * 64), None),
            (lambda m: m["records"].update(path="../coverage.jsonl"), None),
            (None, lambda rows: rows[0].update(synthetic=False)),
            (None, lambda rows: rows[0].update(availability="complete")),
            (None, lambda rows: rows[1].update(date_gaps=[["2025-01-01", "2025-01-02"]])),
            (None, lambda rows: rows[0].update(artifact_sha256=None)),
            (None, lambda rows: rows.append(copy.deepcopy(rows[0]))),
        ]
        for manifest_change, row_change in cases:
            with self.subTest(manifest=manifest_change, rows=row_change), self.assertRaises(ContractError):
                self.mutate_and_load(manifest_change, row_change)

    def test_live_export_requires_pinned_commit_and_accepted_review_for_gate(self):
        def live(manifest):
            manifest.update(fixture_mode=False, producer={"name": "FOIA_Aggregator", "commit": "a" * 40},
                            fc14_review={"status": "accepted", "reference": "synthetic"})

        def unsynthetic(rows):
            for row in rows:
                row["synthetic"] = False
        export = self.mutate_and_load(live, unsynthetic)
        self.assertTrue(export.corpus_gate_satisfied)
        with self.assertRaises(ContractError):
            self.mutate_and_load(lambda m: (live(m), m["producer"].update(commit="main")), unsynthetic)


if __name__ == "__main__":
    unittest.main()


class CustomRangeTests(unittest.TestCase):
    TODAY = date(2026, 11, 16)

    def test_last_week_is_seven_complete_days_ending_yesterday(self):
        from foia_requests.periods import last_days
        period = last_days(7, self.TODAY)
        self.assertEqual((period.iso(), period.basis, period.days), (("2026-11-09", "2026-11-15"), "custom", 7))
        for bad in (0, -1, 3661, "7", True):
            with self.assertRaises(PeriodError):
                last_days(bad, self.TODAY)

    def test_select_period_validation(self):
        from foia_requests.periods import select_period
        period, warnings = select_period(start="2026-01-01", end="2026-03-31", today=self.TODAY)
        self.assertEqual((period.iso(), warnings), (("2026-01-01", "2026-03-31"), []))
        with self.assertRaisesRegex(PeriodError, "future"):
            select_period(start="2026-11-01", end="2026-11-17", today=self.TODAY)
        with self.assertRaisesRegex(PeriodError, "inverted"):
            select_period(start="2026-03-01", end="2026-02-01", today=self.TODAY)
        with self.assertRaisesRegex(PeriodError, "YYYY-MM-DD"):
            select_period(start="2026-02-30", end="2026-03-01", today=self.TODAY)
        with self.assertRaisesRegex(PeriodError, "together"):
            select_period(start="2026-01-01", today=self.TODAY)
        with self.assertRaisesRegex(PeriodError, "exactly one"):
            select_period(last=7, calendar=2024, today=self.TODAY)
        with self.assertRaisesRegex(PeriodError, "exactly one"):
            select_period(today=self.TODAY)
        with self.assertRaisesRegex(PeriodError, "precedes"):
            select_period(start="1960-01-01", end="1960-12-31", today=self.TODAY)
        with self.assertRaisesRegex(PeriodError, "future"):
            select_period(calendar=2026, today=self.TODAY)  # year not yet over

    def test_recent_ranges_warn_but_are_allowed(self):
        from foia_requests.periods import select_period
        _, warnings = select_period(last=7, today=self.TODAY)
        self.assertEqual(len(warnings), 1)
        self.assertIn("slow to release", warnings[0])
        _, warnings = select_period(start="2026-11-10", end="2026-11-16", today=self.TODAY)
        self.assertTrue(any("ends today" in w for w in warnings))

    def test_annual_selectors_unchanged(self):
        from foia_requests.periods import select_period
        self.assertEqual(select_period(calendar=2024, today=self.TODAY), (calendar_year(2024), []))
        self.assertEqual(select_period(fiscal=2025, today=self.TODAY), (fiscal_year(2025), []))

    def test_gap_analysis_on_short_custom_range(self):
        export = load_export(EXPORT_DIR)
        inside = coverage_decision("fed:doj/oip", custom("2023-03-01", "2023-03-07"), export)
        self.assertEqual(inside["decision"], "suppress_full_public_coverage")
        recent = coverage_decision("fed:doj/oip", custom("2026-11-09", "2026-11-15"), export)
        self.assertEqual((recent["decision"], recent["uncovered_days"]), ("draft", 7))
