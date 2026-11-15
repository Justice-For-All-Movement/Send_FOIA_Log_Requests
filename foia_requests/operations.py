"""SFL-13 durable operation: verified backups, isolated restore checks, health.

No hosting, scheduler or paid service is provisioned here (EXT-RUNTIME).
These are the local procedures a hosted deployment must also run.

* ``full_digest`` hashes every table (requests, events, outbox, receipts,
  responses, ...), so a restore proves *all* evidence survived.
* ``verified_backup`` refuses to run below a free-space floor, writes a private
  backup plus a manifest, and checks the backup's full digest.
* ``isolated_restore_check`` restores into a scratch directory and proves the
  digest matches and that nothing already submitted, unknown or submitting can
  be claimed again (a restart never resends).
* ``health_report`` distinguishes not_configured / never_run / degraded / healthy.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from . import migrations
from .authorization import claim_next, stopped
from .ledger import LedgerError, canonical_json, restore

MIN_FREE_BYTES = 64 * 1024 * 1024


def _ts(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def full_digest(conn) -> str:
    digest = hashlib.sha256()
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                         "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    for table in tables:
        columns = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        order = ", ".join(f'"{c}"' for c in columns)
        for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY {order}'):
            digest.update(canonical_json([table, list(row)]).encode("utf-8"))
    return digest.hexdigest()


def verified_backup(ledger, destination, *, min_free_bytes=MIN_FREE_BYTES, disk_usage=shutil.disk_usage):
    destination = Path(os.path.abspath(destination))
    probe = destination.parent
    while not probe.exists():
        probe = probe.parent
    free = disk_usage(probe).free
    if free < min_free_bytes:
        raise LedgerError(f"insufficient free space for a backup ({free} bytes < {min_free_bytes})")
    result = ledger.backup(destination)
    with closing(sqlite3.connect(f"file:{destination}?mode=ro", uri=True)) as copy:
        backup_full = full_digest(copy)
    if backup_full != full_digest(ledger.connection):
        raise LedgerError("backup full digest mismatch")
    manifest = {"path": str(destination), "created_at": ledger.clock(), "full_digest": backup_full,
                "ledger_digest": result["digest"], "schema_version": migrations.SCHEMA_VERSION,
                "size": destination.stat().st_size}
    manifest_path = destination.with_name(destination.name + ".manifest.json")
    fd = os.open(manifest_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(manifest, stream, indent=2, sort_keys=True)
    return manifest


def isolated_restore_check(backup_path, manifest, scratch_dir, now):
    target = Path(scratch_dir) / "restore-check" / "ledger.sqlite"
    restored = restore(backup_path, target)
    try:
        digest_ok = full_digest(restored.connection) == manifest["full_digest"]
        protected = restored.connection.execute(
            "SELECT COUNT(*) FROM outbox WHERE state IN ('submitting','submitted','unknown')").fetchone()[0]
        claimed = []
        while True:
            attempt = claim_next(restored, "restore-check", now)
            if attempt is None:
                break
            claimed.append(attempt)
        resend = restored.connection.execute(
            "SELECT COUNT(*) FROM outbox WHERE attempt_id IN (%s) AND tries > 0" % ",".join("?" * len(claimed)),
            claimed).fetchone()[0] if claimed else 0
        return {"digest_matches": digest_ok, "protected_attempts": protected,
                "claimable_after_restore": len(claimed), "would_resend": resend,
                "passed": digest_ok and resend == 0}
    finally:
        restored.close()


def health_report(ledger, now, *, backup_manifest=None, stuck_after_hours=1, backup_max_age_hours=24):
    conn = ledger.connection
    states = {r[0]: r[1] for r in conn.execute("SELECT state, COUNT(*) FROM requests GROUP BY state")}
    routes = {r[0]: r[1] for r in conn.execute(
        "SELECT state, COUNT(*) FROM contact_routes WHERE superseded_at IS NULL GROUP BY state")}
    components = conn.execute("SELECT COUNT(*) FROM components").fetchone()[0]
    stuck = conn.execute("SELECT attempt_id, state, updated_at FROM outbox WHERE state IN ('submitting','unknown') "
                         "ORDER BY updated_at").fetchall()
    oldest = stuck[0]["updated_at"] if stuck else None
    unmatched = conn.execute("SELECT COUNT(*) FROM responses WHERE request_id IS NULL").fetchone()[0]
    tasks = conn.execute("SELECT COUNT(*) FROM followup_tasks WHERE status='open'").fetchone()[0]
    backup_age = None
    if backup_manifest:
        backup_age = (_ts(now) - _ts(backup_manifest["created_at"])).total_seconds() / 3600
    problems = []
    if stuck and (_ts(now) - _ts(oldest)).total_seconds() > stuck_after_hours * 3600:
        problems.append("attempts stuck in submitting/unknown need reconciliation")
    if stopped(ledger):
        problems.append("dispatch stop is active")
    if unmatched:
        problems.append("uncorrelated responses")
    if states and (backup_age is None or backup_age > backup_max_age_hours):
        problems.append("no recent verified backup")
    if not components and not states:
        status = "not_configured"
    elif not states:
        status = "never_run"
    else:
        status = "degraded" if problems else "healthy"
    return {"status": status, "problems": problems, "request_states": dict(sorted(states.items())),
            "routes": dict(sorted(routes.items())), "components": components,
            "stuck_attempts": len(stuck), "oldest_stuck_at": oldest, "unmatched_responses": unmatched,
            "open_tasks": tasks, "backup_age_hours": backup_age, "dispatch_stopped": stopped(ledger),
            "note": "process-up status cannot certify submissions"}
