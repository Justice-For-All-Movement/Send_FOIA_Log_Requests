"""SFL-05 durable request ledger and semantic deduplication.

The ledger is a single-writer SQLite database created by
:func:`foia_requests.migrations.initialize`. Every mutation runs inside a
``BEGIN IMMEDIATE`` transaction so concurrent workers serialize on the database
lock and the ``UNIQUE(dedupe_key)`` constraint guarantees one canonical request
per semantic scope.

Semantic dedupe key = requester principal + jurisdiction + canonical component
+ record class + exact period. Template wording, provider, recipient route and
period *basis label* deliberately do not participate: changing them creates a
new revision, never a fresh send entitlement.

Nothing in this module contacts an agency or a provider.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from . import migrations

DEDUPE_KEY_VERSION = "dk1"
RECORD_CLASSES = frozenset({"foia_request_log"})
PERIOD_BASES = frozenset({"calendar", "fiscal", "custom"})

STATES = frozenset({
    "draft", "validated", "dry_run_reviewed", "authorized", "queued", "submitting",
    "submitted", "acknowledged", "response_received", "fulfilled",
    "partially_fulfilled", "closed_unfulfilled", "blocked", "rejected",
    "cancelled", "delivery_failed", "submission_unknown",
})
TERMINAL_STATES = frozenset({"fulfilled", "closed_unfulfilled", "cancelled"})
TRANSITIONS = {
    "draft": {"validated", "blocked", "cancelled"},
    "validated": {"dry_run_reviewed", "draft", "blocked", "cancelled"},
    "dry_run_reviewed": {"authorized", "draft", "rejected", "blocked", "cancelled"},
    "authorized": {"queued", "draft", "blocked", "cancelled"},
    "queued": {"submitting", "draft", "blocked", "cancelled"},
    # A send attempt ends in exactly one of three outcomes. Unknown outcomes
    # are never retried automatically; they need reconciliation evidence.
    "submitting": {"submitted", "delivery_failed", "submission_unknown"},
    "submission_unknown": {"submitted", "delivery_failed"},
    "delivery_failed": {"queued", "blocked", "cancelled"},
    "submitted": {"acknowledged", "response_received"},
    "acknowledged": {"response_received"},
    "response_received": {"response_received", "fulfilled", "partially_fulfilled", "closed_unfulfilled"},
    "partially_fulfilled": {"response_received", "fulfilled", "closed_unfulfilled"},
    "blocked": {"draft", "cancelled"},
    "rejected": {"draft", "cancelled"},
    "fulfilled": set(),
    "closed_unfulfilled": set(),
    "cancelled": set(),
}
# States in which an edit to content invalidates prior review/authorization.
REVIEWED_STATES = frozenset({"validated", "dry_run_reviewed", "authorized", "queued"})
# States after which the content is frozen (an attempt may already exist).
FROZEN_STATES = frozenset(STATES - {"draft", "blocked", "rejected"} - REVIEWED_STATES)


class LedgerError(RuntimeError):
    pass


class TransitionError(LedgerError):
    pass


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _iso_date(value, field):
    if isinstance(value, date):
        value = value.isoformat()
    if not isinstance(value, str):
        raise LedgerError(f"{field} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise LedgerError(f"{field} must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise LedgerError(f"{field} must be canonical YYYY-MM-DD")
    return value


def _text(value, field, limit=512):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise LedgerError(f"{field} must be nonempty text of at most {limit} characters")
    if any(ord(c) < 32 for c in value):
        raise LedgerError(f"{field} contains control characters")
    return value


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RequestScope:
    """Semantic scope of one component-period log request."""

    requester: str
    jurisdiction: str
    component_id: str
    record_class: str
    period_start: str
    period_end: str
    period_basis: str = "calendar"

    def __post_init__(self):
        _text(self.requester, "requester")
        _text(self.jurisdiction, "jurisdiction", 64)
        _text(self.component_id, "component_id")
        if self.record_class not in RECORD_CLASSES:
            raise LedgerError("unsupported record class")
        start = _iso_date(self.period_start, "period_start")
        end = _iso_date(self.period_end, "period_end")
        if start > end:
            raise LedgerError("period_start must not follow period_end")
        if self.period_basis not in PERIOD_BASES:
            raise LedgerError("period_basis must be calendar, fiscal or custom")


def dedupe_key(scope: RequestScope, canonical_component: str | None = None) -> str:
    material = [DEDUPE_KEY_VERSION, scope.requester, scope.jurisdiction,
                canonical_component or scope.component_id, scope.record_class,
                scope.period_start, scope.period_end]
    return sha256_text(canonical_json(material))


@dataclass(frozen=True)
class DraftResult:
    request_id: str
    dedupe_key: str
    created: bool
    overlaps: tuple


class Ledger:
    """Transactional access to one private ledger database."""

    def __init__(self, path, clock=utc_now):
        if str(path) == ":memory:":
            raise LedgerError("an in-memory ledger is not durable; choose a private file path")
        self.path = migrations.initialize(path)
        self.clock = clock
        self._conn = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=30000")

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @property
    def connection(self):
        return self._conn

    @contextmanager
    def transaction(self):
        conn = self._conn
        if conn.in_transaction:
            # Nested use joins the outer transaction.
            yield conn
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    # -- components --------------------------------------------------------
    def canonical_component(self, component_id, conn=None):
        conn = conn or self._conn
        seen = set()
        current = component_id
        while True:
            if current in seen:
                raise LedgerError("component alias cycle")
            seen.add(current)
            row = conn.execute("SELECT canonical_id FROM component_aliases WHERE alias=?",
                               (current,)).fetchone()
            if row is None:
                return current
            current = row[0]

    def add_component_alias(self, alias, canonical_id, reason, actor="system"):
        """Record that ``alias`` (e.g. a renamed/merged component) is ``canonical_id``.

        Existing requests filed under the alias are re-keyed to the canonical
        component. If that would collide with an existing canonical request both
        are kept, linked and flagged for review instead of silently merged.
        """
        _text(alias, "alias")
        _text(canonical_id, "canonical_id")
        _text(reason, "reason")
        now = self.clock()
        with self.transaction() as conn:
            target = self.canonical_component(canonical_id, conn)
            if target == alias:
                raise LedgerError("component alias cycle")
            existing = conn.execute("SELECT canonical_id FROM component_aliases WHERE alias=?",
                                    (alias,)).fetchone()
            if existing is not None:
                if self.canonical_component(existing[0], conn) == target:
                    return False
                raise LedgerError("alias already maps to a different component")
            conn.execute("INSERT INTO component_aliases VALUES (?,?,?,?)", (alias, canonical_id, reason, now))
            rows = conn.execute("SELECT * FROM requests WHERE component_id=?", (alias,)).fetchall()
            for row in rows:
                scope = _scope_from_row(row)
                key = dedupe_key(scope, target)
                clash = conn.execute("SELECT request_id FROM requests WHERE dedupe_key=?", (key,)).fetchone()
                if clash is None:
                    conn.execute("UPDATE requests SET component_id=?, dedupe_key=? WHERE request_id=?",
                                 (target, key, row["request_id"]))
                    self._event(conn, row["request_id"], row["state"], row["state"], actor,
                                f"component alias {alias} -> {target}: {reason}", None, now)
                else:
                    for request_id, other in ((row["request_id"], clash[0]), (clash[0], row["request_id"])):
                        conn.execute("INSERT OR IGNORE INTO request_flags(request_id, flag, detail, created_at) "
                                     "VALUES (?,?,?,?)", (request_id, "alias_duplicate", other, now))
                    conn.execute("INSERT OR IGNORE INTO request_links VALUES (?,?,?,?,?)",
                                 (row["request_id"], clash[0], "possible_duplicate", reason, now))
            return True

    # -- requests ----------------------------------------------------------
    def create_draft(self, scope: RequestScope, actor="system", origin="generated",
                     request_id=None, state="draft", reason="draft created", evidence=None):
        """Idempotently create the canonical request for ``scope``.

        Returns the existing request when the semantic key is already present;
        never creates a second canonical request.
        """
        if state not in STATES:
            raise LedgerError("unknown state")
        _text(actor, "actor")
        now = self.clock()
        with self.transaction() as conn:
            component = self.canonical_component(scope.component_id, conn)
            key = dedupe_key(scope, component)
            row = conn.execute("SELECT request_id FROM requests WHERE dedupe_key=?", (key,)).fetchone()
            if row is not None:
                return DraftResult(row[0], key, False, self._overlaps(conn, row[0]))
            request_id = request_id or str(uuid.uuid4())
            try:
                uuid.UUID(request_id)
            except ValueError as exc:
                raise LedgerError("request_id must be a UUID") from exc
            conn.execute(
                "INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (request_id, key, scope.requester, scope.jurisdiction, component, scope.record_class,
                 scope.period_start, scope.period_end, scope.period_basis, state, origin, now))
            self._event(conn, request_id, None, state, actor, reason, evidence, now)
            overlapping = conn.execute(
                "SELECT request_id FROM requests WHERE requester=? AND jurisdiction=? AND component_id=? "
                "AND record_class=? AND request_id<>? AND period_start<=? AND period_end>=? "
                "ORDER BY request_id",
                (scope.requester, scope.jurisdiction, component, scope.record_class, request_id,
                 scope.period_end, scope.period_start)).fetchall()
            for other in overlapping:
                conn.execute("INSERT OR IGNORE INTO request_flags(request_id, flag, detail, created_at) "
                             "VALUES (?,?,?,?)", (request_id, "period_overlap", other[0], now))
            return DraftResult(request_id, key, True, self._overlaps(conn, request_id))

    def import_history(self, entries, actor="import"):
        """Import previously filed requests without creating duplicates.

        Each entry is a mapping with RequestScope fields plus ``state`` and
        optional ``request_id``/``evidence``. Existing canonical requests are
        never overwritten (their receipts/events survive). Order-independent
        for the set of canonical requests produced.
        """
        results = []
        with self.transaction():
            for entry in entries:
                fields = {k: entry[k] for k in ("requester", "jurisdiction", "component_id", "record_class",
                                                "period_start", "period_end") if k in entry}
                fields["period_basis"] = entry.get("period_basis", "calendar")
                scope = RequestScope(**fields)
                results.append(self.create_draft(
                    scope, actor=actor, origin="historical_import", request_id=entry.get("request_id"),
                    state=entry.get("state", "submitted"), reason="historical import",
                    evidence=entry.get("evidence")))
        return results

    def _overlaps(self, conn, request_id):
        return tuple(r[0] for r in conn.execute(
            "SELECT detail FROM request_flags WHERE request_id=? AND flag='period_overlap' "
            "AND resolved_at IS NULL ORDER BY detail", (request_id,)))

    def _event(self, conn, request_id, from_state, to_state, actor, reason, evidence, now):
        conn.execute("INSERT INTO request_events(request_id, from_state, to_state, actor, reason, evidence, at) "
                     "VALUES (?,?,?,?,?,?,?)",
                     (request_id, from_state, to_state, actor, reason,
                      None if evidence is None else canonical_json(evidence), now))

    def get(self, request_id):
        row = self._conn.execute("SELECT * FROM requests WHERE request_id=?", (request_id,)).fetchone()
        if row is None:
            raise LedgerError("unknown request")
        return dict(row)

    def open_flags(self, request_id):
        return [dict(r) for r in self._conn.execute(
            "SELECT flag, detail FROM request_flags WHERE request_id=? AND resolved_at IS NULL "
            "ORDER BY flag, detail", (request_id,))]

    def resolve_flag(self, request_id, flag, detail, resolution, actor):
        _text(resolution, "resolution")
        now = self.clock()
        with self.transaction() as conn:
            changed = conn.execute(
                "UPDATE request_flags SET resolved_at=?, resolution=? WHERE request_id=? AND flag=? "
                "AND detail=? AND resolved_at IS NULL", (now, resolution, request_id, flag, detail)).rowcount
            if not changed:
                raise LedgerError("no such open flag")
            state = self.get(request_id)["state"]
            self._event(conn, request_id, state, state, actor, f"flag {flag} resolved: {resolution}",
                        {"flag": flag, "detail": detail}, now)

    def transition(self, request_id, to_state, actor, reason, evidence=None, expected_state=None):
        _text(actor, "actor")
        _text(reason, "reason", 2000)
        if to_state not in STATES:
            raise TransitionError("unknown state")
        now = self.clock()
        with self.transaction() as conn:
            row = conn.execute("SELECT state FROM requests WHERE request_id=?", (request_id,)).fetchone()
            if row is None:
                raise LedgerError("unknown request")
            current = row[0]
            if expected_state is not None and current != expected_state:
                raise TransitionError(f"request is {current}, expected {expected_state}")
            if to_state not in TRANSITIONS[current]:
                raise TransitionError(f"transition {current} -> {to_state} is not allowed")
            if to_state == "validated":
                if self.open_flags(request_id):
                    raise TransitionError("unresolved review flags block validation")
                if self.latest_revision(request_id) is None:
                    raise TransitionError("a rendered revision is required before validation")
            conn.execute("UPDATE requests SET state=? WHERE request_id=?", (to_state, request_id))
            self._event(conn, request_id, current, to_state, actor, reason, evidence, now)
        return to_state

    def add_revision(self, request_id, template_id, template_revision, subject, body,
                     recipient_route=None, actor="system"):
        """Store rendered content. Identical content is idempotent.

        Changing content after review returns the request to ``draft`` with an
        explicit event, which invalidates earlier review/authorization.
        """
        _text(template_id, "template_id")
        _text(template_revision, "template_revision")
        if not isinstance(subject, str) or not isinstance(body, str):
            raise LedgerError("subject/body must be text")
        content = sha256_text(canonical_json([subject, body, recipient_route]))
        now = self.clock()
        with self.transaction() as conn:
            row = conn.execute("SELECT state FROM requests WHERE request_id=?", (request_id,)).fetchone()
            if row is None:
                raise LedgerError("unknown request")
            state = row[0]
            latest = self.latest_revision(request_id)
            if latest is not None and latest["content_sha256"] == content:
                return latest["revision"], False
            if state in FROZEN_STATES:
                raise LedgerError(f"content is frozen in state {state}; record an amendment instead")
            if conn.execute("SELECT 1 FROM request_revisions WHERE request_id=? AND content_sha256=?",
                            (request_id, content)).fetchone() is not None:
                raise LedgerError("content equals an earlier revision; amend explicitly")
            number = conn.execute("SELECT COALESCE(MAX(revision), 0) + 1 FROM request_revisions WHERE request_id=?",
                                  (request_id,)).fetchone()[0]
            conn.execute("INSERT INTO request_revisions VALUES (?,?,?,?,?,?,?,?,?)",
                         (request_id, number, template_id, template_revision, recipient_route,
                          subject, body, content, now))
            self._event(conn, request_id, state, state, actor, f"revision {number} stored",
                        {"content_sha256": content, "template_id": template_id,
                         "template_revision": template_revision}, now)
            if state in REVIEWED_STATES:
                conn.execute("UPDATE requests SET state='draft' WHERE request_id=?", (request_id,))
                self._event(conn, request_id, state, "draft", actor,
                            "content changed; prior review/authorization invalidated", None, now)
            return number, True

    def latest_revision(self, request_id):
        row = self._conn.execute("SELECT * FROM request_revisions WHERE request_id=? ORDER BY revision DESC LIMIT 1",
                                 (request_id,)).fetchone()
        return None if row is None else dict(row)

    def events(self, request_id):
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM request_events WHERE request_id=? ORDER BY event_id", (request_id,))]

    def supersede(self, old_request_id, new_scope: RequestScope, actor, reason):
        """Record an intentional amendment/replacement without erasing history."""
        _text(reason, "reason", 2000)
        with self.transaction() as conn:
            old = self.get(old_request_id)
            result = self.create_draft(new_scope, actor=actor, origin="amendment",
                                       reason=f"amends {old_request_id}: {reason}")
            if result.request_id == old_request_id:
                raise LedgerError("amendment has the same semantic scope; store a revision instead")
            conn.execute("INSERT OR IGNORE INTO request_links VALUES (?,?,?,?,?)",
                         (result.request_id, old_request_id, "supersedes", reason, self.clock()))
            # The superseded request's overlap with its replacement is the intent.
            conn.execute("UPDATE request_flags SET resolved_at=?, resolution=? WHERE request_id=? "
                         "AND flag='period_overlap' AND detail=? AND resolved_at IS NULL",
                         (self.clock(), "explicit amendment", result.request_id, old_request_id))
            if old["state"] not in TERMINAL_STATES and "cancelled" in TRANSITIONS[old["state"]]:
                self.transition(old_request_id, "cancelled", actor, f"superseded by {result.request_id}: {reason}")
            return result

    def links(self, request_id):
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM request_links WHERE from_request_id=? OR to_request_id=? ORDER BY at, kind",
            (request_id, request_id))]

    def list_requests(self, states=None):
        query = "SELECT * FROM requests"
        params = ()
        if states:
            query += " WHERE state IN (%s)" % ",".join("?" * len(states))
            params = tuple(states)
        return [dict(r) for r in self._conn.execute(query + " ORDER BY created_at, request_id", params)]

    # -- integrity, backup and restore --------------------------------------
    def digest(self):
        """Deterministic digest of every request, revision, event and link."""
        return ledger_digest(self._conn)

    def backup(self, destination):
        """Write a consistent private backup and verify it before returning."""
        target = migrations.private_database_path(destination)
        if target.stat().st_size:
            raise LedgerError("backup destination must be a new empty private file")
        with closing(sqlite3.connect(str(target))) as copy:
            self._conn.backup(copy)
            if copy.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise LedgerError("backup integrity check failed")
            backup_digest = ledger_digest(copy)
        if backup_digest != self.digest():
            raise LedgerError("backup digest mismatch")
        return {"path": str(target), "digest": backup_digest}


def _scope_from_row(row):
    return RequestScope(row["requester"], row["jurisdiction"], row["component_id"], row["record_class"],
                        row["period_start"], row["period_end"], row["period_basis"])


def ledger_digest(conn):
    digest = hashlib.sha256()
    for table, order in (("requests", "request_id"), ("request_revisions", "request_id, revision"),
                         ("request_events", "event_id"), ("request_links", "from_request_id, to_request_id, kind"),
                         ("request_flags", "request_id, flag, detail"), ("component_aliases", "alias")):
        for row in conn.execute(f"SELECT * FROM {table} ORDER BY {order}"):
            digest.update(canonical_json([table, list(row)]).encode("utf-8"))
            digest.update(b"\n")
    return digest.hexdigest()


def restore(backup_path, destination, clock=utc_now):
    """Restore a verified backup into a new private ledger path."""
    source = Path(backup_path)
    with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as conn:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise LedgerError("backup integrity check failed")
        expected = ledger_digest(conn)
        target = migrations.private_database_path(destination)
        if target.stat().st_size:
            raise LedgerError("restore destination must be a new empty private file")
        with closing(sqlite3.connect(str(target))) as copy:
            conn.backup(copy)
    restored = Ledger(target, clock=clock)
    if restored.digest() != expected:
        restored.close()
        raise LedgerError("restored digest mismatch")
    return restored
