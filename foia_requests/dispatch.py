"""SFL-08 controlled submission worker with rate limits and outcome classes.

``run_once`` processes at most one outbox intent through an injected
``Sender``. Live senders are refused unless the configuration enables
transport, and ``Config`` currently forbids that, so only the in-memory fake
can be exercised (tests). The disabled default sender is never even claimed
against, so no intent can get stuck in ``submitting``.

Outcome handling (never a blind resend):
* accepted        -> outbox submitted, request submitted, immutable receipt
* rate_limited    -> request delivery_failed -> queued with not_before (Retry-After)
* rejected/bounced-> outbox failed, request delivery_failed
* auth_failed     -> outbox failed, request delivery_failed, dispatch stop set
* exception/timeout after hand-off -> outbox unknown, request submission_unknown;
  reconciliation (SFL-09) is required, never automatic retry.
Per-destination-host and global hourly caps are enforced from the durable
attempt log, before any transport call.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from .authorization import STOP_KEY, AuthorizationError, begin_submission, claim_next
from .ledger import canonical_json
from .transport import DisabledSender, Sender, TransportDisabled

MAX_TRIES = 3


@dataclass(frozen=True)
class RateLimits:
    per_destination_per_hour: int = 2
    global_per_hour: int = 10


def _ts(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _fmt(value):
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def destination_host(kind, destination):
    if kind == "email":
        return destination.rsplit("@", 1)[-1].lower()
    return (urlsplit(destination).hostname or destination).lower()


def _log(conn, message, host, now, outcome, result=None):
    conn.execute("INSERT INTO dispatch_log(attempt_id, request_id, destination_host, at, outcome, provider_message_id,"
                 " retry_after, error_class) VALUES (?,?,?,?,?,?,?,?)",
                 (message.attempt_id, message.request_id, host, now, outcome,
                  getattr(result, "provider_message_id", None), getattr(result, "retry_after", None),
                  getattr(result, "error_class", None)))


def _rate_ok(ledger, host, now, limits):
    since = _fmt(_ts(now) - timedelta(hours=1))
    conn = ledger.connection
    per_host = conn.execute("SELECT COUNT(*) FROM dispatch_log WHERE destination_host=? AND at>?",
                            (host, since)).fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM dispatch_log WHERE at>?", (since,)).fetchone()[0]
    return per_host < limits.per_destination_per_hour and total < limits.global_per_hour


def run_once(ledger, sender: Sender, config, *, owner, now, limits=RateLimits()):
    if isinstance(sender, DisabledSender):
        return {"status": "transport_disabled", "sent": 0}
    if getattr(sender, "live", True) and not config.transport_enabled:
        raise TransportDisabled("live sender refused: transport is disabled by configuration")
    attempt = claim_next(ledger, owner, now)
    if attempt is None:
        return {"status": "idle", "sent": 0}
    row = ledger.connection.execute("SELECT o.*, c.kind FROM outbox o JOIN contact_routes c ON c.route_id=o.route_id "
                                    "WHERE o.attempt_id=?", (attempt,)).fetchone()
    host = destination_host(row["kind"], row["destination"])
    if not _rate_ok(ledger, host, now, limits):
        with ledger.transaction() as conn:
            conn.execute("UPDATE outbox SET state='queued', lease_owner=NULL, lease_expires=NULL, not_before=?, "
                         "updated_at=? WHERE attempt_id=?", (_fmt(_ts(now) + timedelta(hours=1)), now, attempt))
        return {"status": "rate_limited_local", "attempt_id": attempt, "sent": 0}
    try:
        message = begin_submission(ledger, attempt, owner, now)
    except AuthorizationError as exc:
        return {"status": "blocked", "attempt_id": attempt, "reason": str(exc), "sent": 0}
    try:
        result = sender.send(message)
    except Exception as exc:  # noqa: BLE001 - any failure after hand-off is an unknown outcome
        with ledger.transaction() as conn:
            conn.execute("UPDATE outbox SET state='unknown', updated_at=? WHERE attempt_id=?", (now, attempt))
            _log(conn, message, host, now, "unknown")
            ledger.transition(message.request_id, "submission_unknown", owner,
                              f"transport outcome unknown: {type(exc).__name__}", {"attempt_id": attempt})
        return {"status": "submission_unknown", "attempt_id": attempt, "sent": None}
    try:
        return _record(ledger, message, result, host, owner, now)
    except Exception:
        # Never claim proof we failed to store: fall back to an unknown outcome.
        with ledger.transaction() as conn:
            conn.execute("UPDATE outbox SET state='unknown', updated_at=? WHERE attempt_id=?", (now, attempt))
            ledger.transition(message.request_id, "submission_unknown", owner,
                              "provider responded but the receipt could not be stored", {"attempt_id": attempt})
        return {"status": "submission_unknown", "attempt_id": attempt, "sent": None}


def _record(ledger, message, result, host, owner, now):
    attempt, rid = message.attempt_id, message.request_id
    with ledger.transaction() as conn:
        _log(conn, message, host, now, result.outcome, result)
        if result.outcome == "accepted":
            receipt = canonical_json(result.receipt or {})
            if (result.receipt or {}).get("content_sha256") not in (None, message.content_sha256):
                raise RuntimeError("provider receipt content hash mismatch")
            receipt_id = "rcpt-" + hashlib.sha256(f"{attempt}:{receipt}".encode()).hexdigest()[:24]
            conn.execute("INSERT INTO receipts VALUES (?,?,?,?,?,?,?,?)",
                         (receipt_id, attempt, rid, "provider", result.provider_message_id, receipt,
                          hashlib.sha256(receipt.encode()).hexdigest(), now))
            conn.execute("UPDATE outbox SET state='submitted', updated_at=? WHERE attempt_id=?", (now, attempt))
            ledger.transition(rid, "submitted", owner, "provider accepted the message (not agency acknowledgment)",
                              {"attempt_id": attempt, "receipt_id": receipt_id,
                               "provider_message_id": result.provider_message_id})
            return {"status": "submitted", "attempt_id": attempt, "receipt_id": receipt_id, "sent": 1}
        ledger.transition(rid, "delivery_failed", owner, f"provider outcome {result.outcome}",
                          {"attempt_id": attempt, "error_class": result.error_class})
        tries = conn.execute("SELECT tries FROM outbox WHERE attempt_id=?", (attempt,)).fetchone()[0]
        if result.outcome == "rate_limited" and tries < MAX_TRIES:
            wait = max(int(result.retry_after or 60), 1)
            conn.execute("UPDATE outbox SET state='queued', lease_owner=NULL, lease_expires=NULL, not_before=?, "
                         "updated_at=? WHERE attempt_id=?", (_fmt(_ts(now) + timedelta(seconds=wait)), now, attempt))
            ledger.transition(rid, "queued", owner, f"provider rate limit; retry after {wait}s",
                              {"attempt_id": attempt})
            return {"status": "retry_scheduled", "attempt_id": attempt, "retry_after": wait, "sent": 0}
        conn.execute("UPDATE outbox SET state='failed', updated_at=? WHERE attempt_id=?", (now, attempt))
        if result.outcome == "auth_failed":
            conn.execute("INSERT INTO dispatch_control VALUES (?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                         "value=excluded.value, set_by=excluded.set_by, reason=excluded.reason, set_at=excluded.set_at",
                         (STOP_KEY, "stopped", "system", "provider authentication failure", now))
        return {"status": "failed", "attempt_id": attempt, "outcome": result.outcome, "sent": 0}
