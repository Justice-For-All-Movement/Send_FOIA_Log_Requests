"""SFL-12 Aggregator interchange bundles and completion reporting.

SFL never writes the Aggregator database. It publishes a versioned, checksummed
bundle (``sfl-interchange/1``) that FOIA_Aggregator can ingest, for example
through its FC-03 request-log path:

    manifest.json          schema_version, generated_at, counts, files[{path, records, sha256}]
    components.jsonl       public component directory entries + official routes
    requests.jsonl         request scope/state per component-period (no requester data, no bodies)
    fulfilled_logs.jsonl   accepted response attachments with delivered coverage
    artifacts/<sha256>     the accepted attachment bytes (quarantined files never leave)

Privacy: requester principal, profile, rendered bodies (which carry the reply
route), personal fields and quarantined files are excluded, and
``export_bundle`` fails if any blocked marker string still appears in the output.
Downstream acknowledgment is idempotent per ``ack_id``; an unacknowledged
bundle reports ``awaiting_downstream`` (upstream unavailable is explicit).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

from .ledger import canonical_json

SCHEMA_VERSION = "sfl-interchange/1"
FILES = ("components.jsonl", "requests.jsonl", "fulfilled_logs.jsonl")


class InterchangeError(RuntimeError):
    pass


def _private_dir(path):
    path = Path(os.path.abspath(path))
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise InterchangeError("bundle directory must be private (0700)")
    if any(path.iterdir()):
        raise InterchangeError("bundle directory must be empty")
    return path


def _lines(rows):
    return "".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in rows)


def collect(ledger):
    conn = ledger.connection
    components = []
    for row in conn.execute("SELECT * FROM components ORDER BY component_id"):
        routes = [{"kind": r["kind"], "destination": r["destination"], "state": r["state"],
                   "source_url": r["source_url"]}
                  for r in conn.execute("SELECT * FROM contact_routes WHERE component_id=? AND superseded_at IS NULL "
                                        "ORDER BY route_id", (row["component_id"],))]
        components.append({"component_id": row["component_id"], "agency_id": row["agency_id"],
                           "agency_name": row["agency_name"], "name": row["name"], "status": row["status"],
                           "aliases": json.loads(row["aliases_json"]), "routes": routes})
    requests = []
    for row in conn.execute("SELECT * FROM requests ORDER BY request_id"):
        revision = ledger.latest_revision(row["request_id"])
        requests.append({"request_id": row["request_id"], "component_id": row["component_id"],
                         "jurisdiction": row["jurisdiction"], "record_class": row["record_class"],
                         "period": [row["period_start"], row["period_end"], row["period_basis"]],
                         "state": row["state"], "content_sha256": revision["content_sha256"] if revision else None})
    fulfilled = []
    for row in conn.execute(
            "SELECT a.sha256, a.filename, a.media_type, a.size, r.response_id, r.request_id, r.kind, r.received_at, "
            "q.component_id FROM response_attachments a JOIN responses r USING(response_id) "
            "JOIN requests q ON q.request_id = r.request_id WHERE a.state='accepted' "
            "ORDER BY r.request_id, r.response_id, a.sha256"):
        coverage = [{"period": [c["period_start"], c["period_end"]], "fields": json.loads(c["fields_json"])}
                    for c in conn.execute("SELECT * FROM response_coverage WHERE response_id=? ORDER BY period_start",
                                          (row["response_id"],))]
        fulfilled.append({"sha256": row["sha256"], "filename": row["filename"], "media_type": row["media_type"],
                          "size": row["size"], "request_id": row["request_id"], "component_id": row["component_id"],
                          "response_kind": row["kind"], "received_at": row["received_at"], "coverage": coverage})
    return {"components.jsonl": components, "requests.jsonl": requests, "fulfilled_logs.jsonl": fulfilled}


def export_bundle(ledger, out_dir, *, artifact_dir, generated_at, blocked_markers=()):
    out = _private_dir(out_dir)
    data = collect(ledger)
    files = []
    for name in FILES:
        body = _lines(data[name])
        for marker in blocked_markers:
            if marker and marker in body:
                shutil.rmtree(out)
                raise InterchangeError("private data would leave in the bundle; export aborted")
        (out / name).write_text(body, encoding="utf-8")
        files.append({"path": name, "records": len(data[name]), "sha256": hashlib.sha256(body.encode()).hexdigest()})
    (out / "artifacts").mkdir(mode=0o700)
    for item in data["fulfilled_logs.jsonl"]:
        source = Path(artifact_dir) / item["sha256"]
        if hashlib.sha256(source.read_bytes()).hexdigest() != item["sha256"]:
            raise InterchangeError("stored artifact does not match its hash")
        target = out / "artifacts" / item["sha256"]
        if not target.exists():
            shutil.copyfile(source, target)
    counts = {name.split(".")[0]: len(data[name]) for name in FILES}
    states = {}
    for request in data["requests.jsonl"]:
        states[request["state"]] = states.get(request["state"], 0) + 1
    manifest = {"schema_version": SCHEMA_VERSION, "generated_at": generated_at, "counts": counts,
                "request_states": dict(sorted(states.items())), "files": files,
                "excluded": ["requester principal", "requester profile", "rendered bodies", "quarantined attachments"]}
    raw = json.dumps(manifest, indent=2, sort_keys=True)
    (out / "manifest.json").write_text(raw, encoding="utf-8")
    bundle_sha = hashlib.sha256(raw.encode()).hexdigest()
    with ledger.transaction() as conn:
        conn.execute("INSERT OR IGNORE INTO interchange_exports VALUES (?,?,?,?,?)",
                     (bundle_sha, SCHEMA_VERSION, generated_at, canonical_json(counts), ledger.clock()))
    return {"bundle_sha256": bundle_sha, "manifest": manifest}


def import_bundle(directory):
    """Verify and load a bundle (round-trip / downstream replay)."""
    root = Path(directory)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise InterchangeError("unsupported interchange version")
    loaded = {}
    for entry in manifest["files"]:
        if entry["path"] not in FILES:
            raise InterchangeError("unexpected file in bundle")
        body = (root / entry["path"]).read_bytes()
        if hashlib.sha256(body).hexdigest() != entry["sha256"]:
            raise InterchangeError(f"{entry['path']} SHA-256 mismatch")
        rows = [json.loads(line) for line in body.decode("utf-8").splitlines() if line]
        if len(rows) != entry["records"]:
            raise InterchangeError(f"{entry['path']} record count mismatch")
        loaded[entry["path"]] = rows
    for item in loaded.get("fulfilled_logs.jsonl", []):
        data = (root / "artifacts" / item["sha256"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise InterchangeError("artifact hash mismatch")
    return manifest, loaded


def record_ack(ledger, bundle_sha256, *, consumer, ack_id, status, detail=""):
    if status not in ("accepted", "rejected"):
        raise InterchangeError("ack status must be accepted or rejected")
    with ledger.transaction() as conn:
        if conn.execute("SELECT 1 FROM interchange_exports WHERE bundle_sha256=?", (bundle_sha256,)).fetchone() is None:
            raise InterchangeError("unknown bundle")
        existing = conn.execute("SELECT bundle_sha256, status FROM interchange_acks WHERE ack_id=?",
                                (ack_id,)).fetchone()
        if existing is not None:
            if tuple(existing) != (bundle_sha256, status):
                raise InterchangeError("ack_id reused with different content")
            return "duplicate"
        conn.execute("INSERT INTO interchange_acks VALUES (?,?,?,?,?,?)",
                     (ack_id, bundle_sha256, consumer, status, detail, ledger.clock()))
        return "recorded"


def delivery_status(ledger, bundle_sha256):
    rows = ledger.connection.execute("SELECT consumer, status FROM interchange_acks WHERE bundle_sha256=? "
                                     "ORDER BY received_at, ack_id", (bundle_sha256,)).fetchall()
    if not rows:
        return {"status": "awaiting_downstream", "acks": []}
    final = rows[-1]["status"]
    return {"status": "acknowledged" if final == "accepted" else "rejected_downstream",
            "acks": [dict(r) for r in rows]}
