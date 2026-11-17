"""Follow-up requests for released records the FOIA Aggregator lacks (``foia-requests plan-followups``).

Input is an Aggregator export in format ``foia-release-gaps/1``: a folder with ``manifest.json`` and
``gaps.jsonl`` (one closed request per line whose log says records were released, but for which the Aggregator
holds no matching record). Validation is strict: unknown formats, unknown or missing fields, bad types and
non-releasing dispositions are rejected with the line number, and nothing is drafted from an invalid export.

Each gap becomes a ``released_records_followup`` draft. Drafts are deduplicated by (agency, tracking number)
against the ledger's ``release_followups`` table and within the export. With ``group=True`` several tracking
numbers for the same agency component share one letter (at most 25 per letter). Drafts are written to a private
folder (``followups.json`` + ``followups.md``) and, unless ``record=False``, recorded in the ledger so later runs
skip them. Nothing here sends; follow-up letters are not requests in the sending path and cannot be authorized,
queued or sent without a further reviewed change.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from string import Template
from urllib.parse import urlsplit

from . import directory
from .dryrun import _private_dir, _write_private, choose_route
from .ledger import canonical_json, sha256_text
from .prepare import _fence, _md, _route_summary
from .templates import (MAX_BODY, MAX_SUBJECT, RequesterProfile, RequestTemplate, TemplateError, _clean_line, _clean_text,
                        _fee_paragraph, human_date, public_interest_values)

FORMAT = "foia-release-gaps/1"
TEMPLATE_ID = "released_records_followup"
MANIFEST_FIELDS = {"format", "generated_at", "producer", "period", "count"}
GAP_FIELDS = {"request_key", "agency_slug", "agency_name", "component", "tracking_number", "received_date",
              "closed_date", "disposition", "subject", "pages_released", "log_id", "log_source_url", "gap_reason"}
RELEASING = ("full_grant", "partial_grant")
GAP_REASONS = ("no_matching_record",)
MAX_GAPS = 200000
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_TEXT = 2000
MAX_GROUP = 25
MAX_ITEM_SUBJECT = 300
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
OUT_JSON = "followups.json"
OUT_MD = "followups.md"


class GapsError(ValueError):
    pass


@dataclass(frozen=True)
class GapsExport:
    manifest: dict
    manifest_sha256: str
    gaps: tuple


def _text(value, name, where, nullable=False, limit=MAX_TEXT):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise GapsError(f"{where}: {name} must be nonempty text of at most {limit} characters"
                        + (" or null" if nullable else ""))
    if any(unicodedata.category(c) in ("Cc", "Cf") for c in value):
        raise GapsError(f"{where}: {name} contains control or formatting characters")
    return value


def _day(value, name, where, nullable=True):
    if value is None and nullable:
        return None
    try:
        if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except ValueError as exc:
        raise GapsError(f"{where}: {name} must be YYYY-MM-DD" + (" or null" if nullable else "")) from exc
    return value


def normalize_tracking(value):
    """Dedupe form of a tracking number: NFKC, trimmed, inner whitespace collapsed, upper case."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip().upper()


def _gap(raw, where):
    if not isinstance(raw, dict):
        raise GapsError(f"{where}: each line must be a JSON object")
    missing, unknown = GAP_FIELDS - set(raw), set(raw) - GAP_FIELDS
    if missing or unknown:
        raise GapsError(f"{where}: " + "; ".join(filter(None, (
            missing and "missing fields " + ", ".join(sorted(missing)),
            unknown and "unknown fields " + ", ".join(sorted(unknown))))))
    if raw["disposition"] not in RELEASING:
        raise GapsError(f"{where}: disposition {raw['disposition']!r} releases no records "
                        f"(expected {' or '.join(RELEASING)})")
    if raw["gap_reason"] not in GAP_REASONS:
        raise GapsError(f"{where}: gap_reason must be {' or '.join(GAP_REASONS)}")
    slug = raw["agency_slug"]
    if not isinstance(slug, str) or not SLUG.fullmatch(slug) or len(slug) > 64:
        raise GapsError(f"{where}: agency_slug must be a lowercase slug")
    pages = raw["pages_released"]
    if pages is not None and (type(pages) is not int or pages < 0):
        raise GapsError(f"{where}: pages_released must be a non-negative integer or null")
    url = _text(raw["log_source_url"], "log_source_url", where, limit=2048)
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.hostname or any(ord(c) < 33 for c in url):
        raise GapsError(f"{where}: log_source_url must be an http(s) URL")
    gap = {
        "request_key": _text(raw["request_key"], "request_key", where, limit=512),
        "agency_slug": slug,
        "agency_name": _text(raw["agency_name"], "agency_name", where, limit=200),
        "component": _text(raw["component"], "component", where, nullable=True, limit=200),
        "tracking_number": _text(raw["tracking_number"], "tracking_number", where, limit=128),
        "received_date": _day(raw["received_date"], "received_date", where),
        "closed_date": _day(raw["closed_date"], "closed_date", where),
        "disposition": raw["disposition"],
        "subject": _text(raw["subject"], "subject", where, nullable=True),
        "pages_released": pages,
        "log_id": _text(raw["log_id"], "log_id", where, limit=512),
        "log_source_url": url,
        "gap_reason": raw["gap_reason"],
    }
    if gap["received_date"] and gap["closed_date"] and gap["received_date"] > gap["closed_date"]:
        raise GapsError(f"{where}: received_date is after closed_date")
    return gap


def load_gaps(folder) -> GapsExport:
    """Strictly read a ``foia-release-gaps/1`` folder."""
    folder = Path(folder)
    raw_manifest = (folder / "manifest.json").read_bytes()
    manifest = json.loads(raw_manifest.decode("utf-8"))
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise GapsError(f"unsupported release-gaps format (expected {FORMAT})")
    if set(manifest) != MANIFEST_FIELDS:
        raise GapsError("manifest fields must be exactly " + ", ".join(sorted(MANIFEST_FIELDS)))
    try:
        generated = manifest["generated_at"]
        if not isinstance(generated, str):
            raise ValueError
        datetime.fromisoformat(generated.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GapsError("manifest generated_at must be an ISO timestamp") from exc
    producer = manifest["producer"]
    if not isinstance(producer, dict) or set(producer) != {"commit"} or \
            not (producer["commit"] is None or isinstance(producer["commit"], str)):
        raise GapsError("manifest producer must be {\"commit\": text or null}")
    period = manifest["period"]
    if not isinstance(period, dict) or set(period) != {"start", "end"}:
        raise GapsError("manifest period must be {\"start\", \"end\"}")
    start, end = _day(period["start"], "period.start", "manifest", False), _day(period["end"], "period.end",
                                                                                "manifest", False)
    if start > end:
        raise GapsError("manifest period start is after its end")
    count = manifest["count"]
    if type(count) is not int or not 0 <= count <= MAX_GAPS:
        raise GapsError(f"manifest count must be an integer 0..{MAX_GAPS}")
    path = folder / "gaps.jsonl"
    if path.stat().st_size > MAX_FILE_BYTES:
        raise GapsError("gaps.jsonl is too large")
    gaps, keys = [], set()
    with path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                raise GapsError(f"gaps.jsonl line {number}: blank line")
            try:
                raw = json.loads(line)
            except ValueError as exc:
                raise GapsError(f"gaps.jsonl line {number}: invalid JSON") from exc
            gap = _gap(raw, f"gaps.jsonl line {number}")
            if gap["request_key"] in keys:
                raise GapsError(f"gaps.jsonl line {number}: duplicate request_key {gap['request_key']}")
            keys.add(gap["request_key"])
            gaps.append(gap)
            if len(gaps) > count:
                break
    if len(gaps) != count:
        raise GapsError(f"manifest count {count} does not match {len(gaps)} gap line(s)")
    return GapsExport(manifest, sha256_text(canonical_json(manifest)), tuple(gaps))


# -- rendering -----------------------------------------------------------------------------------------------
def _item_values(gap):
    subject = gap["subject"]
    if subject is None:
        subject = "not given in the log"
    elif len(subject) > MAX_ITEM_SUBJECT:
        subject = subject[:MAX_ITEM_SUBJECT].rstrip() + " [shortened]"
    return {"tracking_number": _clean_line(gap["tracking_number"], "tracking number"),
            "closed_date": human_date(gap["closed_date"]) if gap["closed_date"] else "date not given in the log",
            "subject": " ".join(subject.split())}


def render_followup(template: RequestTemplate, gaps, *, agency_name, addressee, route, profile: RequesterProfile):
    """Render one follow-up letter for 1..25 gaps of one agency component. Pure and deterministic."""
    if template.template_id != TEMPLATE_ID or not template.item:
        raise TemplateError(f"follow-ups need the {TEMPLATE_ID} template")
    if not 1 <= len(gaps) <= MAX_GROUP:
        raise TemplateError(f"a follow-up letter covers 1..{MAX_GROUP} requests")
    missing = []

    def need(value, name):
        if value:
            return value
        missing.append(name)
        return f"[MISSING: {name}]"

    items = [Template(template.item).substitute(_item_values(g)) for g in gaps]
    numbers = [_item_values(g)["tracking_number"] for g in gaps]
    label = (f"FOIA request {numbers[0]}" if len(numbers) == 1
             else f"{len(numbers)} closed FOIA requests ({numbers[0]} and others)")
    reply = [line for line in (profile.organization, profile.reply_email, profile.reply_postal) if line]
    if not (profile.reply_email or profile.reply_postal):
        missing.append("requester reply route")
    if profile.synthetic:
        missing.append("real requester profile (fixture profile is synthetic)")
    elif not profile.verified:
        missing.append("verified requester identity")
    route_id = None
    if route is None:
        missing.append("official submission route")
    else:
        route_id = route["route_id"]
        if route.get("state") != "verified":
            missing.append(f"verified submission route ({route_id} is {route.get('state')})")
    identifiers = set(Template(template.subject).get_identifiers()) | set(Template(template.body).get_identifiers())
    values = {
        "addressee": _clean_text(addressee, "addressee", 500),
        "agency_name": _clean_line(agency_name, "agency name"),
        "request_label": label,
        "request_list": "\n".join(f"- {item}" for item in items),
        "fee_paragraph": need(_fee_paragraph(profile.fee_policy, include_waiver=False), "fee policy"),
        "requester_name": need(profile.name, "requester name"),
        "requester_block": "\n".join(reply) if reply else "[MISSING: requester reply route]",
    }
    values.update(public_interest_values(template, profile, need, identifiers))
    subject = Template(template.subject).substitute(values)
    body = Template(template.body).substitute(values)
    if len(subject) > MAX_SUBJECT or len(body) > MAX_BODY:
        raise TemplateError("rendered follow-up exceeds length bounds")
    if template.status != "approved":
        missing.append(f"approved template (status: {template.status})")
    missing = tuple(dict.fromkeys(missing))
    content = sha256_text(canonical_json([subject, body, route_id]))
    return {"subject": subject, "body": body, "content_sha256": content, "recipient_route": route_id,
            "missing_inputs": list(missing), "live_ready": not missing}


# -- planning ------------------------------------------------------------------------------------------------
def _resolve_component(ledger, gap):
    """Directory component for a gap, when its agency/component slugs match one; else None."""
    if gap["component"] is None:
        return None
    try:
        cid = directory.component_id("fed", gap["agency_slug"], gap["component"])
    except directory.DirectoryError:
        return None
    cid = ledger.canonical_component(cid)
    record = directory.component_record(ledger, cid)
    return record if record is not None and record["status"] == "current" else None


def _already(ledger, agency, tracking):
    row = ledger.connection.execute("SELECT letter_id, created_at FROM release_followups WHERE agency_slug=? AND "
                                    "tracking_number=?", (agency, tracking)).fetchone()
    return None if row is None else dict(row)


def plan_followups(ledger, config, *, export: GapsExport, profile, template, output_dir, as_of, agencies=None,
                   max_letters=None, group=False, record=True):
    """Draft follow-up letters for an export's gaps; write ``followups.json``/``followups.md``."""
    if config.mode != "dry_run" or config.transport_enabled:
        raise GapsError("plan-followups requires mode=dry_run with transport disabled")
    if max_letters is not None and (type(max_letters) is not int or max_letters < 1):
        raise GapsError("--max must be a positive integer")
    wanted = {a.strip().lower() for a in agencies or []}
    skipped, seen, pending = [], set(), []
    order = sorted(export.gaps, key=lambda g: (g["agency_slug"], g["component"] or "", g["closed_date"] or "",
                                               normalize_tracking(g["tracking_number"]), g["request_key"]))
    for gap in order:
        key = (gap["agency_slug"], normalize_tracking(gap["tracking_number"]))
        base = {"request_key": gap["request_key"], "agency_slug": key[0], "tracking_number": key[1]}
        if wanted and gap["agency_slug"] not in wanted:
            continue
        if key in seen:
            skipped.append(dict(base, decision="duplicate_in_export"))
            continue
        seen.add(key)
        prior = _already(ledger, *key)
        if prior is not None:
            skipped.append(dict(base, decision="already_drafted", letter_id=prior["letter_id"]))
            continue
        pending.append((key, gap))
    groups = {}
    for key, gap in pending:
        bucket = (gap["agency_slug"], gap["component"]) if group else (gap["agency_slug"], gap["component"], key[1])
        groups.setdefault(bucket, []).append((key, gap))
    letters = []
    for bucket in groups:  # insertion order follows the sorted gaps
        members = groups[bucket]
        for start in range(0, len(members), MAX_GROUP):
            chunk = members[start:start + MAX_GROUP]
            if max_letters is not None and len(letters) >= max_letters:
                skipped += [{"request_key": g["request_key"], "agency_slug": k[0], "tracking_number": k[1],
                             "decision": "deferred_max"} for k, g in chunk]
                continue
            first = chunk[0][1]
            component = _resolve_component(ledger, first)
            route = None
            routes = []
            if component is not None:
                routes = directory.routes_for(ledger, component["component_id"])
                route = choose_route(routes)
            office = component["name"] if component else first["component"]
            addressee = f"{office} ({first['agency_name']})" if office else first["agency_name"]
            rendered = render_followup(template, [g for _, g in chunk], agency_name=first["agency_name"],
                                       addressee=addressee, route=route, profile=profile)
            letter_id = "fu-" + sha256_text(canonical_json([first["agency_slug"], sorted(k[1] for k, _ in chunk)]))[:24]
            letters.append({
                "letter_id": letter_id, "agency_slug": first["agency_slug"], "agency_name": first["agency_name"],
                "component": first["component"], "directory_component": component["component_id"] if component
                else None, "route": _route_summary(route),
                "items": [{"request_key": g["request_key"], "tracking_number": k[1], "closed_date": g["closed_date"],
                           "disposition": g["disposition"], "pages_released": g["pages_released"],
                           "log_id": g["log_id"], "log_source_url": g["log_source_url"]} for k, g in chunk],
                **rendered})
    if record:
        _record(ledger, letters, template, export)
    blocking = sorted({m for letter in letters for m in letter["missing_inputs"]})
    body = {
        "plan_kind": "release-followups/1", "mode": "dry_run", "as_of": as_of,
        "gaps_export": {"format": FORMAT, "manifest_sha256": export.manifest_sha256,
                        "generated_at": export.manifest["generated_at"], "producer": export.manifest["producer"],
                        "period": export.manifest["period"], "count": export.manifest["count"]},
        "filters": {"agencies": sorted(wanted), "max": max_letters, "group": group},
        "template": {"key": template.key, "sha256": template.sha256, "status": template.status},
        "profile_id": profile.profile_id, "synthetic_profile": profile.synthetic,
        "letters": letters, "skipped": skipped, "blocking_inputs": blocking,
        "records_requested": sum(len(letter["items"]) for letter in letters),
        "ledger_letters_recorded": len(letters) if record else 0, "outbound_messages_sent": 0,
        "transport": "none", "live_dispatch_authorized": False,
        "notice": "Drafts only. Nothing was sent. Review of these drafts does not authorize dispatch.",
    }
    plan = dict(body, plan_sha256=sha256_text(canonical_json(body)))
    out = _private_dir(Path(output_dir))
    _write_private(out / OUT_MD, render_markdown(plan))
    _write_private(out / OUT_JSON, json.dumps(plan, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    return plan, out


def _record(ledger, letters, template, export):
    now = ledger.clock()
    with ledger.transaction() as conn:
        for letter in letters:
            conn.execute("INSERT OR IGNORE INTO release_followup_letters VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (letter["letter_id"], letter["agency_slug"], letter["component"], template.key,
                          template.sha256, letter["recipient_route"], letter["subject"], letter["body"],
                          letter["content_sha256"], export.manifest_sha256, now))
            for item in letter["items"]:
                conn.execute("INSERT INTO release_followups VALUES (?,?,?,?,?,?)",
                             (letter["agency_slug"], item["tracking_number"], item["request_key"],
                              letter["letter_id"], "drafted", now))


def render_markdown(plan):
    gaps = plan["gaps_export"]
    lines = [
        f"# Released-records follow-up drafts: {gaps['period']['start']} to {gaps['period']['end']}", "",
        "**DRY RUN. Nothing was sent to any agency, e-mail address or portal. These drafts do not authorize "
        "dispatch.**", "",
        f"- Gaps export: {FORMAT}, {gaps['count']} gap(s), generated {_md(gaps['generated_at'])}, manifest "
        f"`{gaps['manifest_sha256'][:12]}`",
        f"- Template: `{plan['template']['key']}` — status **`{plan['template']['status']}`**",
        f"- Requester profile: {_md(plan['profile_id'])}"
        + (" (SYNTHETIC fixture, not a real requester)" if plan["synthetic_profile"] else ""),
        f"- Letters: {len(plan['letters'])} covering {plan['records_requested']} closed request(s); grouped: "
        f"{plan['filters']['group']}; skipped: {len(plan['skipped'])}",
        f"- Outbound messages sent: {plan['outbound_messages_sent']}", "",
    ]
    if plan["blocking_inputs"]:
        lines += ["## Inputs still missing before any live send", ""]
        lines += [f"- {_md(m)}" for m in plan["blocking_inputs"]] + [""]
    lines += ["## Letters", ""]
    if not plan["letters"]:
        lines += ["No new follow-ups.", ""]
    for n, letter in enumerate(plan["letters"], 1):
        route = letter["route"]
        lines += [f"### {n}. {_md(letter['agency_name'])} — {_md(letter['component'] or 'agency-wide')}", "",
                  f"- Letter: `{letter['letter_id']}`; requests: "
                  + ", ".join(_md(i["tracking_number"]) for i in letter["items"]),
                  "- Route: " + (f"{route['kind']} → {_md(route['destination'])} ({route['state']})"
                                 if route else "none matched in the directory"),
                  f"- Content SHA-256: `{letter['content_sha256']}`"]
        if letter["missing_inputs"]:
            lines += ["- Missing before live use:"] + [f"  - {_md(m)}" for m in letter["missing_inputs"]]
        lines += ["", f"**Subject:** {_md(letter['subject'])}", "", _fence(letter["body"]), ""]
    if plan["skipped"]:
        lines += ["## Skipped", ""]
        lines += [f"- {_md(s['agency_slug'])} {_md(s['tracking_number'])}: {s['decision']}" for s in plan["skipped"]]
        lines.append("")
    lines += [f"Plan SHA-256: `{plan['plan_sha256']}`", ""]
    return "\n".join(lines)
