"""Offline SQLite schema migration. No external services.

Each schema version is applied exactly once inside one ``BEGIN IMMEDIATE``
transaction and recorded in ``schema_events``. Existing databases are validated
(tracking table shape, contiguous event history and expected ledger tables)
instead of trusting ``user_version`` alone. Future or inconsistent schemas are
rejected without being rewritten.
"""
import sqlite3
import os
import stat
from contextlib import closing
from pathlib import Path


def _private_database_path(path):
    location = Path(os.path.abspath(path))
    # mkdir's mode affects only the final directory with parents=True. Build
    # missing parents individually so none are accidentally world-readable.
    missing = []
    parent = location.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    if parent.resolve() != parent:
        raise RuntimeError("database directory symlinks are not supported")
    for folder in reversed(missing):
        try:
            folder.mkdir(mode=0o700)
        except FileExistsError:
            pass  # Reconcile the directory another starter just created below.
    directory = location.parent.lstat()
    if (not stat.S_ISDIR(directory.st_mode) or location.parent.resolve() != location.parent
            or stat.S_IMODE(directory.st_mode) & 0o077
            or (hasattr(os, "getuid") and directory.st_uid != os.getuid())):
        raise RuntimeError("private owned database directory required")
    descriptor = None
    try:
        descriptor = os.open(location, os.O_CREAT | os.O_EXCL | os.O_WRONLY
                             | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError:
        pass
    finally:
        if descriptor is not None:
            os.close(descriptor)
    current = location.lstat()
    if (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
            or stat.S_IMODE(current.st_mode) & 0o077
            or (hasattr(os, "getuid") and current.st_uid != os.getuid())):
        raise RuntimeError("private regular database file required")
    return location


def private_database_path(path):
    """Public wrapper used by backup/export helpers for new private files."""
    return _private_database_path(path)


def _v1(conn):
    conn.execute("CREATE TABLE schema_events (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")


# SFL-05 durable request ledger. Append-only evidence tables are enforced with
# triggers so later code cannot silently erase history.
_V2_STATEMENTS = (
    """CREATE TABLE component_aliases (
        alias TEXT PRIMARY KEY,
        canonical_id TEXT NOT NULL,
        reason TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        CHECK (alias <> canonical_id))""",
    """CREATE TABLE requests (
        request_id TEXT PRIMARY KEY,
        dedupe_key TEXT NOT NULL UNIQUE,
        requester TEXT NOT NULL,
        jurisdiction TEXT NOT NULL,
        component_id TEXT NOT NULL,
        record_class TEXT NOT NULL,
        period_start TEXT NOT NULL,
        period_end TEXT NOT NULL,
        period_basis TEXT NOT NULL,
        state TEXT NOT NULL,
        origin TEXT NOT NULL,
        created_at TEXT NOT NULL,
        CHECK (period_start <= period_end))""",
    "CREATE INDEX requests_scope ON requests(requester, jurisdiction, component_id, record_class)",
    """CREATE TABLE request_revisions (
        request_id TEXT NOT NULL REFERENCES requests(request_id),
        revision INTEGER NOT NULL,
        template_id TEXT NOT NULL,
        template_revision TEXT NOT NULL,
        recipient_route TEXT,
        subject TEXT NOT NULL,
        body TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (request_id, revision),
        UNIQUE (request_id, content_sha256))""",
    """CREATE TABLE request_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id TEXT NOT NULL REFERENCES requests(request_id),
        from_state TEXT,
        to_state TEXT NOT NULL,
        actor TEXT NOT NULL,
        reason TEXT NOT NULL,
        evidence TEXT,
        at TEXT NOT NULL)""",
    """CREATE TABLE request_flags (
        request_id TEXT NOT NULL REFERENCES requests(request_id),
        flag TEXT NOT NULL,
        detail TEXT NOT NULL,
        created_at TEXT NOT NULL,
        resolved_at TEXT,
        resolution TEXT,
        PRIMARY KEY (request_id, flag, detail))""",
    """CREATE TABLE request_links (
        from_request_id TEXT NOT NULL REFERENCES requests(request_id),
        to_request_id TEXT NOT NULL REFERENCES requests(request_id),
        kind TEXT NOT NULL,
        reason TEXT NOT NULL,
        at TEXT NOT NULL,
        PRIMARY KEY (from_request_id, to_request_id, kind))""",
    "CREATE TRIGGER request_events_no_update BEFORE UPDATE ON request_events BEGIN SELECT RAISE(ABORT, 'request events are append-only'); END",
    "CREATE TRIGGER request_events_no_delete BEFORE DELETE ON request_events BEGIN SELECT RAISE(ABORT, 'request events are append-only'); END",
    "CREATE TRIGGER request_revisions_no_update BEFORE UPDATE ON request_revisions BEGIN SELECT RAISE(ABORT, 'request revisions are immutable'); END",
    "CREATE TRIGGER request_revisions_no_delete BEFORE DELETE ON request_revisions BEGIN SELECT RAISE(ABORT, 'request revisions are immutable'); END",
    "CREATE TRIGGER requests_no_delete BEFORE DELETE ON requests BEGIN SELECT RAISE(ABORT, 'requests are never deleted'); END",
    "CREATE TRIGGER request_links_no_delete BEFORE DELETE ON request_links BEGIN SELECT RAISE(ABORT, 'request links are append-only'); END",
)


def _v2(conn):
    for statement in _V2_STATEMENTS:
        conn.execute(statement)


# SFL-02 component directory and contact routes.
_V3_STATEMENTS = (
    """CREATE TABLE directory_snapshots (
        snapshot_sha256 TEXT PRIMARY KEY,
        source_id TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        complete INTEGER NOT NULL,
        pages INTEGER NOT NULL,
        actions_json TEXT NOT NULL,
        recorded_at TEXT NOT NULL)""",
    """CREATE TABLE components (
        component_id TEXT PRIMARY KEY,
        agency_id TEXT NOT NULL,
        agency_name TEXT NOT NULL,
        name TEXT NOT NULL,
        status TEXT NOT NULL,
        aliases_json TEXT NOT NULL,
        source_id TEXT NOT NULL,
        first_seen_snapshot TEXT NOT NULL,
        last_seen_snapshot TEXT NOT NULL)""",
    """CREATE TABLE contact_routes (
        route_id TEXT PRIMARY KEY,
        component_id TEXT NOT NULL REFERENCES components(component_id),
        kind TEXT NOT NULL,
        destination TEXT NOT NULL,
        source_url TEXT NOT NULL,
        state TEXT NOT NULL,
        reason TEXT NOT NULL,
        redirects_json TEXT NOT NULL,
        verified_at TEXT,
        first_seen_snapshot TEXT NOT NULL,
        last_seen_snapshot TEXT NOT NULL,
        superseded_at TEXT)""",
)


def _v3(conn):
    for statement in _V3_STATEMENTS:
        conn.execute(statement)


# SFL-06 bounded dry runs.
_V4_STATEMENTS = (
    """CREATE TABLE dry_runs (
        run_id TEXT PRIMARY KEY,
        cap INTEGER NOT NULL CHECK (cap BETWEEN 1 AND 3),
        created_at TEXT NOT NULL,
        packet_sha256 TEXT,
        status TEXT NOT NULL)""",
    """CREATE TABLE dry_run_items (
        run_id TEXT NOT NULL REFERENCES dry_runs(run_id),
        ordinal INTEGER NOT NULL,
        item_key TEXT NOT NULL,
        PRIMARY KEY (run_id, item_key),
        UNIQUE (run_id, ordinal))""",
    "CREATE TRIGGER dry_run_item_cap BEFORE INSERT ON dry_run_items BEGIN "
    "SELECT RAISE(ABORT, 'dry-run cap exceeded') WHERE (SELECT COUNT(*) FROM dry_run_items WHERE run_id=NEW.run_id) "
    ">= (SELECT cap FROM dry_runs WHERE run_id=NEW.run_id); END",
)


def _v4(conn):
    for statement in _V4_STATEMENTS:
        conn.execute(statement)


# SFL-07 exact-batch authorization, operator roles, outbox and emergency stop.
_V5_STATEMENTS = (
    """CREATE TABLE operators (
        operator_id TEXT NOT NULL,
        role TEXT NOT NULL CHECK (role IN ('reviewer', 'authorizer', 'dispatcher', 'safety')),
        granted_by TEXT NOT NULL,
        granted_at TEXT NOT NULL,
        PRIMARY KEY (operator_id, role))""",
    """CREATE TABLE authorizations (
        auth_id TEXT PRIMARY KEY,
        envelope_json TEXT NOT NULL,
        envelope_sha256 TEXT NOT NULL UNIQUE,
        authorizer TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        max_messages INTEGER NOT NULL CHECK (max_messages >= 1),
        created_at TEXT NOT NULL,
        revoked_at TEXT,
        revoked_by TEXT,
        revoked_reason TEXT)""",
    """CREATE TABLE authorization_items (
        auth_id TEXT NOT NULL REFERENCES authorizations(auth_id),
        request_id TEXT NOT NULL REFERENCES requests(request_id),
        content_sha256 TEXT NOT NULL,
        route_id TEXT NOT NULL,
        destination TEXT NOT NULL,
        PRIMARY KEY (auth_id, request_id))""",
    """CREATE TABLE outbox (
        attempt_id TEXT PRIMARY KEY,
        auth_id TEXT NOT NULL REFERENCES authorizations(auth_id),
        request_id TEXT NOT NULL REFERENCES requests(request_id),
        content_sha256 TEXT NOT NULL,
        route_id TEXT NOT NULL,
        destination TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('queued', 'leased', 'submitting', 'submitted', 'failed',
                                             'unknown', 'cancelled')),
        lease_owner TEXT,
        lease_expires TEXT,
        not_before TEXT,
        tries INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL)""",
    "CREATE UNIQUE INDEX outbox_one_live_intent ON outbox(request_id) WHERE state IN "
    "('queued', 'leased', 'submitting', 'submitted', 'unknown')",
    """CREATE TABLE dispatch_control (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        set_by TEXT NOT NULL,
        reason TEXT NOT NULL,
        set_at TEXT NOT NULL)""",
    "CREATE TRIGGER authorizations_no_delete BEFORE DELETE ON authorizations BEGIN SELECT RAISE(ABORT, 'authorizations are retained'); END",
    "CREATE TRIGGER outbox_no_delete BEFORE DELETE ON outbox BEGIN SELECT RAISE(ABORT, 'outbox evidence is retained'); END",
)


def _v5(conn):
    for statement in _V5_STATEMENTS:
        conn.execute(statement)


# SFL-08 dispatch attempt log and provider receipts.
_V6_STATEMENTS = (
    """CREATE TABLE dispatch_log (
        log_id INTEGER PRIMARY KEY AUTOINCREMENT,
        attempt_id TEXT NOT NULL REFERENCES outbox(attempt_id),
        request_id TEXT NOT NULL,
        destination_host TEXT NOT NULL,
        at TEXT NOT NULL,
        outcome TEXT NOT NULL,
        provider_message_id TEXT,
        retry_after INTEGER,
        error_class TEXT)""",
    "CREATE INDEX dispatch_log_rate ON dispatch_log(destination_host, at)",
    """CREATE TABLE receipts (
        receipt_id TEXT PRIMARY KEY,
        attempt_id TEXT NOT NULL REFERENCES outbox(attempt_id),
        request_id TEXT NOT NULL,
        source TEXT NOT NULL,
        provider_message_id TEXT,
        receipt_json TEXT NOT NULL,
        receipt_sha256 TEXT NOT NULL,
        recorded_at TEXT NOT NULL)""",
    "CREATE TRIGGER dispatch_log_append_only BEFORE UPDATE ON dispatch_log BEGIN SELECT RAISE(ABORT, 'append-only'); END",
    "CREATE TRIGGER dispatch_log_no_delete BEFORE DELETE ON dispatch_log BEGIN SELECT RAISE(ABORT, 'append-only'); END",
    "CREATE TRIGGER receipts_immutable BEFORE UPDATE ON receipts BEGIN SELECT RAISE(ABORT, 'receipts are immutable'); END",
    "CREATE TRIGGER receipts_no_delete BEFORE DELETE ON receipts BEGIN SELECT RAISE(ABORT, 'receipts are immutable'); END",
)


def _v6(conn):
    for statement in _V6_STATEMENTS:
        conn.execute(statement)


# SFL-09 provider events, reconciliation and agency acknowledgments.
_V7_STATEMENTS = (
    """CREATE TABLE provider_events (
        event_id TEXT PRIMARY KEY,
        provider_message_id TEXT NOT NULL,
        attempt_id TEXT,
        request_id TEXT,
        event_type TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL,
        matched INTEGER NOT NULL,
        received_at TEXT NOT NULL)""",
    """CREATE TABLE acknowledgments (
        request_id TEXT NOT NULL REFERENCES requests(request_id),
        tracking_number TEXT NOT NULL,
        evidence_sha256 TEXT NOT NULL,
        source TEXT NOT NULL,
        recorded_by TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        PRIMARY KEY (request_id, tracking_number))""",
    "CREATE TRIGGER provider_events_immutable BEFORE UPDATE ON provider_events BEGIN SELECT RAISE(ABORT, 'append-only'); END",
    "CREATE TRIGGER acknowledgments_no_delete BEFORE DELETE ON acknowledgments BEGIN SELECT RAISE(ABORT, 'append-only'); END",
)


def _v7(conn):
    for statement in _V7_STATEMENTS:
        conn.execute(statement)


# SFL-10 response intake and fulfillment accounting.
_V8_STATEMENTS = (
    """CREATE TABLE responses (
        response_id TEXT PRIMARY KEY,
        request_id TEXT REFERENCES requests(request_id),
        tracking_number TEXT,
        kind TEXT NOT NULL,
        received_at TEXT NOT NULL,
        correlation TEXT NOT NULL,
        evidence_sha256 TEXT NOT NULL,
        recorded_by TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        detail_json TEXT NOT NULL)""",
    """CREATE TABLE response_attachments (
        response_id TEXT NOT NULL REFERENCES responses(response_id),
        sha256 TEXT NOT NULL,
        filename TEXT NOT NULL,
        size INTEGER NOT NULL,
        media_type TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('accepted', 'quarantined')),
        reason TEXT NOT NULL,
        PRIMARY KEY (response_id, sha256))""",
    """CREATE TABLE response_coverage (
        response_id TEXT NOT NULL REFERENCES responses(response_id),
        period_start TEXT NOT NULL,
        period_end TEXT NOT NULL,
        fields_json TEXT NOT NULL,
        PRIMARY KEY (response_id, period_start, period_end))""",
    """CREATE TABLE referrals (
        response_id TEXT NOT NULL REFERENCES responses(response_id),
        request_id TEXT NOT NULL REFERENCES requests(request_id),
        to_component_id TEXT NOT NULL,
        PRIMARY KEY (response_id, to_component_id))""",
    """CREATE TABLE followup_tasks (
        task_id TEXT PRIMARY KEY,
        request_id TEXT,
        response_id TEXT,
        kind TEXT NOT NULL,
        detail TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open',
        created_at TEXT NOT NULL)""",
    "CREATE TRIGGER responses_immutable BEFORE UPDATE ON responses BEGIN SELECT RAISE(ABORT, 'responses are immutable'); END",
    "CREATE TRIGGER responses_no_delete BEFORE DELETE ON responses BEGIN SELECT RAISE(ABORT, 'responses are immutable'); END",
)


def _v8(conn):
    for statement in _V8_STATEMENTS:
        conn.execute(statement)


MIGRATIONS = {1: _v1, 2: _v2, 3: _v3, 4: _v4, 5: _v5, 6: _v6, 7: _v7, 8: _v8}
SCHEMA_VERSION = max(MIGRATIONS)
# Tables each version must leave behind; used to reject partial/foreign schemas.
EXPECTED_TABLES = {
    1: {"schema_events"},
    2: {"component_aliases", "requests", "request_revisions", "request_events",
        "request_flags", "request_links"},
    3: {"directory_snapshots", "components", "contact_routes"},
    4: {"dry_runs", "dry_run_items"},
    5: {"operators", "authorizations", "authorization_items", "outbox", "dispatch_control"},
    6: {"dispatch_log", "receipts"},
    7: {"provider_events", "acknowledgments"},
    8: {"responses", "response_attachments", "response_coverage", "referrals", "followup_tasks"},
}


def _validate(conn, version):
    signature = [(row[1], row[2], row[3], row[4], row[5])
                 for row in conn.execute("PRAGMA table_info(schema_events)")]
    if signature != [("version", "INTEGER", 0, None, 1),
                      ("applied_at", "TEXT", 1, "CURRENT_TIMESTAMP", 0)]:
        raise RuntimeError("incompatible migration schema")
    events = [row[0] for row in conn.execute("SELECT version FROM schema_events ORDER BY version")]
    if events != list(range(1, version + 1)):
        raise RuntimeError("unexpected migration event history")
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for applied in range(1, version + 1):
        if not EXPECTED_TABLES[applied] <= tables:
            raise RuntimeError("schema tables missing for version %d" % applied)


def initialize(path):
    location = Path(path)
    if str(location) != ":memory:":
        location = _private_database_path(location)
    with closing(sqlite3.connect(str(location), timeout=30)) as conn:
        # Serialize the version check and migration so two starters cannot race.
        conn.execute("BEGIN IMMEDIATE")
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version < 0 or version > SCHEMA_VERSION:
                raise RuntimeError("unsupported future schema")
            if version >= 1:
                # Querying a missing tracking table raises OperationalError.
                conn.execute("SELECT COUNT(*) FROM schema_events").fetchone()
                _validate(conn, version)
            for step in range(version + 1, SCHEMA_VERSION + 1):
                MIGRATIONS[step](conn)
                conn.execute("INSERT INTO schema_events(version) VALUES (?)", (step,))
                conn.execute("PRAGMA user_version=%d" % step)
            _validate(conn, SCHEMA_VERSION)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return location
