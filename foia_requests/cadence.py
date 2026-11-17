"""Periodic closed-request log planner (``foia-requests plan-logs``). Drafts only; never sends.

For each selected directory component (all directory agencies by default) the planner finds the *next
un-requested* closed-request period and drafts one ``periodic_closed_request_log`` request for it:

* **Priority and cadence.** ``data/priority.json`` assigns civil-liberties tiers (1..3; unlisted agencies are 3).
  Components are planned in tier order. Cadence is, in precedence order: the per-agency override file, then
  ``--cadence`` for the run, then the tier default (tier 1 monthly, tiers 2-3 quarterly).

* **Never prospective.** A period is planned only after it has fully ended (its last day is before ``today``).
  FOIA reaches records that exist when the agency searches, not records created after the request, so a request
  for a period that has not ended would miss later closures. Periods that ended less than 30 days ago are planned
  with a warning, because agencies may log closures late.
* **One request per component per period.** Existing ledger requests of record class
  ``foia_closed_request_log`` (in any state, including cancelled) block every period they overlap; the planner
  moves to the next period and reports any days a cadence change leaves unrequested.
* **Agencies that publish logs are skipped.** An optional ``--published-logs`` file lists
  ``{agency_abbr, component_abbr|null, index_url, cadence}``; ``component_abbr: null`` covers the whole agency.
  The FOIA Aggregator scrapes those logs, so asking for them again would only add agency work.
  An entry marked ``always_request`` in the priority list (CIA, which publishes no log) is never skipped this way.
* **One period per component per run.** There is no per-run cap unless ``--max`` is given. A backlog
  (``--since``) is caught up one period per run, not in a burst.

Nothing here schedules itself. A deployment may run it monthly or quarterly; it should never run hourly: agencies
may aggregate repeated requests from one requester, and frequent runs cannot produce new eligible periods.

Drafts are rendered with the same pure ``templates.render`` used by ``prepare`` and written to a private plan
folder (``plan.json`` + ``plan.md``). Unless ``record=False`` each draft is also stored in the ledger as a
``draft`` request so later runs skip its period. Storing a draft is not authorization; the sending path accepts
only the annual template today, so these drafts cannot be sent without a further reviewed change.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from . import directory
from .dryrun import _private_dir, _write_private, choose_route
from .ledger import RequestScope, canonical_json, dedupe_key, sha256_text
from .periods import FOIA_EARLIEST, RECENT_WARNING_DAYS, Period
from .prepare import _fence, _md, _route_summary, select_components
from .templates import render, store

CADENCES = ("monthly", "quarterly")
TIER_CADENCE = {1: "monthly", 2: "quarterly", 3: "quarterly"}
DEFAULT_TIER = 3
PRIORITY_PATH = Path(__file__).resolve().parent / "data" / "priority.json"
RECORD_CLASS = "foia_closed_request_log"
TEMPLATE_ID = "periodic_closed_request_log"
PUBLISHED_CADENCES = ("daily", "weekly", "monthly", "quarterly", "semiannual", "annual", "irregular", "unknown")
PLAN_JSON = "plan.json"
PLAN_MD = "plan.md"
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_ENTRIES = 10000
ONE_DAY = timedelta(days=1)


class CadenceError(ValueError):
    pass


# -- periods -------------------------------------------------------------------------------------------------
def period_containing(day: date, cadence: str) -> Period:
    """The calendar month or calendar quarter that contains ``day``."""
    if cadence not in CADENCES:
        raise CadenceError(f"cadence must be one of {', '.join(CADENCES)}")
    first_month = day.month if cadence == "monthly" else 3 * ((day.month - 1) // 3) + 1
    start = date(day.year, first_month, 1)
    months = 1 if cadence == "monthly" else 3
    year, month = divmod(first_month - 1 + months, 12)
    return Period(start, date(day.year + year, month + 1, 1) - ONE_DAY, "custom")


def next_period(period: Period, cadence: str) -> Period:
    return period_containing(period.end + ONE_DAY, cadence)


def latest_ended(today: date, cadence: str) -> Period:
    """The most recent period whose last day is before ``today``."""
    return period_containing(period_containing(today, cadence).start - ONE_DAY, cadence)


# -- input files ---------------------------------------------------------------------------------------------
def _read_json_list(path, name):
    raw = Path(path).read_bytes()
    if len(raw) > MAX_FILE_BYTES:
        raise CadenceError(f"{name} file exceeds {MAX_FILE_BYTES} bytes")
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, list) or len(data) > MAX_ENTRIES:
        raise CadenceError(f"{name} file must be a JSON list of at most {MAX_ENTRIES} entries")
    return data


def _abbr(value, name, index, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.strip():
        raise CadenceError(f"entry {index}: {name} must be nonempty text" + (" or null" if nullable else ""))
    try:
        return directory.slug(value)
    except directory.DirectoryError as exc:
        raise CadenceError(f"entry {index}: {exc}") from exc


def load_published_logs(path):
    """Validate a published-logs file: ``[{agency_abbr, component_abbr|null, index_url, cadence}, ...]``."""
    out = []
    for index, entry in enumerate(_read_json_list(path, "published-logs")):
        if not isinstance(entry, dict) or set(entry) != {"agency_abbr", "component_abbr", "index_url", "cadence"}:
            raise CadenceError(f"entry {index}: fields must be agency_abbr, component_abbr, index_url, cadence")
        url = entry["index_url"]
        parts = urlsplit(url) if isinstance(url, str) else None
        if parts is None or parts.scheme != "https" or not parts.hostname or any(ord(c) < 33 for c in url):
            raise CadenceError(f"entry {index}: index_url must be an https URL")
        if entry["cadence"] not in PUBLISHED_CADENCES:
            raise CadenceError(f"entry {index}: cadence must be one of {', '.join(PUBLISHED_CADENCES)}")
        out.append({"agency": _abbr(entry["agency_abbr"], "agency_abbr", index),
                    "component": _abbr(entry["component_abbr"], "component_abbr", index, nullable=True),
                    "index_url": url, "cadence": entry["cadence"]})
    return out


def load_cadence_overrides(path):
    """Validate per-agency overrides: ``[{agency_abbr, component_abbr|null, cadence}, ...]``."""
    out, seen = [], set()
    for index, entry in enumerate(_read_json_list(path, "cadence overrides")):
        if not isinstance(entry, dict) or set(entry) != {"agency_abbr", "component_abbr", "cadence"}:
            raise CadenceError(f"entry {index}: fields must be agency_abbr, component_abbr, cadence")
        if entry["cadence"] not in CADENCES:
            raise CadenceError(f"entry {index}: cadence must be one of {', '.join(CADENCES)}")
        key = (_abbr(entry["agency_abbr"], "agency_abbr", index),
               _abbr(entry["component_abbr"], "component_abbr", index, nullable=True))
        if key in seen:
            raise CadenceError(f"entry {index}: duplicate override for {key[0]}/{key[1] or '*'}")
        seen.add(key)
        out.append({"agency": key[0], "component": key[1], "cadence": entry["cadence"]})
    return out


def load_priority(path=PRIORITY_PATH):
    """Validate ``sfl-priority/1``: ``{"entries": {"AGENCY" | "AGENCY/COMPONENT": {tier, reason[, always_request]}}}``."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or doc.get("format") != "sfl-priority/1" or set(doc) != {"format", "note", "entries"}:
        raise CadenceError("priority file must be format sfl-priority/1 with note and entries")
    out = {}
    for key, entry in doc["entries"].items():
        if not isinstance(entry, dict) or not {"tier", "reason"} <= set(entry) <= {"tier", "reason", "always_request"}:
            raise CadenceError(f"priority {key}: fields must be tier, reason and optional always_request")
        if entry["tier"] not in (1, 2, 3) or type(entry["tier"]) is not int:
            raise CadenceError(f"priority {key}: tier must be 1, 2 or 3")
        if not isinstance(entry["reason"], str) or not entry["reason"].strip() or "\n" in entry["reason"]:
            raise CadenceError(f"priority {key}: reason must be one line of text")
        if type(entry.get("always_request", False)) is not bool:
            raise CadenceError(f"priority {key}: always_request must be a boolean")
        agency, _, component = key.partition("/")
        slug_key = (_abbr(agency, "agency", key), _abbr(component, "component", key) if component else None)
        if slug_key in out:
            raise CadenceError(f"priority {key}: duplicate entry")
        out[slug_key] = {"tier": entry["tier"], "reason": entry["reason"],
                         "always_request": entry.get("always_request", False), "key": key}
    return out


def priority_for(component_id, priority):
    """Most specific priority entry (component, else agency); unlisted components are tier 3."""
    agency, component = _slugs(component_id)
    found = priority.get((agency, component)) or priority.get((agency, None))
    if found is None:
        # FOIA.gov may list a component (e.g. FBI) as its own agency; match its component slug as an agency key.
        found = priority.get((component, None))
    return found or {"tier": DEFAULT_TIER, "reason": "not on the priority list", "always_request": False,
                     "key": None}


def _slugs(component_id):
    agency, _, component = component_id.split(":", 1)[-1].partition("/")
    return agency, component


def cadence_for(component_id, run_cadence, overrides, tier=DEFAULT_TIER):
    """(cadence, source): override file, else the run's ``--cadence``, else the tier default."""
    agency, component = _slugs(component_id)
    exact = [o for o in overrides if o["agency"] == agency and o["component"] == component]
    agency_wide = [o for o in overrides if o["agency"] == agency and o["component"] is None]
    chosen = (exact or agency_wide or [None])[0]
    if chosen:
        return chosen["cadence"], "override"
    if run_cadence is not None:
        return run_cadence, "--cadence"
    return TIER_CADENCE[tier], f"tier {tier} default"


def published_entries(component_id, published):
    agency, component = _slugs(component_id)
    return [p for p in published if p["agency"] == agency and p["component"] in (None, component)]


# -- planning ------------------------------------------------------------------------------------------------
def _existing(ledger, requester, canonical):
    rows = ledger.connection.execute(
        "SELECT request_id, state, period_start, period_end FROM requests WHERE requester=? AND component_id=? "
        "AND record_class=? ORDER BY period_end, request_id", (requester, canonical, RECORD_CLASS)).fetchall()
    return [dict(r) for r in rows]


def _overlap(period, existing):
    start, end = period.iso()
    return next((r for r in existing if r["period_start"] <= end and r["period_end"] >= start), None)


def next_unrequested(existing, cadence, today, since=None):
    """(Period, notes) for the next period not overlapping ``existing``; the period may not have ended yet."""
    notes = []
    if existing:
        last_end = date.fromisoformat(max(r["period_end"] for r in existing))
        candidate = period_containing(last_end + ONE_DAY, cadence)
    elif since is not None:
        candidate = period_containing(since, cadence)
    else:
        candidate = latest_ended(today, cadence)
    first_try = candidate
    while (clash := _overlap(candidate, existing)) is not None:
        candidate = next_period(candidate, cadence)
    if candidate != first_try:
        clash_end = date.fromisoformat(max(r["period_end"] for r in existing if r["period_start"] <=
                                           first_try.end.isoformat()))
        gap_start = clash_end + ONE_DAY
        if gap_start < candidate.start:
            notes.append(f"cadence change: {gap_start.isoformat()}..{(candidate.start - ONE_DAY).isoformat()} "
                         "overlaps no request but is shorter than one aligned period; request it explicitly "
                         "if needed")
        notes.append(f"{first_try.start.isoformat()}..{first_try.end.isoformat()} overlaps an existing request; "
                     "moved to the next aligned period")
    return candidate, notes


def _entry(ledger, component_id, *, today, run_cadence, overrides, published, priority, since, profile, template,
           jurisdiction):
    canonical = ledger.canonical_component(component_id)
    component = directory.component_record(ledger, component_id)
    rank = priority_for(canonical, priority)
    entry = {"component_id": component_id, "canonical_component_id": canonical,
             "agency_name": component["agency_name"], "name": component["name"], "status": component["status"],
             "tier": rank["tier"], "priority_reason": rank["reason"]}
    if canonical != component_id:
        entry.update(included=False, decision="excluded_alias",
                     why=[f"{component_id} is an alias of {canonical}; the canonical component is planned instead"])
        return entry, None
    if component["status"] != "current":
        entry.update(included=False, decision="excluded_not_current",
                     why=[f"directory status is {component['status']} (only current components are planned)"])
        return entry, None
    logs = published_entries(canonical, published)
    notes = []
    if logs and rank["always_request"]:
        notes.append(f"published-logs file lists this component, but the priority list marks it always_request "
                     f"({rank['reason']})")
    elif logs:
        entry.update(included=False, decision="excluded_publishes_logs",
                     why=[f"publishes FOIA logs ({p['cadence']}) at {p['index_url']}; the FOIA Aggregator "
                          "collects them" for p in logs])
        return entry, None
    cadence, source = cadence_for(canonical, run_cadence, overrides, rank["tier"])
    existing = _existing(ledger, profile.requester_principal, canonical)
    period, period_notes = next_unrequested(existing, cadence, today, since)
    notes += period_notes
    entry.update(cadence=cadence, cadence_source=source, period=[period.start.isoformat(), period.end.isoformat()],
                 prior_requests=len(existing))
    if period.end >= today:
        entry.update(included=False, decision="not_due",
                     why=notes + [f"next {cadence} period {period.start.isoformat()}..{period.end.isoformat()} has "
                                  f"not ended; it can be planned from {(period.end + ONE_DAY).isoformat()} "
                                  "(no prospective requests)"])
        return entry, None
    scope = RequestScope(profile.requester_principal, jurisdiction, canonical, RECORD_CLASS,
                         period.start.isoformat(), period.end.isoformat(), "custom")
    warnings = []
    if (today - period.end).days < RECENT_WARNING_DAYS:
        warnings.append(f"period ended {(today - period.end).days} day(s) ago (< {RECENT_WARNING_DAYS}); the agency "
                        "may not have logged every closure yet")
    routes = directory.routes_for(ledger, canonical)
    route = choose_route(routes)
    why = [f"current directory component; tier {rank['tier']} ({rank['reason']}); {cadence} cadence ({source})"]
    why += (notes if logs else ["no published log listed"] + notes)
    if existing:
        why.append(f"{len(existing)} earlier closed-log request(s); latest ends {existing[-1]['period_end']}")
    if route is None:
        why.append("no submission route on file: find an official route before any send")
    elif route["state"] != "verified" or route["kind"] not in ("email", "portal"):
        why.append(f"no verified electronic route; best available is {route['kind']} ({route['state']})")
    else:
        why.append(f"route: verified {route['kind']}")
    rendered = render(template, scope, {"component_id": canonical, "name": component["name"],
                                        "agency_name": component["agency_name"]}, route, profile)
    entry.update(included=True, decision="draft", why=why, warnings=warnings,
                 dedupe_key=dedupe_key(scope, canonical), route=_route_summary(route), subject=rendered.subject,
                 body=rendered.body, content_sha256=rendered.content_sha256,
                 missing_inputs=list(rendered.missing_inputs), live_ready=rendered.live_ready)
    return entry, rendered


def plan_logs(ledger, config, *, today: date, profile, template, output_dir, as_of, cadence=None,
              overrides=(), published=(), priority=None, agencies=None, components=None, max_drafts=None,
              since=None, record=True, jurisdiction="us-federal"):
    """Plan the next closed-request period per component; write ``plan.json``/``plan.md``; return (plan, folder)."""
    if config.mode != "dry_run" or config.transport_enabled:
        raise CadenceError("plan-logs requires mode=dry_run with transport disabled")
    if cadence is not None and cadence not in CADENCES:
        raise CadenceError(f"cadence must be one of {', '.join(CADENCES)}")
    priority = load_priority() if priority is None else priority
    if template.template_id != TEMPLATE_ID or template.record_class != RECORD_CLASS:
        raise CadenceError(f"plan-logs needs the {TEMPLATE_ID} template")
    if max_drafts is not None and (type(max_drafts) is not int or max_drafts < 1):
        raise CadenceError("--max must be a positive integer")
    if since is not None and not FOIA_EARLIEST <= since < today:
        raise CadenceError("--since must be a past date on or after 1967-07-04")
    entries, recorded = [], []
    drafted = 0
    chosen = select_components(ledger, agencies, components)
    ordered = sorted(chosen, key=lambda c: (priority_for(ledger.canonical_component(c), priority)["tier"], c))
    for component_id in ordered:
        entry, rendered = _entry(ledger, component_id, today=today, run_cadence=cadence, overrides=overrides,
                                 published=published, priority=priority, since=since, profile=profile,
                                 template=template, jurisdiction=jurisdiction)
        if rendered is not None and max_drafts is not None and drafted >= max_drafts:
            for key in ("subject", "body", "content_sha256", "missing_inputs", "live_ready", "route", "warnings"):
                entry.pop(key, None)
            entry.update(included=False, decision="deferred_max",
                         why=[f"--max {max_drafts} reached; planned again on the next run"])
            rendered = None
        if rendered is not None:
            drafted += 1
            if record:
                request_id, revision, _ = store(ledger, rendered, actor="plan-logs")
                entry.update(request_id=request_id, ledger_revision=revision)
                recorded.append(request_id)
        entries.append(entry)
    blocking = sorted({m for e in entries if e["included"] for m in e["missing_inputs"]})
    body = {
        "plan_kind": "closed-log-plan/1", "mode": "dry_run", "as_of": as_of, "today": today.isoformat(),
        "run_cadence": cadence, "tier_cadence": {str(k): v for k, v in TIER_CADENCE.items()}, "since": since.isoformat() if since else None, "max": max_drafts,
        "filters": {"agencies": sorted({a.strip().lower() for a in agencies or []}),
                    "components": sorted(set(components or []))},
        "template": {"key": template.key, "sha256": template.sha256, "status": template.status},
        "profile_id": profile.profile_id, "synthetic_profile": profile.synthetic,
        "published_log_entries": len(published), "cadence_overrides": len(overrides),
        "tiers": {str(t): sum(e["tier"] == t for e in entries) for t in (1, 2, 3)},
        "entries": entries, "blocking_inputs": blocking, "drafts": drafted,
        "ledger_drafts_recorded": len(recorded), "outbound_messages_sent": 0, "transport": "none",
        "live_dispatch_authorized": False,
        "notice": "Plan and drafts only. Nothing was sent. Review of this plan does not authorize dispatch.",
    }
    plan = dict(body, plan_sha256=sha256_text(canonical_json(body)))
    out = _private_dir(Path(output_dir))
    _write_private(out / PLAN_MD, render_markdown(plan))
    _write_private(out / PLAN_JSON, json.dumps(plan, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    return plan, out


def render_markdown(plan):
    included = [e for e in plan["entries"] if e["included"]]
    lines = [
        f"# Closed-request log plan: {plan['today']}", "",
        "**DRY RUN. Nothing was sent to any agency, e-mail address or portal. This plan does not authorize "
        "dispatch.**", "",
        f"- Prepared at: {_md(plan['as_of'])} (reference date {plan['today']})",
        f"- Cadence: {plan['run_cadence'] or 'tier default (tier 1 monthly, tiers 2-3 quarterly)'}; "
        f"overrides: {plan['cadence_overrides']}; "
        f"published-log entries: {plan['published_log_entries']}",
        f"- Template: `{plan['template']['key']}` — status **`{plan['template']['status']}`**",
        f"- Requester profile: {_md(plan['profile_id'])}"
        + (" (SYNTHETIC fixture, not a real requester)" if plan["synthetic_profile"] else ""),
        f"- Components considered: {len(plan['entries'])}; drafts: {plan['drafts']}"
        + (f" (cap {plan['max']})" if plan["max"] else "")
        + f"; recorded in ledger: {plan['ledger_drafts_recorded']}",
        f"- Outbound messages sent: {plan['outbound_messages_sent']}", "",
        "Only periods that have fully ended are planned; FOIA does not reach records created after a request.", "",
    ]
    if plan["blocking_inputs"]:
        lines += ["## Inputs still missing before any live send", ""]
        lines += [f"- {_md(m)}" for m in plan["blocking_inputs"]] + [""]
    lines += ["## Plan (priority order)", "", "| # | Tier | Agency | Component | Cadence | Period | Decision |",
              "|---|---|---|---|---|---|---|"]
    for n, e in enumerate(plan["entries"], 1):
        period = "..".join(e["period"]) if e.get("period") else "—"
        lines.append(f"| {n} | {e['tier']} | {_md(e['agency_name'])} | {_md(e['name'])} `{e['component_id']}` | "
                     f"{e.get('cadence', '—')} | {period} | {e['decision']} |")
    lines += ["", "## Drafts", ""]
    if not included:
        lines += ["No drafts in this run.", ""]
    for n, e in enumerate(plan["entries"], 1):
        if not e["included"]:
            continue
        route = e["route"]
        lines += [f"### {n}. {_md(e['agency_name'])} — {_md(e['name'])}", "",
                  f"- Component: `{e['component_id']}`; period {e['period'][0]} to {e['period'][1]}",
                  f"- Priority: tier {e['tier']} — {_md(e['priority_reason'])}",
                  "- Route: " + (f"{route['kind']} → {_md(route['destination'])} ({route['state']})"
                                 if route else "none on file"),
                  f"- Content SHA-256: `{e['content_sha256']}`"]
        if e.get("request_id"):
            lines.append(f"- Ledger draft: `{e['request_id']}`")
        lines += ["- Why:"] + [f"  - {_md(w)}" for w in e["why"]]
        if e["warnings"]:
            lines += ["- Warnings:"] + [f"  - {_md(w)}" for w in e["warnings"]]
        if e["missing_inputs"]:
            lines += ["- Missing before live use:"] + [f"  - {_md(m)}" for m in e["missing_inputs"]]
        lines += ["", f"**Subject:** {_md(e['subject'])}", "", _fence(e["body"]), ""]
    skipped = [e for e in plan["entries"] if not e["included"]]
    if skipped:
        lines += ["## Not drafted", ""]
        lines += [f"- `{e['component_id']}` (tier {e['tier']}) {_md(e['name'])}: {e['decision']} — "
                  f"{_md('; '.join(e['why']))}"
                  for e in skipped] + [""]
    lines += [f"Plan SHA-256: `{plan['plan_sha256']}`", ""]
    return "\n".join(lines)
