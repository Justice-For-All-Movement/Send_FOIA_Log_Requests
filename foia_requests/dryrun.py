"""SFL-06 strict small dry run and review packet.

A dry run selects at most ``Config.max_dry_run_drafts`` (1..3) explicit
component-periods, decides coverage/duplicates, renders drafts into the ledger
and writes a reproducible review packet. It is structurally unable to send:

* configuration must be dry-run with transport disabled;
* the injected sender must be a non-live sender, and its attempt counter must
  still be zero when the run ends (otherwise the run fails loudly);
* the cap is enforced by a database trigger, so parallel generation in the
  same run cannot exceed it.

Recording a review (``record_review``) re-verifies the packet hash and every
draft's content hash against the ledger before moving drafts to
``dry_run_reviewed``. Review is not authorization to send.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from . import directory as directory_module
from .ledger import RequestScope, canonical_json, sha256_text
from .periods import Period, coverage_decision
from .templates import render, store
from .transport import DisabledSender, Sender

PACKET_VERSION = 1
ROUTE_PREFERENCE = ("email", "portal")


class DryRunError(RuntimeError):
    pass


def _private_dir(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise DryRunError("packet output directory must be a private (0700) real directory")
    return path


def _write_private(path: Path, text: str):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(text)


def reserve(ledger, run_id: str, item_key: str):
    """Reserve one dry-run slot; the cap trigger makes this safe under concurrency."""
    with ledger.transaction() as conn:
        if conn.execute("SELECT 1 FROM dry_run_items WHERE run_id=? AND item_key=?", (run_id, item_key)).fetchone():
            return False
        ordinal = conn.execute("SELECT COUNT(*) FROM dry_run_items WHERE run_id=?", (run_id,)).fetchone()[0]
        try:
            conn.execute("INSERT INTO dry_run_items VALUES (?,?,?)", (run_id, ordinal, item_key))
        except sqlite3.IntegrityError as exc:
            raise DryRunError("dry-run cap exceeded") from exc
        return True


def choose_route(routes):
    usable = [r for r in routes if r["state"] == "verified" and r["kind"] in ROUTE_PREFERENCE]
    usable.sort(key=lambda r: (ROUTE_PREFERENCE.index(r["kind"]), r["route_id"]))
    if usable:
        return usable[0]
    return routes[0] if routes else None


def run_dry_run(ledger, config, *, selection, export, profile, template, output_dir, as_of,
                requester_jurisdiction="us-federal", sender: Sender | None = None):
    """Generate a bounded review packet. ``selection`` is [(component_id, Period), ...]."""
    if config.mode != "dry_run" or config.transport_enabled:
        raise DryRunError("live mode is denied; dry runs require mode=dry_run and transport disabled")
    sender = sender if sender is not None else DisabledSender()
    if getattr(sender, "live", True):
        raise DryRunError("dry run requires a non-live sender")
    attempts_before = getattr(sender, "attempts", 0)
    items = list(dict.fromkeys((c, p) for c, p in selection))
    if not items:
        raise DryRunError("select at least one component-period explicitly")
    if len(items) > config.max_dry_run_drafts:
        raise DryRunError(f"selection of {len(items)} exceeds dry-run cap {config.max_dry_run_drafts}")
    for _, period in items:
        if not isinstance(period, Period):
            raise DryRunError("periods must be explicit Period values")
    run_body = {"as_of": as_of, "items": [[c, p.start.isoformat(), p.end.isoformat(), p.basis] for c, p in items],
                "export": export.manifest_sha256, "profile": profile.profile_id, "template": template.sha256}
    run_id = sha256_text(canonical_json(run_body))[:32]
    with ledger.transaction() as conn:
        existing = conn.execute("SELECT status FROM dry_runs WHERE run_id=?", (run_id,)).fetchone()
        if existing is None:
            conn.execute("INSERT INTO dry_runs VALUES (?,?,?,?,?)",
                         (run_id, config.max_dry_run_drafts, as_of, None, "generating"))
    entries = []
    for component_id, period in items:
        reserve(ledger, run_id, canonical_json([component_id, period.start.isoformat(), period.end.isoformat()]))
        canonical = ledger.canonical_component(component_id)
        component = directory_module.component_record(ledger, canonical)
        routes = directory_module.routes_for(ledger, canonical)
        coverage = coverage_decision(canonical, period, export, ledger.canonical_component)
        scope = RequestScope(profile.requester_principal, requester_jurisdiction, canonical,
                             "foia_request_log", period.start.isoformat(), period.end.isoformat(), period.basis)
        entry = {"component_id": canonical, "requested_component_id": component_id,
                 "period": [period.start.isoformat(), period.end.isoformat(), period.basis],
                 "coverage": coverage, "routes_considered": [
                     {k: r[k] for k in ("route_id", "kind", "destination", "state", "reason")} for r in routes]}
        if component is None:
            entry.update(decision="blocked_unknown_component", missing_inputs=["component not in directory"])
            entries.append(entry)
            continue
        entry["component"] = {"name": component["name"], "agency_name": component["agency_name"],
                              "status": component["status"]}
        if component["status"] != "current":
            entry.update(decision="blocked_component_not_current",
                         missing_inputs=[f"component status is {component['status']}"])
            entries.append(entry)
            continue
        if coverage["decision"] == "suppress_full_public_coverage":
            entry.update(decision="suppressed_public_coverage", missing_inputs=[])
            entries.append(entry)
            continue
        from .ledger import dedupe_key
        key = dedupe_key(scope, canonical)
        prior = ledger.connection.execute("SELECT request_id, state FROM requests WHERE dedupe_key=?",
                                          (key,)).fetchone()
        if prior is not None and prior["state"] not in ("draft", "validated", "dry_run_reviewed", "blocked",
                                                        "rejected"):
            entry.update(decision="duplicate_existing_request", request_id=prior["request_id"],
                         existing_state=prior["state"], dedupe_key=key, missing_inputs=[])
            entries.append(entry)
            continue
        route = choose_route(routes)
        rendered = render(template, scope, {"component_id": canonical, "name": component["name"],
                                            "agency_name": component["agency_name"]}, route, profile)
        request_id, revision, _ = store(ledger, rendered, actor="dry-run")
        record = ledger.get(request_id)
        entry.update(
            decision="draft", request_id=request_id, dedupe_key=record["dedupe_key"], state=record["state"],
            revision=revision, review_flags=ledger.open_flags(request_id),
            recipient=rendered.manifest["route_destination"], route_kind=rendered.manifest["route_kind"],
            route_id=rendered.recipient_route, subject=rendered.subject, body=rendered.body,
            content_sha256=rendered.content_sha256, attachments=[], fee_policy=profile.fee_policy,
            missing_inputs=list(rendered.missing_inputs), live_ready=rendered.live_ready)
        entries.append(entry)
    attempts_after = getattr(sender, "attempts", 0)
    if attempts_after != attempts_before:
        raise DryRunError("a send was attempted during a dry run")
    body = {
        "packet_version": PACKET_VERSION, "mode": "dry_run", "run_id": run_id, "as_of": as_of,
        "cap": config.max_dry_run_drafts, "selected": len(items), "transport": sender.name,
        "outbound_messages_sent": 0, "corpus_gate": export.gate_report(),
        "template": {"key": template.key, "sha256": template.sha256, "status": template.status},
        "profile_id": profile.profile_id, "synthetic_profile": profile.synthetic, "entries": entries,
        "live_dispatch_authorized": False,
        "notice": "Dry-run review packet. Nothing was sent. Review does not authorize dispatch.",
    }
    packet_sha = sha256_text(canonical_json(body))
    packet = dict(body, packet_sha256=packet_sha)
    out = _private_dir(Path(output_dir) / run_id)
    _write_private(out / "packet.json", json.dumps(packet, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    for index, entry in enumerate(entries, 1):
        if entry.get("body"):
            _write_private(out / f"{index:02d}-{entry['request_id']}.txt",
                           f"To: {entry['recipient']}\nSubject: {entry['subject']}\n\n{entry['body']}")
    with ledger.transaction() as conn:
        conn.execute("UPDATE dry_runs SET packet_sha256=?, status='generated' WHERE run_id=?", (packet_sha, run_id))
    return packet, out


def record_review(ledger, packet_path, actor, note):
    """Mark the packet's drafts dry_run_reviewed after re-verifying every hash."""
    packet = json.loads(Path(packet_path).read_text(encoding="utf-8"))
    claimed = packet.pop("packet_sha256", None)
    if claimed != sha256_text(canonical_json(packet)):
        raise DryRunError("packet hash mismatch; packet was modified")
    row = ledger.connection.execute("SELECT packet_sha256 FROM dry_runs WHERE run_id=?", (packet["run_id"],)).fetchone()
    if row is None or row[0] != claimed:
        raise DryRunError("packet is not the one generated by this ledger")
    reviewed = []
    for entry in packet["entries"]:
        if entry.get("decision") != "draft":
            continue
        latest = ledger.latest_revision(entry["request_id"])
        if latest is None or latest["content_sha256"] != entry["content_sha256"]:
            raise DryRunError(f"request {entry['request_id']} changed after the packet was generated")
    with ledger.transaction():
        for entry in packet["entries"]:
            if entry.get("decision") != "draft":
                continue
            rid = entry["request_id"]
            evidence = {"packet_sha256": claimed, "content_sha256": entry["content_sha256"],
                        "missing_inputs": entry["missing_inputs"]}
            state = ledger.get(rid)["state"]
            if state == "draft":
                ledger.transition(rid, "validated", actor, f"dry-run packet checked: {note}", evidence)
                state = "validated"
            if state == "validated":
                ledger.transition(rid, "dry_run_reviewed", actor, f"dry-run reviewed: {note}", evidence)
                reviewed.append(rid)
    return reviewed
