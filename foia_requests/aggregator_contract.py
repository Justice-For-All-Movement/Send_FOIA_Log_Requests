"""FOIA_Aggregator interchange contracts, consumer side (single module).

1. **FC-13 catalog export (real, pinned).**
   repository  Justice-For-All-Movement/FOIA_Aggregator
   commit      ca822fad6bc089a48ede6d89d94cdad47967b11f (PR #72)
   contract    foia-catalog-export/1; schema vendored at
               ``foia_requests/schemas/catalog-record.v1.schema.json`` (SHA-256 pinned).
   SFL uses it for the agency/office census imported by SFL-02
   (``directory.import_catalog_census``). FC-13/1 carries no contact routes,
   so the census can only add *unverified* component candidates.

2. **Log-coverage proposal (not produced upstream).** FC-13/1 does not export
   request-log coverage (FC-03 rows exist in the Aggregator database but are not
   exported). SFL-03's coverage dataset is therefore a consumer *proposal*,
   ``sfl-log-coverage-proposal-1``, validated here and exercised only with
   synthetic fixtures. ``ext_corpus_satisfied`` stays false until FOIA_Aggregator
   publishes an equivalent export with a pinned commit and an accepted FC-14 review.

Coverage manifest (proposal)::

    contract "foia-aggregator-export", contract_version, dataset "log_coverage",
    snapshot_id, producer {"name", "commit"}, observed_at "...Z", fixture_mode,
    fc14_review {"status": "absent"|"in_review"|"accepted", "reference": str|null},
    records {"path", "sha256", "count"}

Coverage record (JSONL)::

    coverage_id, jurisdiction, agency_abbr, component_abbr, record_class
    ("foia_request_log" | "annual_report" | "log_link_only"), period_start,
    period_end, availability ("full" | "partial" | "unknown"), artifact_sha256,
    source_url, retrieved_at, missing_fields [...], date_gaps [[start, end], ...],
    synthetic
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

CONTRACT_NAME = "foia-aggregator-export"
CONTRACT_VERSION = "sfl-log-coverage-proposal-1"
SUPPORTED_VERSIONS = (CONTRACT_VERSION,)
AGGREGATOR_REPOSITORY = "Justice-For-All-Movement/FOIA_Aggregator"
AGGREGATOR_CONTRACT_COMMIT = "ca822fad6bc089a48ede6d89d94cdad47967b11f"
CATALOG_VERSION = "foia-catalog-export/1"
CATALOG_SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "catalog-record.v1.schema.json"
CATALOG_SCHEMA_SHA256 = "1e8abc956b31db20d4855b2c73fb785696f6f0c28c437b91c1fc83c9bd039f34"
CATALOG_REQUIRED_EXCLUSIONS = {"requester_name", "storage_key", "local_path"}
DATASET = "log_coverage"
LIVE_PRODUCER = "FOIA_Aggregator"
FIXTURE_PRODUCER = "synthetic-fixture"
RECORD_CLASSES = ("foia_request_log", "annual_report", "log_link_only")
AVAILABILITY = ("full", "partial", "unknown")
FC14_STATES = ("absent", "in_review", "accepted")
MANIFEST_FIELDS = {"contract", "contract_version", "dataset", "snapshot_id", "producer", "observed_at",
                   "fixture_mode", "fc14_review", "records"}
RECORD_FIELDS = {"coverage_id", "jurisdiction", "agency_abbr", "component_abbr", "record_class", "period_start",
                 "period_end", "availability", "artifact_sha256", "source_url", "retrieved_at", "missing_fields",
                 "date_gaps", "synthetic"}
MAX_BYTES = 64 * 1024 * 1024


class ContractError(ValueError):
    pass


def _require(condition, message):
    if not condition:
        raise ContractError(message)


def _date(value, name):
    _require(isinstance(value, str) and len(value) == 10, f"{name} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ContractError(f"{name} must be a valid date") from exc


@dataclass(frozen=True)
class CoverageExport:
    snapshot_id: str
    producer: dict
    observed_at: str
    fixture_mode: bool
    fc14_review: dict
    manifest_sha256: str
    records: tuple

    @property
    def corpus_gate_satisfied(self) -> bool:
        """EXT-CORPUS evidence: a live, producer-pinned export with accepted FC-14 review."""
        return (not self.fixture_mode and self.producer["name"] == LIVE_PRODUCER
                and self.fc14_review["status"] == "accepted")

    def gate_report(self):
        return {"contract_version": CONTRACT_VERSION, "snapshot_id": self.snapshot_id,
                "producer": self.producer, "observed_at": self.observed_at, "fixture_mode": self.fixture_mode,
                "manifest_sha256": self.manifest_sha256, "fc14_review": self.fc14_review,
                "ext_corpus_satisfied": self.corpus_gate_satisfied}


def validate_manifest(manifest):
    _require(isinstance(manifest, dict) and set(manifest) == MANIFEST_FIELDS, "manifest fields mismatch")
    _require(manifest["contract"] == CONTRACT_NAME and manifest["dataset"] == DATASET, "not a log_coverage export")
    _require(manifest["contract_version"] in SUPPORTED_VERSIONS, "unsupported contract version")
    _require(isinstance(manifest["snapshot_id"], str) and manifest["snapshot_id"], "snapshot_id required")
    producer = manifest["producer"]
    _require(isinstance(producer, dict) and set(producer) == {"name", "commit"}, "producer invalid")
    fixture = manifest["fixture_mode"]
    _require(type(fixture) is bool, "fixture_mode must be boolean")
    if fixture:
        _require(producer == {"name": FIXTURE_PRODUCER, "commit": "synthetic"}, "fixture producer mismatch")
    else:
        _require(producer["name"] == LIVE_PRODUCER and isinstance(producer["commit"], str)
                 and re.fullmatch(r"[0-9a-f]{40}", producer["commit"]) is not None,
                 "live export must pin a FOIA_Aggregator commit")
    _require(isinstance(manifest["observed_at"], str) and manifest["observed_at"].endswith("Z"), "observed_at UTC")
    datetime.fromisoformat(manifest["observed_at"].replace("Z", "+00:00"))
    review = manifest["fc14_review"]
    _require(isinstance(review, dict) and set(review) == {"status", "reference"} and review["status"] in FC14_STATES,
             "fc14_review invalid")
    records = manifest["records"]
    _require(isinstance(records, dict) and set(records) == {"path", "sha256", "count"}, "records invalid")
    _require(isinstance(records["sha256"], str) and re.fullmatch("[0-9a-f]{64}", records["sha256"]) is not None,
             "records.sha256 invalid")
    _require(type(records["count"]) is int and records["count"] >= 0, "records.count invalid")
    _require(isinstance(records["path"], str) and "/" not in records["path"] and not records["path"].startswith("."),
             "records.path must be a plain file name")
    return manifest


def validate_record(record, fixture):
    _require(isinstance(record, dict) and set(record) == RECORD_FIELDS, "coverage record fields mismatch")
    _require(record["synthetic"] is fixture, "synthetic record cannot cross fixture/live boundary")
    for key in ("coverage_id", "jurisdiction", "agency_abbr", "component_abbr"):
        _require(isinstance(record[key], str) and record[key], f"{key} required")
    _require(record["record_class"] in RECORD_CLASSES, "unknown record_class")
    _require(record["availability"] in AVAILABILITY, "unknown availability")
    start, end = _date(record["period_start"], "period_start"), _date(record["period_end"], "period_end")
    _require(start <= end, "period_start after period_end")
    _require(record["artifact_sha256"] is None or re.fullmatch("[0-9a-f]{64}", record["artifact_sha256"]),
             "artifact_sha256 invalid")
    _require(isinstance(record["missing_fields"], list), "missing_fields must be a list")
    gaps = record["date_gaps"]
    _require(isinstance(gaps, list), "date_gaps must be a list")
    for gap in gaps:
        _require(isinstance(gap, list) and len(gap) == 2, "date gap must be [start, end]")
        g0, g1 = _date(gap[0], "gap start"), _date(gap[1], "gap end")
        _require(start <= g0 <= g1 <= end, "date gap outside record period")
    if record["availability"] == "full":
        _require(record["artifact_sha256"] is not None, "full coverage requires an artifact hash")
    return record


def load_export(directory) -> CoverageExport:
    root = Path(directory)
    raw = (root / "manifest.json").read_bytes()
    _require(len(raw) <= 1024 * 1024, "manifest too large")
    manifest = validate_manifest(json.loads(raw.decode("utf-8")))
    data = (root / manifest["records"]["path"]).read_bytes()
    _require(len(data) <= MAX_BYTES, "coverage records exceed size bound")
    _require(hashlib.sha256(data).hexdigest() == manifest["records"]["sha256"], "coverage records SHA-256 mismatch")
    rows = [validate_record(json.loads(line), manifest["fixture_mode"])
            for line in data.decode("utf-8").splitlines() if line.strip()]
    _require(len(rows) == manifest["records"]["count"], "coverage record count mismatch")
    ids = [r["coverage_id"] for r in rows]
    _require(len(ids) == len(set(ids)), "duplicate coverage_id")
    return CoverageExport(manifest["snapshot_id"], manifest["producer"], manifest["observed_at"],
                          manifest["fixture_mode"], manifest["fc14_review"], hashlib.sha256(raw).hexdigest(),
                          tuple(rows))


# -- FC-13 catalog export (real, pinned) --------------------------------------
_TYPES = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool, "null": type(None)}


def load_catalog_schema():
    raw = CATALOG_SCHEMA_PATH.read_bytes()
    _require(hashlib.sha256(raw).hexdigest() == CATALOG_SCHEMA_SHA256, "vendored FC-13 schema SHA-256 mismatch")
    return json.loads(raw.decode("utf-8"))


def _schema_validate(instance, schema, path="$"):
    types = schema.get("type")
    if types is not None:
        allowed = types if isinstance(types, list) else [types]
        _require(any(isinstance(instance, _TYPES[t]) and not (t == "integer" and isinstance(instance, bool))
                     for t in allowed), f"{path}: expected {allowed}")
    if isinstance(instance, dict):
        for key in schema.get("required", []):
            _require(key in instance, f"{path}: missing {key}")
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            _require(not set(instance) - set(props), f"{path}: unexpected fields")
        for key, value in instance.items():
            if key in props:
                _schema_validate(value, props[key], f"{path}.{key}")
    if isinstance(instance, list) and "items" in schema:
        for i, item in enumerate(instance):
            _schema_validate(item, schema["items"], f"{path}[{i}]")
    if isinstance(instance, str):
        if "enum" in schema:
            _require(instance in schema["enum"], f"{path}: value not allowed")
        if "pattern" in schema:
            _require(re.search(schema["pattern"], instance) is not None, f"{path}: pattern mismatch")


def _normalize_url(url):
    from urllib.parse import urlsplit, urlunsplit
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower() or "https"
    host = (parts.hostname or "").lower()
    if parts.port and (scheme, parts.port) not in (("https", 443), ("http", 80)):
        host = f"{host}:{parts.port}"
    return urlunsplit((scheme, host, parts.path or "/", parts.query, ""))


def validate_catalog_record(record, schema=None):
    _schema_validate(record, schema or load_catalog_schema())
    expected = "rec_" + hashlib.sha256(_normalize_url(record["url"]).encode("utf-8")).hexdigest()[:24]
    _require(record["record_id"] == expected, "record_id does not derive from url")
    archived = record["preservation"]["status"] == "archived"
    _require(archived == (record["preservation"]["sha256"] is not None), "sha256 present iff archived")
    return record


@dataclass(frozen=True)
class CatalogSnapshot:
    snapshot_id: str
    manifest_sha256: str
    generated_at: str
    fixture: bool
    records: tuple

    def census(self):
        """Agency/office pairs observed in released records (no contact data)."""
        out = {}
        for record in self.records:
            agency, office = record["agency"], record["office"]
            if not agency:
                continue
            key = (agency["slug"], office["slug"] if office else None)
            entry = out.setdefault(key, {"agency_slug": agency["slug"], "agency_name": agency["name"],
                                         "office_slug": key[1], "office_name": office["name"] if office else None,
                                         "records": 0, "source_ids": set()})
            entry["records"] += 1
            entry["source_ids"].update(s["source_id"] for s in record["sources"])
        return [dict(v, source_ids=sorted(v["source_ids"])) for _, v in sorted(out.items(), key=lambda kv: (kv[0][0], kv[0][1] or ""))]


def load_catalog_snapshot(directory, expected_fixture=True) -> CatalogSnapshot:
    """Verify an ``export-catalog`` snapshot (hashes, counts, schema, unique IDs)."""
    root = Path(directory)
    raw = (root / "manifest.json").read_bytes()
    _require(len(raw) <= 1024 * 1024, "manifest too large")
    manifest = json.loads(raw.decode("utf-8"))
    _require(isinstance(manifest, dict) and manifest.get("schema_version") == CATALOG_VERSION,
             "unsupported FC-13 schema_version")
    _require(re.fullmatch(r"snap_[0-9a-f]{20}", str(manifest.get("snapshot_id"))) is not None, "snapshot_id invalid")
    _require(CATALOG_REQUIRED_EXCLUSIONS <= set(manifest.get("excluded_fields", [])), "privacy exclusions missing")
    fixture = (root / "FIXTURE.json").is_file()
    _require(fixture == expected_fixture, "fixture mode mismatch")
    schema = load_catalog_schema()
    records, seen, total = [], set(), 0
    for entry in manifest.get("files", []):
        _require(isinstance(entry, dict) and re.fullmatch(r"records-\d{5}\.jsonl", str(entry.get("path"))) is not None,
                 "file entry invalid")
        data = (root / entry["path"]).read_bytes()
        _require(len(data) <= MAX_BYTES, "snapshot file too large")
        _require(hashlib.sha256(data).hexdigest() == entry["sha256"], f"{entry['path']} SHA-256 mismatch")
        lines = [json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip()]
        _require(len(lines) == entry["records"], f"{entry['path']} record count mismatch")
        for record in lines:
            validate_catalog_record(record, schema)
            _require(record["record_id"] not in seen, "duplicate record_id")
            seen.add(record["record_id"])
        records.extend(lines)
        total += len(lines)
    _require(total == manifest.get("counts", {}).get("records"), "record total disagrees with manifest counts")
    return CatalogSnapshot(manifest["snapshot_id"], hashlib.sha256(raw).hexdigest(), manifest["generated_at"],
                           fixture, tuple(records))

