"""One-command review packet for a date range across many agencies (dry run only).

``prepare`` renders a draft for every selected directory component for one
period and writes a single readable Markdown file plus a hashed JSON manifest.
It is a *preview*: it never stores requests in the ledger, never constructs a
sender and has no code path to any transport, so it is not bound by the 1..3
dry-run draft cap (which governs ledger drafts made by ``draft``). Nothing in
the packet authorizes dispatch.

For each component the packet states whether it is included and why: directory
status, public-log gap analysis, existing/overlapping ledger requests and the
submission route that would be used (or why none is usable).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .dryrun import _private_dir, _write_private, choose_route
from .ledger import RequestScope, canonical_json, dedupe_key, sha256_text
from .periods import Period, coverage_decision
from .templates import render

PACKET_NAME = "review-packet.md"
MANIFEST_NAME = "prepare-manifest.json"
MAX_COMPONENTS = 5000
# Ledger states that mean the request already went (or may have gone) out.
PAST_REVIEW = ("authorized", "queued", "submitting", "submitted", "submission_unknown", "acknowledged",
               "response_received", "fulfilled", "partially_fulfilled", "closed_unfulfilled", "delivery_failed")


class PrepareError(RuntimeError):
    pass


def _agency_slug(component_id):
    return component_id.split(":", 1)[-1].split("/", 1)[0]


def select_components(ledger, agencies=None, components=None):
    """Directory components matching the filters (all components when no filter is given)."""
    rows = ledger.connection.execute("SELECT component_id FROM components ORDER BY component_id").fetchall()
    every = [r[0] for r in rows]
    agencies = {a.strip().lower() for a in agencies or []}
    wanted = set(components or [])
    unknown = sorted(wanted - set(every))
    if unknown:
        raise PrepareError(f"components not in the directory: {', '.join(unknown)}")
    if agencies:
        missing = sorted(agencies - {_agency_slug(c) for c in every})
        if missing:
            raise PrepareError(f"agencies not in the directory: {', '.join(missing)}")
    chosen = [c for c in every if (not agencies and not wanted) or c in wanted or _agency_slug(c) in agencies]
    if not chosen:
        raise PrepareError("no directory components selected; run `directory` first or relax the filter")
    if len(chosen) > MAX_COMPONENTS:
        raise PrepareError(f"{len(chosen)} components exceed the packet bound {MAX_COMPONENTS}; filter by agency")
    return chosen


def _route_summary(route):
    if route is None:
        return None
    return {k: route[k] for k in ("route_id", "kind", "destination", "state", "reason")}


def _existing(ledger, requester, canonical, period):
    rows = ledger.connection.execute(
        "SELECT request_id, state, period_start, period_end, dedupe_key FROM requests WHERE requester=? "
        "AND component_id=? AND record_class='foia_request_log' AND period_start<=? AND period_end>=? "
        "ORDER BY period_start, request_id",
        (requester, canonical, period.end.isoformat(), period.start.isoformat())).fetchall()
    return [dict(r) for r in rows]


def _entry(ledger, component_id, period, export, profile, template, jurisdiction):
    from . import directory
    canonical = ledger.canonical_component(component_id)
    component = directory.component_record(ledger, component_id)
    entry = {"component_id": component_id, "canonical_component_id": canonical,
             "agency_name": component["agency_name"], "name": component["name"], "status": component["status"]}
    if canonical != component_id:
        entry.update(included=False, decision="excluded_alias",
                     why=[f"{component_id} is an alias of {canonical}; the canonical component is listed instead"])
        return entry
    if component["status"] != "current":
        why = [f"directory status is {component['status']} (only current components are drafted)"]
        if component["status"] == "unknown":
            why.append("verify the component and its official contact routes first")
        entry.update(included=False, decision="excluded_not_current", why=why)
        return entry
    coverage = coverage_decision(canonical, period, export, ledger.canonical_component)
    entry["coverage"] = {k: coverage[k] for k in ("decision", "reasons", "uncovered", "uncovered_days")}
    if coverage["decision"] == "suppress_full_public_coverage":
        entry.update(included=False, decision="excluded_public_coverage", why=coverage["reasons"])
        return entry
    scope = RequestScope(profile.requester_principal, jurisdiction, canonical, "foia_request_log",
                         period.start.isoformat(), period.end.isoformat(), period.basis)
    key = dedupe_key(scope, canonical)
    existing = _existing(ledger, profile.requester_principal, canonical, period)
    prior = next((r for r in existing if r["dedupe_key"] == key and r["state"] in PAST_REVIEW), None)
    if prior is not None:
        entry.update(included=False, decision="excluded_duplicate",
                     why=[f"request {prior['request_id']} for this exact period is already {prior['state']}"])
        return entry
    why = ["current directory component"]
    why += [f"public logs: {r}" for r in coverage["reasons"]]
    if coverage["uncovered_days"] and coverage["uncovered_days"] != period.days:
        why.append(f"{coverage['uncovered_days']} of {period.days} days not covered by full public logs")
    for row in existing:
        kind = "same-period" if row["dedupe_key"] == key else "overlapping"
        why.append(f"review {kind} ledger request {row['request_id']} ({row['state']}) covers "
                   f"{row['period_start']}..{row['period_end']}")
    routes = directory.routes_for(ledger, canonical)
    route = choose_route(routes)
    if route is None:
        why.append("no submission route on file: find an official route before any send")
    elif route["state"] != "verified" or route["kind"] not in ("email", "portal"):
        why.append(f"no verified electronic route; best available is {route['kind']} ({route['state']}): "
                   "manual handling required")
    else:
        why.append(f"route: verified {route['kind']}")
    rendered = render(template, scope, {"component_id": canonical, "name": component["name"],
                                        "agency_name": component["agency_name"]}, route, profile)
    entry.update(included=True, decision="draft", why=why, dedupe_key=key, route=_route_summary(route),
                 routes_considered=[_route_summary(r) for r in routes], subject=rendered.subject,
                 body=rendered.body, content_sha256=rendered.content_sha256,
                 missing_inputs=list(rendered.missing_inputs), live_ready=rendered.live_ready)
    return entry


# Enough to stop links, emphasis, inline HTML and table breaks while keeping the raw file readable.
_MD_SPECIAL = re.compile(r"([\\`*\[\]<>|])")


def _md(text):
    return _MD_SPECIAL.sub(r"\\\1", str(text))


def _fence(text):
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{text.rstrip()}\n{fence}"


def render_markdown(manifest):
    period = manifest["period"]
    included = [e for e in manifest["entries"] if e["included"]]
    excluded = [e for e in manifest["entries"] if not e["included"]]
    lines = [
        f"# FOIA log request review packet: {period['start']} to {period['end']}",
        "",
        "**DRY RUN. Nothing was sent to any agency, e-mail address or portal. This packet does not "
        "authorize dispatch.**",
        "",
        f"- Period: {period['start']} through {period['end']} ({period['days']} days, {period['basis']})",
        f"- Prepared at: {_md(manifest['as_of'])} (reference date {period['today']})",
        f"- Template: `{manifest['template']['key']}` — status **`{manifest['template']['status']}`**",
        f"- Requester profile: {_md(manifest['profile_id'])}"
        + (" (SYNTHETIC fixture, not a real requester)" if manifest["synthetic_profile"] else ""),
        f"- Agency filter: {_md(', '.join(manifest['filters']['agencies']) or 'all directory agencies')}"
        + (f"; components: {_md(', '.join(manifest['filters']['components']))}"
           if manifest["filters"]["components"] else ""),
        f"- Components considered: {len(manifest['entries'])}; drafts: {len(included)}; excluded: {len(excluded)}",
        f"- Public-log corpus gate (EXT-CORPUS) satisfied: {manifest['corpus_gate']['ext_corpus_satisfied']}",
        f"- Outbound messages sent: {manifest['outbound_messages_sent']}",
        "",
    ]
    if period["warnings"]:
        lines += ["## Period warnings", ""] + [f"- {_md(w)}" for w in period["warnings"]] + [""]
    if manifest["blocking_inputs"]:
        lines += ["## Inputs still missing before any live send", ""]
        lines += [f"- {_md(m)}" for m in manifest["blocking_inputs"]] + [""]
    lines += ["## Summary", "", "| # | Agency | Component | Decision | Route |", "|---|---|---|---|---|"]
    for n, e in enumerate(manifest["entries"], 1):
        route = e.get("route") or {}
        route_text = f"{route.get('kind')} ({route.get('state')})" if route else "—"
        lines.append(f"| {n} | {_md(e['agency_name'])} | {_md(e['name'])} `{e['component_id']}` | "
                     f"{e['decision']} | {_md(route_text)} |")
    lines += ["", "## Drafts", ""]
    if not included:
        lines += ["No drafts for this selection.", ""]
    for n, e in enumerate(manifest["entries"], 1):
        if not e["included"]:
            continue
        route = e["route"]
        lines += [f"### {n}. {_md(e['agency_name'])} — {_md(e['name'])}", "",
                  f"- Component: `{e['component_id']}`",
                  f"- Route: " + (f"{route['kind']} → {_md(route['destination'])} ({route['state']})"
                                  if route else "none on file"),
                  f"- Content SHA-256: `{e['content_sha256']}`", "- Why included:"]
        lines += [f"  - {_md(w)}" for w in e["why"]]
        if e["missing_inputs"]:
            lines += ["- Missing before live use:"] + [f"  - {_md(m)}" for m in e["missing_inputs"]]
        lines += ["", f"**Subject:** {_md(e['subject'])}", "", _fence(e["body"]), ""]
    if excluded:
        lines += ["## Excluded", ""]
        for e in excluded:
            lines.append(f"- `{e['component_id']}` {_md(e['agency_name'])} — {_md(e['name'])}: "
                         f"{e['decision']} — {_md('; '.join(e['why']))}")
        lines.append("")
    lines += [f"Manifest SHA-256: `{manifest['manifest_sha256']}`", ""]
    return "\n".join(lines)


def prepare_packet(ledger, config, *, period: Period, warnings, export, profile, template, output_dir, as_of,
                   today, agencies=None, components=None, requester_jurisdiction="us-federal"):
    """Write ``review-packet.md`` and ``prepare-manifest.json``; return (manifest, folder)."""
    if config.mode != "dry_run" or config.transport_enabled:
        raise PrepareError("prepare requires mode=dry_run with transport disabled")
    if not isinstance(period, Period):
        raise PrepareError("an explicit Period is required")
    chosen = select_components(ledger, agencies, components)
    entries = [_entry(ledger, c, period, export, profile, template, requester_jurisdiction) for c in chosen]
    blocking = sorted({m for e in entries if e["included"] for m in e["missing_inputs"]})
    body = {
        "packet_kind": "prepare-review/1", "mode": "dry_run", "as_of": as_of,
        "period": {"start": period.start.isoformat(), "end": period.end.isoformat(), "basis": period.basis,
                   "days": period.days, "today": today.isoformat(), "warnings": list(warnings)},
        "filters": {"agencies": sorted({a.strip().lower() for a in agencies or []}),
                    "components": sorted(set(components or []))},
        "template": {"key": template.key, "sha256": template.sha256, "status": template.status},
        "profile_id": profile.profile_id, "synthetic_profile": profile.synthetic,
        "corpus_gate": export.gate_report(), "entries": entries, "blocking_inputs": blocking,
        "outbound_messages_sent": 0, "transport": "none", "ledger_requests_written": 0,
        "live_dispatch_authorized": False,
        "notice": "Preview only. Nothing was sent. Review of this packet does not authorize dispatch.",
    }
    manifest = dict(body, manifest_sha256=sha256_text(canonical_json(body)))
    markdown = render_markdown(manifest)
    manifest["markdown_sha256"] = sha256_text(markdown)
    out = _private_dir(Path(output_dir))
    _write_private(out / PACKET_NAME, markdown)
    _write_private(out / MANIFEST_NAME, json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    return manifest, out
