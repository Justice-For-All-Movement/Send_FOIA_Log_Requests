"""SFL-03 period selection (annual or custom date range) and public-log gap analysis.

Periods are explicit: callers pass the years/bases or exact dates they want.
Custom ranges (``--start/--end`` or ``--last-days N``) are validated by
``check_range``: future end dates are rejected and recent ranges carry review
warnings, because agencies may log or release very recent requests late. A
FOIA request for such a range asks for "the log of requests received between
X and Y"; the same template and coverage analysis apply to any range. Nothing infers a
historical range (no 1993-2025 default borrowed from FOIA_Bias_Analysis).
Coverage decisions compare a component-period against Aggregator log-coverage
records (``aggregator_contract``):

* Only ``foia_request_log`` records with ``availability: full`` whose union
  covers every day of the period, with no date gaps or missing fields, can
  suppress a draft. Annual reports and bare links never count.
* Partial or unknown coverage produces a draft and lists the uncovered days.
* Component aliases (renames/mergers) resolve through the ledger.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .directory import component_id as make_component_id
from .ledger import canonical_json, sha256_text

BASES = ("calendar", "fiscal", "custom")
ONE_DAY = timedelta(days=1)


class PeriodError(ValueError):
    pass


@dataclass(frozen=True, order=True)
class Period:
    start: date
    end: date
    basis: str = "calendar"

    def __post_init__(self):
        if not isinstance(self.start, date) or not isinstance(self.end, date) or self.start > self.end:
            raise PeriodError("period requires start <= end dates")
        if self.basis not in BASES:
            raise PeriodError("unknown period basis")

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def iso(self):
        return self.start.isoformat(), self.end.isoformat()


def calendar_year(year: int) -> Period:
    return Period(date(year, 1, 1), date(year, 12, 31), "calendar")


def fiscal_year(year: int, start_month: int = 10) -> Period:
    """Fiscal year *named* by its ending year (US federal FY2024 = 2023-10-01..2024-09-30)."""
    if not 1 <= start_month <= 12:
        raise PeriodError("start_month must be 1..12")
    if start_month == 1:
        return Period(date(year, 1, 1), date(year, 12, 31), "fiscal")
    return Period(date(year - 1, start_month, 1), date(year, start_month, 1) - ONE_DAY, "fiscal")


def custom(start: str, end: str) -> Period:
    return Period(date.fromisoformat(start), date.fromisoformat(end), "custom")


FOIA_EARLIEST = date(1967, 7, 4)  # FOIA took effect July 4, 1967
MAX_LAST_DAYS = 3660
RECENT_WARNING_DAYS = 30


def last_days(n: int, today: date) -> Period:
    """The ``n`` complete days ending yesterday (``today`` is excluded as still in progress).

    ``last_days(7, date(2026, 11, 16))`` is 2026-11-09..2026-11-15 ("the last week").
    """
    if type(n) is not int or not 1 <= n <= MAX_LAST_DAYS:
        raise PeriodError(f"--last-days must be an integer 1..{MAX_LAST_DAYS}")
    end = today - ONE_DAY
    return Period(end - timedelta(days=n - 1), end, "custom")


def check_range(period: Period, today: date):
    """Validate an operator-chosen range against ``today``; return review warnings.

    Rejects end dates after ``today`` and ranges before FOIA existed. Warns, but
    allows, recent periods: agencies log and publish with a delay, so a request
    covering the last few weeks may legitimately produce an incomplete log.
    """
    if period.end > today:
        raise PeriodError(f"end date {period.end.isoformat()} is in the future (today is {today.isoformat()})")
    if period.start < FOIA_EARLIEST:
        raise PeriodError("start date precedes the FOIA (1967-07-04)")
    warnings = []
    if period.end == today:
        warnings.append("range ends today; requests received later today will not be in the log")
    if (today - period.end).days < RECENT_WARNING_DAYS:
        warnings.append(f"range ends within {RECENT_WARNING_DAYS} days of today; agencies may not have logged "
                        "every request yet or may be slow to release very recent log entries")
    return warnings


def select_period(*, start=None, end=None, last=None, calendar=None, fiscal=None, today: date):
    """Exactly one period selector -> (Period, warnings). Annual selectors are unchanged."""
    chosen = [name for name, value in (("--start/--end", start or end), ("--last-days", last),
                                       ("--calendar-year", calendar), ("--fiscal-year", fiscal))
              if value is not None]
    if len(chosen) != 1:
        raise PeriodError("choose exactly one of --start/--end, --last-days, --calendar-year, --fiscal-year")
    if chosen[0] == "--start/--end":
        if not (start and end):
            raise PeriodError("--start and --end must be given together")
        try:
            period = custom(start, end)
        except ValueError as exc:
            if isinstance(exc, PeriodError):
                raise PeriodError(f"inverted range: start {start} is after end {end}") from exc
            raise PeriodError(f"dates must be YYYY-MM-DD: {exc}") from exc
    elif last is not None:
        period = last_days(last, today)
    elif calendar is not None:
        period = explicit_periods([calendar])[0]
    else:
        period = explicit_periods([fiscal], "fiscal")[0]
    return period, check_range(period, today)


def explicit_periods(years, basis="calendar"):
    if not years:
        raise PeriodError("explicit years are required; no implicit historical range")
    if any(type(y) is not int or not 1966 <= y <= 2100 for y in years):
        raise PeriodError("years must be integers in 1966..2100")
    maker = calendar_year if basis == "calendar" else fiscal_year if basis == "fiscal" else None
    if maker is None:
        raise PeriodError("use custom() for custom periods")
    return sorted({maker(y) for y in years})


def overlapping(periods):
    """Pairs of distinct periods that share at least one day (backfill/overlap review)."""
    ordered = sorted(periods)
    out = []
    for i, a in enumerate(ordered):
        for b in ordered[i + 1:]:
            if b.start > a.end:
                break
            out.append((a, b))
    return out


def _merge(intervals):
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1] + ONE_DAY:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _subtract(target, covered):
    """Days of ``target`` (start, end) not inside merged ``covered`` intervals."""
    gaps = []
    cursor = target[0]
    for start, end in _merge(covered):
        if end < cursor or start > target[1]:
            continue
        if start > cursor:
            gaps.append((cursor, min(start - ONE_DAY, target[1])))
        cursor = max(cursor, end + ONE_DAY)
        if cursor > target[1]:
            break
    if cursor <= target[1]:
        gaps.append((cursor, target[1]))
    return gaps


def coverage_decision(component: str, period: Period, export, resolve=lambda cid: cid, jurisdiction="fed"):
    """Decide whether public logs already cover ``component`` for ``period``."""
    relevant, evidence, reasons = [], [], []
    full_intervals = []
    for record in export.records:
        cid = resolve(make_component_id(jurisdiction, record["agency_abbr"], record["component_abbr"]))
        if cid != component:
            continue
        r_start, r_end = date.fromisoformat(record["period_start"]), date.fromisoformat(record["period_end"])
        if r_end < period.start or r_start > period.end:
            continue
        relevant.append(record)
        evidence.append({"coverage_id": record["coverage_id"], "record_class": record["record_class"],
                         "availability": record["availability"], "artifact_sha256": record["artifact_sha256"]})
        if record["record_class"] != "foia_request_log":
            reasons.append(f"{record['coverage_id']}: {record['record_class']} does not satisfy log coverage")
            continue
        if record["availability"] != "full":
            reasons.append(f"{record['coverage_id']}: {record['availability']} availability")
        if record["missing_fields"]:
            reasons.append(f"{record['coverage_id']}: missing fields {', '.join(record['missing_fields'])}")
        if record["availability"] == "full" and not record["missing_fields"]:
            pieces = _subtract((r_start, r_end), [(date.fromisoformat(a), date.fromisoformat(b))
                                                  for a, b in record["date_gaps"]])
            full_intervals.extend(pieces)
            if record["date_gaps"]:
                reasons.append(f"{record['coverage_id']}: date gaps in published log")
    uncovered = _subtract((period.start, period.end), full_intervals)
    if not relevant:
        decision, reasons = "draft", ["no public log found in export"]
    elif not uncovered:
        decision = "suppress_full_public_coverage"
        reasons = ["published full logs cover every day of the period"]
    else:
        decision = "draft"
        if not any("availability" in r or "does not satisfy" in r or "missing" in r or "gaps" in r for r in reasons):
            reasons.append("published logs leave uncovered days")
    return {
        "component_id": component, "period": [period.start.isoformat(), period.end.isoformat(), period.basis],
        "decision": decision, "reasons": reasons,
        "uncovered": [[a.isoformat(), b.isoformat()] for a, b in uncovered],
        "uncovered_days": sum((b - a).days + 1 for a, b in uncovered),
        "evidence": evidence,
    }


def scope_manifest(components, periods, export, resolve=lambda cid: cid, jurisdiction="fed"):
    """Explicit live-scope manifest for review: one decision per component-period."""
    entries = [coverage_decision(c, p, export, resolve, jurisdiction)
               for c in sorted(set(components)) for p in sorted(set(periods))]
    overlaps = [[a.iso(), b.iso()] for a, b in overlapping(set(periods))]
    body = {"entries": entries, "period_overlaps": overlaps, "corpus_gate": export.gate_report()}
    return dict(body, manifest_sha256=sha256_text(canonical_json(body)))
