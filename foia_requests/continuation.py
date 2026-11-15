"""SFL-11 restart-safe yearly continuation.

A cycle prepares next-period *drafts* for a campaign's components. It never
sends, never authorizes and never inherits an earlier authorization:
authorizations bind exact request IDs (SFL-07), and every new period is a new
semantic request in ``draft``.

* Idempotent: a cycle is unique per (campaign, period); a repeated trigger or
  a restart after a crash resumes the same cycle and per-component items are
  recorded once (``create_draft`` is idempotent on the semantic key).
* Each component is rechecked: canonical alias, current status, verified
  route, public-log coverage and existing duplicates.
* Periods roll forward explicitly (calendar/fiscal). Custom periods need an
  explicit next period. Missed periods are reported, never silently backfilled.
* The scheduler is disabled by default. Enabling requires the ``authorizer``
  role, a timezone and an expiry; even enabled it only prepares drafts.
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime
from zoneinfo import ZoneInfo

from . import directory
from .authorization import require_role
from .ledger import RequestScope, canonical_json, dedupe_key
from .periods import Period, PeriodError, calendar_year, coverage_decision, fiscal_year
from .templates import render, store


class ContinuationError(RuntimeError):
    pass


def next_period(period: Period) -> Period:
    if period.basis == "calendar":
        return calendar_year(period.end.year + 1)
    if period.basis == "fiscal":
        start_month = period.start.month
        return fiscal_year(period.end.year + 1, start_month)
    raise PeriodError("custom periods need an explicit next period")


def due_periods(last: Period, as_of: date):
    """Every period after ``last`` that has fully ended by ``as_of`` (oldest first)."""
    out, current = [], next_period(last)
    while current.end < as_of:
        out.append(current)
        current = next_period(current)
    return out


def cycle_id_for(campaign_id, period: Period):
    return "cyc-" + hashlib.sha256(canonical_json([campaign_id, period.start.isoformat(), period.end.isoformat()])
                                   .encode()).hexdigest()[:24]


def prepare_cycle(ledger, *, campaign_id, components, period: Period, export, profile, template, actor,
                  prior_cycle_id=None, requester_jurisdiction="us-federal", hooks=None):
    hooks = hooks or {}
    cycle_id = cycle_id_for(campaign_id, period)
    now = ledger.clock()
    with ledger.transaction() as conn:
        row = conn.execute("SELECT status FROM continuation_cycles WHERE cycle_id=?", (cycle_id,)).fetchone()
        if row is None:
            conn.execute("INSERT INTO continuation_cycles VALUES (?,?,?,?,?,?,?,?)",
                         (cycle_id, campaign_id, period.start.isoformat(), period.end.isoformat(), period.basis,
                          prior_cycle_id, "preparing", now))
        elif row["status"] == "prepared":
            return summary(ledger, cycle_id)
    for component_id in sorted(set(components)):
        canonical = ledger.canonical_component(component_id)
        with ledger.transaction() as conn:
            if conn.execute("SELECT 1 FROM continuation_items WHERE cycle_id=? AND component_id=?",
                            (cycle_id, canonical)).fetchone():
                continue
            decision, request_id, detail = _decide(ledger, canonical, period, export, profile, template,
                                                   requester_jurisdiction, actor)
            conn.execute("INSERT INTO continuation_items VALUES (?,?,?,?,?)",
                         (cycle_id, canonical, decision, request_id, canonical_json(detail)))
            if "after_item" in hooks:
                hooks["after_item"](canonical)
    with ledger.transaction() as conn:
        conn.execute("UPDATE continuation_cycles SET status='prepared' WHERE cycle_id=?", (cycle_id,))
    return summary(ledger, cycle_id)


def _decide(ledger, component_id, period, export, profile, template, jurisdiction, actor):
    component = directory.component_record(ledger, component_id)
    if component is None or component["status"] != "current":
        return "blocked_component", None, {"status": component["status"] if component else "unknown"}
    coverage = coverage_decision(component_id, period, export, ledger.canonical_component)
    if coverage["decision"] == "suppress_full_public_coverage":
        return "suppressed_public_coverage", None, {"evidence": coverage["evidence"]}
    scope = RequestScope(profile.requester_principal, jurisdiction, component_id, "foia_request_log",
                         period.start.isoformat(), period.end.isoformat(), period.basis)
    existing = ledger.connection.execute("SELECT request_id, state FROM requests WHERE dedupe_key=?",
                                         (dedupe_key(scope, component_id),)).fetchone()
    if existing is not None and existing["state"] != "draft":
        return "existing_request", existing["request_id"], {"state": existing["state"]}
    routes = directory.routes_for(ledger, component_id)
    route = next((r for r in routes if r["state"] == "verified" and r["kind"] in ("email", "portal")), None)
    rendered = render(template, scope, {"component_id": component_id, "name": component["name"],
                                        "agency_name": component["agency_name"]}, route, profile)
    request_id, _, _ = store(ledger, rendered, actor=actor)
    return "draft", request_id, {"missing_inputs": list(rendered.missing_inputs),
                                 "uncovered": coverage["uncovered"]}


def summary(ledger, cycle_id):
    cycle = ledger.connection.execute("SELECT * FROM continuation_cycles WHERE cycle_id=?", (cycle_id,)).fetchone()
    items = [dict(r) for r in ledger.connection.execute(
        "SELECT component_id, decision, request_id FROM continuation_items WHERE cycle_id=? ORDER BY component_id",
        (cycle_id,))]
    return {"cycle_id": cycle_id, "status": cycle["status"], "period": [cycle["period_start"], cycle["period_end"],
                                                                        cycle["period_basis"]],
            "items": items, "sends": 0, "authorizations_created": 0}


def enable_schedule(ledger, campaign_id, operator, *, timezone, basis, expires_at):
    require_role(ledger, operator, "authorizer")
    ZoneInfo(timezone)  # validates the zone name
    if basis not in ("calendar", "fiscal"):
        raise ContinuationError("scheduled continuation supports calendar or fiscal periods only")
    if not expires_at:
        raise ContinuationError("a schedule needs an expiry")
    with ledger.transaction() as conn:
        conn.execute("INSERT INTO continuation_schedules VALUES (?,?,?,?,?,?,?) ON CONFLICT(campaign_id) DO UPDATE "
                     "SET enabled=1, timezone=excluded.timezone, basis=excluded.basis, expires_at=excluded.expires_at,"
                     " set_by=excluded.set_by, set_at=excluded.set_at",
                     (campaign_id, 1, timezone, basis, expires_at, operator, ledger.clock()))


def scheduled_tick(ledger, campaign_id, now_utc: datetime, last: Period):
    """What a scheduler may do now: never more than report the due periods."""
    row = ledger.connection.execute("SELECT * FROM continuation_schedules WHERE campaign_id=?",
                                    (campaign_id,)).fetchone()
    if row is None or not row["enabled"]:
        return {"status": "disabled", "due": []}
    if datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00")) <= now_utc:
        return {"status": "expired", "due": []}
    local_today = now_utc.astimezone(ZoneInfo(row["timezone"])).date()
    due = due_periods(last, local_today)
    return {"status": "due" if due else "idle", "due": [[p.start.isoformat(), p.end.isoformat(), p.basis] for p in due],
            "missed": len(due) > 1, "note": "prepare_cycle must be called per period; nothing is sent"}
