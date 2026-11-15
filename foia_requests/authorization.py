"""SFL-07 exact-batch authorization and transactional outbox.

An authorization is an immutable envelope binding: authorizer + external
reference, expiry, sender profile, transport, caps, and for every request its
exact content hash, route ID and destination. It is created only for
``dry_run_reviewed`` requests whose current rendering is live-ready and
byte-identical to the stored revision. Any later edit to content,
destination, route state, sender or scope makes it unusable: queueing and
submission re-verify everything and revoke on mismatch.

The outbox holds durable send intent. A unique partial index allows one live
intent per request. Claims are leased; an attempt that reached ``submitting``
is never reclaimed automatically (its outcome may be unknown). A dispatch stop
flag blocks claims and submissions immediately.

Nothing here transmits. A merge, CI run or documentation change never creates
an authorization; only an explicit call by an operator with the
``authorizer`` role does.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from . import directory
from .ledger import RequestScope, canonical_json, sha256_text
from .templates import render
from .transport import OutboundMessage

ROLES = ("reviewer", "authorizer", "dispatcher", "safety")
STOP_KEY = "dispatch_stop"
ATTEMPT_NAMESPACE = uuid.UUID("0b6f4f8e-2f35-5d2c-8f3e-7c1d9a6b4e21")


class AuthorizationError(RuntimeError):
    pass


def _ts(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _fmt(value):
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def grant_role(ledger, operator_id, role, granted_by):
    if role not in ROLES:
        raise AuthorizationError("unknown role")
    with ledger.transaction() as conn:
        conn.execute("INSERT OR IGNORE INTO operators VALUES (?,?,?,?)", (operator_id, role, granted_by, ledger.clock()))


def require_role(ledger, operator_id, role):
    if ledger.connection.execute("SELECT 1 FROM operators WHERE operator_id=? AND role=?",
                                 (operator_id, role)).fetchone() is None:
        raise AuthorizationError(f"{operator_id} lacks the {role} role")


def set_stop(ledger, operator_id, reason):
    require_role(ledger, operator_id, "safety")
    with ledger.transaction() as conn:
        conn.execute("INSERT INTO dispatch_control VALUES (?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
                     " set_by=excluded.set_by, reason=excluded.reason, set_at=excluded.set_at",
                     (STOP_KEY, "stopped", operator_id, reason, ledger.clock()))


def clear_stop(ledger, operator_id, reason):
    require_role(ledger, operator_id, "safety")
    with ledger.transaction() as conn:
        conn.execute("UPDATE dispatch_control SET value='running', set_by=?, reason=?, set_at=? WHERE key=?",
                     (operator_id, reason, ledger.clock(), STOP_KEY))


def stopped(ledger):
    row = ledger.connection.execute("SELECT value FROM dispatch_control WHERE key=?", (STOP_KEY,)).fetchone()
    return row is not None and row[0] == "stopped"


def _current_route(ledger, route_id):
    row = ledger.connection.execute("SELECT * FROM contact_routes WHERE route_id=?", (route_id,)).fetchone()
    return None if row is None else dict(row)


def authorize_batch(ledger, *, request_ids, authorizer, reference, expires_at, profile, template,
                    transport, max_messages, now=None):
    """Create an immutable authorization envelope for an exact reviewed batch."""
    require_role(ledger, authorizer, "authorizer")
    now = now or ledger.clock()
    if not reference or not isinstance(reference, str):
        raise AuthorizationError("an external authorization reference is required")
    if _ts(expires_at) <= _ts(now):
        raise AuthorizationError("authorization must expire in the future")
    ids = list(dict.fromkeys(request_ids))
    if not ids or len(ids) > max_messages:
        raise AuthorizationError("batch must contain 1..max_messages requests")
    if profile.synthetic or not profile.verified:
        raise AuthorizationError("a verified, non-synthetic requester profile is required")
    items = []
    for rid in ids:
        request = ledger.get(rid)
        if request["state"] != "dry_run_reviewed":
            raise AuthorizationError(f"{rid} is {request['state']}; only dry_run_reviewed requests can be authorized")
        if request["requester"] != profile.requester_principal:
            raise AuthorizationError(f"{rid} belongs to a different requester")
        revision = ledger.latest_revision(rid)
        route = _current_route(ledger, revision["recipient_route"]) if revision["recipient_route"] else None
        if route is None or route["state"] != "verified" or route["superseded_at"] is not None:
            raise AuthorizationError(f"{rid} has no currently verified route")
        component = directory.component_record(ledger, request["component_id"])
        scope = RequestScope(request["requester"], request["jurisdiction"], request["component_id"],
                             request["record_class"], request["period_start"], request["period_end"],
                             request["period_basis"])
        rendered = render(template, scope, {"component_id": component["component_id"], "name": component["name"],
                                            "agency_name": component["agency_name"]}, route, profile)
        if rendered.content_sha256 != revision["content_sha256"]:
            raise AuthorizationError(f"{rid} reviewed content differs from the current rendering")
        if not rendered.live_ready:
            raise AuthorizationError(f"{rid} is not live-ready: {', '.join(rendered.missing_inputs)}")
        items.append({"request_id": rid, "content_sha256": revision["content_sha256"],
                      "route_id": route["route_id"], "destination": route["destination"]})
    envelope = {
        "authorizer": authorizer, "reference": reference, "created_at": now, "expires_at": expires_at,
        "sender_profile": profile.profile_id, "requester": profile.requester_principal,
        "transport": transport, "max_messages": max_messages,
        "max_fee_usd": (profile.fee_policy or {}).get("max_authorized_usd", 0),
        "template": template.key, "template_sha256": template.sha256, "items": items,
    }
    digest = sha256_text(canonical_json(envelope))
    auth_id = "auth-" + digest[:24]
    with ledger.transaction() as conn:
        conn.execute("INSERT INTO authorizations VALUES (?,?,?,?,?,?,?,NULL,NULL,NULL)",
                     (auth_id, canonical_json(envelope), digest, authorizer, expires_at, max_messages, now))
        for item in items:
            conn.execute("INSERT INTO authorization_items VALUES (?,?,?,?,?)",
                         (auth_id, item["request_id"], item["content_sha256"], item["route_id"], item["destination"]))
            ledger.transition(item["request_id"], "authorized", authorizer, f"authorization {auth_id}",
                              {"auth_id": auth_id, "envelope_sha256": digest})
    return auth_id, digest


def _authorization(ledger, auth_id):
    row = ledger.connection.execute("SELECT * FROM authorizations WHERE auth_id=?", (auth_id,)).fetchone()
    if row is None:
        raise AuthorizationError("unknown authorization")
    return dict(row)


def revoke(ledger, auth_id, operator_id, reason, *, _system=False):
    if not _system:
        require_role(ledger, operator_id, "authorizer")
    with ledger.transaction() as conn:
        auth = _authorization(ledger, auth_id)
        if auth["revoked_at"] is None:
            conn.execute("UPDATE authorizations SET revoked_at=?, revoked_by=?, revoked_reason=? WHERE auth_id=?",
                         (ledger.clock(), operator_id, reason, auth_id))
        for row in conn.execute("SELECT attempt_id, request_id FROM outbox WHERE auth_id=? AND state IN "
                                "('queued', 'leased')", (auth_id,)).fetchall():
            conn.execute("UPDATE outbox SET state='cancelled', updated_at=? WHERE attempt_id=?",
                         (ledger.clock(), row["attempt_id"]))
        for (rid,) in conn.execute("SELECT request_id FROM authorization_items WHERE auth_id=?", (auth_id,)).fetchall():
            state = ledger.get(rid)["state"]
            if state in ("authorized", "queued"):
                ledger.transition(rid, "draft", operator_id, f"authorization {auth_id} revoked: {reason}")


def _problems(ledger, auth, now):
    """Return a list of reasons this authorization cannot be used right now."""
    problems = []
    if auth["revoked_at"]:
        problems.append("revoked")
    if _ts(auth["expires_at"]) <= _ts(now):
        problems.append("expired")
    for item in ledger.connection.execute("SELECT * FROM authorization_items WHERE auth_id=?", (auth["auth_id"],)):
        revision = ledger.latest_revision(item["request_id"])
        if revision["content_sha256"] != item["content_sha256"] or revision["recipient_route"] != item["route_id"]:
            problems.append(f"{item['request_id']}: content or route changed")
        route = _current_route(ledger, item["route_id"])
        if (route is None or route["state"] != "verified" or route["superseded_at"] is not None
                or route["destination"] != item["destination"]):
            problems.append(f"{item['request_id']}: route no longer verified")
    return problems


def _ensure_usable(ledger, auth_id, now):
    """Revoke (committed) and raise when an authorization has become unusable."""
    auth = _authorization(ledger, auth_id)
    problems = _problems(ledger, auth, now)
    if problems:
        if "revoked" not in problems:
            revoke(ledger, auth_id, "system", "; ".join(problems), _system=True)
        raise AuthorizationError("authorization unusable: " + "; ".join(problems))
    return auth


def queue_authorized(ledger, auth_id, operator_id, now=None):
    """Transactionally create one outbox intent per authorized request."""
    require_role(ledger, operator_id, "dispatcher")
    now = now or ledger.clock()
    _ensure_usable(ledger, auth_id, now)
    with ledger.transaction() as conn:
        auth = _authorization(ledger, auth_id)
        if _problems(ledger, auth, now):
            raise AuthorizationError("authorization changed while queueing; retry")
        if stopped(ledger):
            raise AuthorizationError("dispatch stop is active")
        queued = []
        items = conn.execute("SELECT * FROM authorization_items WHERE auth_id=? ORDER BY request_id",
                             (auth_id,)).fetchall()
        for item in items:
            attempt_id = str(uuid.uuid5(ATTEMPT_NAMESPACE, f"{auth_id}:{item['request_id']}"))
            if conn.execute("SELECT 1 FROM outbox WHERE attempt_id=?", (attempt_id,)).fetchone():
                continue
            conn.execute("INSERT INTO outbox(attempt_id, auth_id, request_id, content_sha256, route_id, destination, "
                         "state, created_at, updated_at) VALUES (?,?,?,?,?,?,'queued',?,?)",
                         (attempt_id, auth_id, item["request_id"], item["content_sha256"], item["route_id"],
                          item["destination"], now, now))
            ledger.transition(item["request_id"], "queued", operator_id, f"queued under {auth_id}",
                              {"attempt_id": attempt_id}, expected_state="authorized")
            queued.append(attempt_id)
        total = conn.execute("SELECT COUNT(*) FROM outbox WHERE auth_id=?", (auth_id,)).fetchone()[0]
        if total > auth["max_messages"]:
            raise AuthorizationError("batch exceeds authorization cap")
        return queued


def claim_next(ledger, owner, now, lease_seconds=120):
    """Lease the next due queued intent (or an expired pre-submission lease)."""
    with ledger.transaction() as conn:
        if stopped(ledger):
            return None
        rows = conn.execute(
            "SELECT * FROM outbox WHERE (state='queued' OR (state='leased' AND lease_expires<=?)) "
            "AND (not_before IS NULL OR not_before<=?) ORDER BY created_at, attempt_id", (now, now)).fetchall()
        for row in rows:
            auth = _authorization(ledger, row["auth_id"])
            problems = _problems(ledger, auth, now)
            if problems:
                if "revoked" not in problems:
                    revoke(ledger, row["auth_id"], "system", "; ".join(problems), _system=True)
                continue
            expires = _fmt(_ts(now) + timedelta(seconds=lease_seconds))
            conn.execute("UPDATE outbox SET state='leased', lease_owner=?, lease_expires=?, updated_at=? "
                         "WHERE attempt_id=?", (owner, expires, now, row["attempt_id"]))
            return row["attempt_id"]
        return None


def begin_submission(ledger, attempt_id, owner, now):
    """Final pre-send gate. Commits ``submitting`` before any transport call."""
    row = ledger.connection.execute("SELECT auth_id FROM outbox WHERE attempt_id=?", (attempt_id,)).fetchone()
    if row is None:
        raise AuthorizationError("unknown attempt")
    _ensure_usable(ledger, row["auth_id"], now)
    with ledger.transaction() as conn:
        row = conn.execute("SELECT * FROM outbox WHERE attempt_id=?", (attempt_id,)).fetchone()
        if row["state"] != "leased" or row["lease_owner"] != owner or row["lease_expires"] <= now:
            raise AuthorizationError("lease not held")
        if stopped(ledger):
            raise AuthorizationError("dispatch stop is active")
        if _problems(ledger, _authorization(ledger, row["auth_id"]), now):
            raise AuthorizationError("authorization changed during submission gate")
        revision = ledger.latest_revision(row["request_id"])
        route = _current_route(ledger, row["route_id"])
        conn.execute("UPDATE outbox SET state='submitting', tries=tries+1, updated_at=? WHERE attempt_id=?",
                     (now, attempt_id))
        ledger.transition(row["request_id"], "submitting", owner, f"attempt {attempt_id}",
                          {"attempt_id": attempt_id, "content_sha256": revision["content_sha256"]},
                          expected_state="queued")
        return OutboundMessage(row["request_id"], attempt_id, row["route_id"], route["kind"], row["destination"],
                               revision["subject"], revision["body"], revision["content_sha256"])


def outbox_rows(ledger, auth_id=None):
    query, params = "SELECT * FROM outbox", ()
    if auth_id:
        query, params = query + " WHERE auth_id=?", (auth_id,)
    return [dict(r) for r in ledger.connection.execute(query + " ORDER BY created_at, attempt_id", params)]


def envelope(ledger, auth_id):
    return json.loads(_authorization(ledger, auth_id)["envelope_json"])
