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


MIGRATIONS = {1: _v1, 2: _v2}
SCHEMA_VERSION = max(MIGRATIONS)
# Tables each version must leave behind; used to reject partial/foreign schemas.
EXPECTED_TABLES = {
    1: {"schema_events"},
    2: {"component_aliases", "requests", "request_revisions", "request_events",
        "request_flags", "request_links"},
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
