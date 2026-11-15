"""SFL-10 response intake and fulfillment accounting (manual/upload-first).

Operators record agency responses with their attachments; nothing is fetched
from a mailbox or portal. Responses are immutable, keyed by a content-derived
ID (duplicate intake is idempotent) and correlated to exactly one request by
agency tracking number or explicit request ID. A conflict between the two is
an error, and uncorrelated responses are kept for review rather than attached
to a guessed request.

Attachments are stored by SHA-256 in a private artifact directory after type
sniffing. Oversize files, executables/scripts, unknown types and ZIP containers
with excessive expansion are quarantined (stored, never parsed).

Accounting never invents legal deadlines, appeals, payments or follow-up
messages. Partial coverage, denials, referrals, fee notices and clarification
requests create operator tasks; only coverage of every requested day and field
marks a request fulfilled.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
from datetime import date, timedelta
from pathlib import Path

from .authorization import require_role
from .ledger import canonical_json
from .templates import load_template

KINDS = ("interim", "final", "partial", "no_records", "denial", "referral", "fee_notice", "clarification")
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_UNZIPPED_BYTES = 200 * 1024 * 1024
MAX_ZIP_RATIO = 100
RESPONSE_STATES = ("submitted", "acknowledged", "response_received", "partially_fulfilled")


class ResponseError(RuntimeError):
    pass


def sniff(data: bytes):
    """Return (media_type, state, reason)."""
    if len(data) > MAX_ATTACHMENT_BYTES:
        return "application/octet-stream", "quarantined", "exceeds size limit"
    head = data[:8]
    if head.startswith(b"MZ") or head.startswith(b"\x7fELF") or head.startswith(b"#!"):
        return "application/octet-stream", "quarantined", "executable or script"
    if head.startswith(b"%PDF-"):
        return "application/pdf", "accepted", "pdf signature"
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                infos = archive.infolist()
                total = sum(i.file_size for i in infos)
                compressed = sum(i.compress_size for i in infos) or 1
                names = [i.filename for i in infos]
        except zipfile.BadZipFile:
            return "application/zip", "quarantined", "corrupt zip container"
        if total > MAX_UNZIPPED_BYTES or total / compressed > MAX_ZIP_RATIO:
            return "application/zip", "quarantined", "excessive expansion (possible zip bomb)"
        if any(n.startswith("/") or ".." in n.split("/") for n in names):
            return "application/zip", "quarantined", "unsafe member path"
        if "[Content_Types].xml" in names and any(n.startswith("xl/") for n in names):
            return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "accepted", "xlsx container"
        if "[Content_Types].xml" in names and any(n.startswith("word/") for n in names):
            return "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "accepted", "docx container"
        return "application/zip", "quarantined", "unrecognized zip container"
    if b"\x00" not in data:
        try:
            data.decode("utf-8")
            return "text/plain", "accepted", "utf-8 text"
        except UnicodeDecodeError:
            pass
    return "application/octet-stream", "quarantined", "unrecognized type"


def _store(artifact_dir: Path, digest: str, data: bytes):
    root = Path(os.path.abspath(artifact_dir))
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or root.stat().st_mode & 0o077:
        raise ResponseError("artifact directory must be private (0700)")
    path = root / digest
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
    return path


def _task(conn, request_id, response_id, kind, detail, now):
    task_id = "task-" + hashlib.sha256(canonical_json([request_id, response_id, kind, detail]).encode()).hexdigest()[:20]
    conn.execute("INSERT OR IGNORE INTO followup_tasks(task_id, request_id, response_id, kind, detail, created_at) "
                 "VALUES (?,?,?,?,?,?)", (task_id, request_id, response_id, kind, detail, now))


def _correlate(conn, request_id, tracking_number):
    by_tracking = None
    if tracking_number:
        rows = conn.execute("SELECT request_id FROM acknowledgments WHERE tracking_number=?",
                            (tracking_number,)).fetchall()
        if len(rows) > 1:
            raise ResponseError("tracking number maps to several requests; correlate explicitly")
        by_tracking = rows[0][0] if rows else None
    if request_id and by_tracking and request_id != by_tracking:
        raise ResponseError("tracking number belongs to a different request")
    resolved = request_id or by_tracking
    if resolved is None:
        return None, "unmatched"
    return resolved, ("tracking_number" if by_tracking else "explicit_request_id")


def intake_response(ledger, *, operator, kind, received_at, artifact_dir, request_id=None, tracking_number=None,
                    attachments=(), coverage=(), referral_to=(), evidence: bytes = b"", note=""):
    """Record one agency response. ``attachments``: [(filename, bytes)];
    ``coverage``: [(period_start, period_end, [fields...])] actually delivered."""
    require_role(ledger, operator, "reviewer")
    if kind not in KINDS:
        raise ResponseError("unknown response kind")
    if kind == "referral" and not referral_to:
        raise ResponseError("a referral must name the receiving component")
    now = ledger.clock()
    with ledger.transaction() as conn:
        resolved, correlation = _correlate(conn, request_id, tracking_number)
        stored = []
        for filename, data in attachments:
            digest = hashlib.sha256(data).hexdigest()
            media, verdict, reason = sniff(data)
            _store(artifact_dir, digest, data)
            stored.append((digest, os.path.basename(filename)[:200] or "attachment", len(data), media, verdict, reason))
        cover = sorted((date.fromisoformat(s).isoformat(), date.fromisoformat(e).isoformat(), sorted(set(f)))
                       for s, e, f in coverage)
        evidence_sha = hashlib.sha256(evidence).hexdigest()
        response_id = "resp-" + hashlib.sha256(canonical_json(
            [resolved, tracking_number, kind, received_at, sorted(s[0] for s in stored), cover, evidence_sha]
        ).encode()).hexdigest()[:24]
        if conn.execute("SELECT 1 FROM responses WHERE response_id=?", (response_id,)).fetchone():
            return {"response_id": response_id, "status": "duplicate", "request_id": resolved}
        if resolved is not None:
            state = ledger.get(resolved)["state"]
            if state not in RESPONSE_STATES:
                raise ResponseError(f"cannot record a response for a request in state {state}")
        conn.execute("INSERT INTO responses VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (response_id, resolved, tracking_number, kind, received_at, correlation, evidence_sha, operator,
                      now, canonical_json({"note": note})))
        for digest, name, size, media, verdict, reason in stored:
            conn.execute("INSERT INTO response_attachments VALUES (?,?,?,?,?,?,?)",
                         (response_id, digest, name, size, media, verdict, reason))
            if verdict == "quarantined":
                _task(conn, resolved, response_id, "quarantined_attachment", f"{name}: {reason}", now)
        for start, end, fields in cover:
            conn.execute("INSERT INTO response_coverage VALUES (?,?,?,?)", (response_id, start, end,
                                                                             canonical_json(fields)))
        for target in referral_to:
            if resolved is not None:
                conn.execute("INSERT INTO referrals VALUES (?,?,?)", (response_id, resolved, target))
        if resolved is None:
            _task(conn, None, response_id, "correlate_response", "no matching request; review manually", now)
            return {"response_id": response_id, "status": "unmatched", "request_id": None}
        outcome = _account(ledger, conn, resolved, response_id, kind, operator, now, referral_to)
        return {"response_id": response_id, "status": "recorded", "request_id": resolved, **outcome}


def fulfillment_report(ledger, request_id, requested_fields=None):
    request = ledger.get(request_id)
    requested_fields = sorted(requested_fields or load_template().requested_fields)
    start, end = date.fromisoformat(request["period_start"]), date.fromisoformat(request["period_end"])
    rows = ledger.connection.execute(
        "SELECT c.period_start, c.period_end, c.fields_json FROM response_coverage c JOIN responses r "
        "USING(response_id) WHERE r.request_id=? ORDER BY c.period_start", (request_id,)).fetchall()
    complete_spans, partial_fields = [], set()
    for row in rows:
        fields = set(json.loads(row["fields_json"]))
        span = (max(start, date.fromisoformat(row["period_start"])), min(end, date.fromisoformat(row["period_end"])))
        if span[0] > span[1]:
            continue
        partial_fields |= fields
        if set(requested_fields) <= fields:
            complete_spans.append(span)
    uncovered, cursor = [], start
    for a, b in sorted(complete_spans):
        if a > cursor:
            uncovered.append([cursor.isoformat(), (a - timedelta(days=1)).isoformat()])
        cursor = max(cursor, b + timedelta(days=1))
    if cursor <= end:
        uncovered.append([cursor.isoformat(), end.isoformat()])
    return {"request_id": request_id, "period": [request["period_start"], request["period_end"]],
            "uncovered_periods": uncovered, "fields_never_delivered": sorted(set(requested_fields) - partial_fields),
            "complete": not uncovered}


def _account(ledger, conn, request_id, response_id, kind, operator, now, referral_to):
    state = ledger.get(request_id)["state"]
    if state in ("submitted", "acknowledged"):
        ledger.transition(request_id, "response_received", operator, f"agency response {kind}",
                          {"response_id": response_id})
        state = "response_received"
    report = fulfillment_report(ledger, request_id)
    if kind == "no_records":
        ledger.transition(request_id, "closed_unfulfilled", operator, "agency reported no records",
                          {"response_id": response_id})
        return {"request_state": "closed_unfulfilled"}
    if kind == "denial":
        _task(conn, request_id, response_id, "review_denial",
              "operator review required; no appeal is filed automatically", now)
        ledger.transition(request_id, "closed_unfulfilled", operator, "agency denial recorded",
                          {"response_id": response_id})
        return {"request_state": "closed_unfulfilled"}
    if kind == "referral":
        _task(conn, request_id, response_id, "track_referral", "referred to " + ", ".join(referral_to), now)
    if kind == "fee_notice":
        _task(conn, request_id, response_id, "fee_decision_required",
              "processing paused pending an authorized fee decision; nothing is paid automatically", now)
        conn.execute("INSERT OR IGNORE INTO request_flags(request_id, flag, detail, created_at) VALUES (?,?,?,?)",
                     (request_id, "fee_pause", response_id, now))
    if kind == "clarification":
        _task(conn, request_id, response_id, "clarification_requested", "agency asked for clarification", now)
    if report["complete"] and kind in ("final", "partial", "interim"):
        if state != "fulfilled":
            ledger.transition(request_id, "fulfilled", operator, "every requested day and field delivered",
                              {"response_id": response_id})
        return {"request_state": "fulfilled", "fulfillment": report}
    if kind in ("final", "partial") or (kind == "interim" and report["uncovered_periods"] != [report["period"]]):
        if state != "partially_fulfilled":
            ledger.transition(request_id, "partially_fulfilled", operator, "response covers part of the request",
                              {"response_id": response_id})
        _task(conn, request_id, response_id, "missing_coverage", canonical_json(
            {"uncovered": report["uncovered_periods"], "fields": report["fields_never_delivered"]}), now)
        return {"request_state": "partially_fulfilled", "fulfillment": report}
    return {"request_state": ledger.get(request_id)["state"], "fulfillment": report}


def open_tasks(ledger, request_id=None):
    query, params = "SELECT * FROM followup_tasks WHERE status='open'", ()
    if request_id:
        query, params = query + " AND request_id=?", (request_id,)
    return [dict(r) for r in ledger.connection.execute(query + " ORDER BY created_at, task_id", params)]
