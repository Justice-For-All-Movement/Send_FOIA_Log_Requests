"""Consumer-side FOIA_Aggregator export contract for log coverage (single module).

FOIA_Aggregator's FC-13 export contract is being defined concurrently; no
producer schema/commit is published yet. This module pins the consumer draft
``fc13-consumer-draft-1`` for the ``log_coverage`` dataset. Every field, enum
and invariant SFL relies on lives here so alignment with the real FC-13 export
is a one-module change plus fixture regeneration. Nothing here contacts the
Aggregator or writes its database.

Manifest::

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
CONTRACT_VERSION = "fc13-consumer-draft-1"
SUPPORTED_VERSIONS = (CONTRACT_VERSION,)
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
