"""SFL-09 submission receipts, acknowledgments and uncertain outcomes.

Three distinct facts are recorded separately and never inferred from each
other: provider acceptance (SFL-08 receipt), provider delivery events
(callbacks), and agency acknowledgment (tracking number with evidence).

* ``reconcile_unknown`` resolves a ``submission_unknown`` attempt only from
  explicit operator-supplied evidence (provider status lookup or agency
  receipt). There is no automatic retry of an unknown outcome.
* ``record_provider_event`` is idempotent per provider event ID, so duplicate
  callbacks change nothing. Unmatched events are kept for review. A bounce
  after acceptance raises a review flag; it never rewrites history.
* ``record_acknowledgment`` moves ``submitted`` to ``acknowledged`` only with
  an agency tracking number and an evidence hash. It is a reviewable operator
  claim, not an invented API response.
"""
from __future__ import annotations

import hashlib
import re

from .authorization import AuthorizationError, require_role
from .ledger import canonical_json

EVENT_TYPES = ("delivered", "bounced", "deferred", "opened", "complaint")
HEX64 = re.compile(r"[0-9a-f]{64}")


class ReceiptError(RuntimeError):
    pass


def reconcile_unknown(ledger, attempt_id, operator, outcome, evidence: dict):
    require_role(ledger, operator, "dispatcher")
    if outcome not in ("submitted", "delivery_failed"):
        raise ReceiptError("outcome must be submitted or delivery_failed")
    if not isinstance(evidence, dict) or not evidence.get("source") or not evidence.get("reference"):
        raise ReceiptError("reconciliation requires evidence with a source and reference")
    now = ledger.clock()
    with ledger.transaction() as conn:
        row = conn.execute("SELECT * FROM outbox WHERE attempt_id=?", (attempt_id,)).fetchone()
        if row is None or row["state"] not in ("unknown", "submitting"):
            raise ReceiptError("attempt is not in an unknown/submitting state")
        state = ledger.get(row["request_id"])["state"]
        if state == "submitting":
            ledger.transition(row["request_id"], "submission_unknown", operator,
                              "stale submitting attempt treated as unknown before reconciliation",
                              {"attempt_id": attempt_id})
        payload = canonical_json(evidence)
        if outcome == "submitted":
            receipt_id = "rcpt-" + hashlib.sha256(f"{attempt_id}:reconcile:{payload}".encode()).hexdigest()[:24]
            conn.execute("INSERT INTO receipts VALUES (?,?,?,?,?,?,?,?)",
                         (receipt_id, attempt_id, row["request_id"], "reconciliation:" + evidence["source"],
                          evidence.get("provider_message_id"), payload,
                          hashlib.sha256(payload.encode()).hexdigest(), now))
            conn.execute("UPDATE outbox SET state='submitted', updated_at=? WHERE attempt_id=?", (now, attempt_id))
        else:
            conn.execute("UPDATE outbox SET state='failed', updated_at=? WHERE attempt_id=?", (now, attempt_id))
        ledger.transition(row["request_id"], outcome, operator, f"reconciled from {evidence['source']}",
                          dict(evidence, attempt_id=attempt_id))
    return outcome


def record_provider_event(ledger, *, event_id, provider_message_id, event_type, payload: dict):
    if event_type not in EVENT_TYPES:
        raise ReceiptError("unknown provider event type")
    digest = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
    now = ledger.clock()
    with ledger.transaction() as conn:
        existing = conn.execute("SELECT payload_sha256 FROM provider_events WHERE event_id=?", (event_id,)).fetchone()
        if existing is not None:
            if existing[0] != digest:
                raise ReceiptError("provider event ID reused with different content")
            return "duplicate"
        receipt = conn.execute("SELECT attempt_id, request_id FROM receipts WHERE provider_message_id=?",
                               (provider_message_id,)).fetchone()
        conn.execute("INSERT INTO provider_events VALUES (?,?,?,?,?,?,?,?)",
                     (event_id, provider_message_id, receipt["attempt_id"] if receipt else None,
                      receipt["request_id"] if receipt else None, event_type, digest, int(receipt is not None), now))
        if receipt is None:
            return "unmatched"
        if event_type in ("bounced", "complaint"):
            conn.execute("INSERT OR IGNORE INTO request_flags(request_id, flag, detail, created_at) VALUES (?,?,?,?)",
                         (receipt["request_id"], f"provider_{event_type}_after_acceptance", event_id, now))
        state = ledger.get(receipt["request_id"])["state"]
        ledger._event(conn, receipt["request_id"], state, state, "provider",
                      f"provider event {event_type}", {"event_id": event_id, "payload_sha256": digest}, now)
        return "recorded"


def record_acknowledgment(ledger, request_id, operator, *, tracking_number, evidence_sha256, source):
    try:
        require_role(ledger, operator, "reviewer")
    except AuthorizationError:
        require_role(ledger, operator, "dispatcher")
    if not isinstance(tracking_number, str) or not tracking_number.strip() or len(tracking_number) > 128:
        raise ReceiptError("agency tracking number required")
    if not isinstance(evidence_sha256, str) or not HEX64.fullmatch(evidence_sha256):
        raise ReceiptError("evidence_sha256 must reference a stored evidence artifact")
    now = ledger.clock()
    with ledger.transaction() as conn:
        existing = conn.execute("SELECT tracking_number FROM acknowledgments WHERE request_id=?",
                                (request_id,)).fetchall()
        if any(r[0] == tracking_number for r in existing):
            return "duplicate"
        state = ledger.get(request_id)["state"]
        if state not in ("submitted", "acknowledged", "response_received", "partially_fulfilled"):
            raise ReceiptError(f"cannot acknowledge a request in state {state}")
        conn.execute("INSERT INTO acknowledgments VALUES (?,?,?,?,?,?)",
                     (request_id, tracking_number, evidence_sha256, source, operator, now))
        if state == "submitted":
            ledger.transition(request_id, "acknowledged", operator, f"agency acknowledgment via {source}",
                              {"tracking_number": tracking_number, "evidence_sha256": evidence_sha256})
        elif existing:
            conn.execute("INSERT OR IGNORE INTO request_flags(request_id, flag, detail, created_at) VALUES (?,?,?,?)",
                         (request_id, "multiple_tracking_numbers", tracking_number, now))
        return "recorded"
