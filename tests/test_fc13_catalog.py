import copy
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from foia_requests import aggregator_contract as contract
from foia_requests import directory
from foia_requests.aggregator_contract import ContractError, load_catalog_snapshot, load_export
from foia_requests.ledger import Ledger

ROOT = Path(__file__).resolve().parents[1]
PRODUCER = ROOT / "fixtures" / "fc13_producer_snapshot"


class CatalogContractTests(unittest.TestCase):
    def test_schema_pin_and_producer_snapshot(self):
        self.assertEqual(hashlib.sha256(contract.CATALOG_SCHEMA_PATH.read_bytes()).hexdigest(),
                         contract.CATALOG_SCHEMA_SHA256)
        self.assertRegex(contract.AGGREGATOR_CONTRACT_COMMIT, r"^[0-9a-f]{40}$")
        snapshot = load_catalog_snapshot(PRODUCER)
        self.assertEqual((snapshot.snapshot_id, len(snapshot.records)), ("snap_936bc978fe5fc655b664", 3))
        census = snapshot.census()
        self.assertEqual(len(census), 1)
        self.assertEqual((census[0]["agency_slug"], census[0]["office_slug"], census[0]["records"]),
                         ("example-dept", "example-office", 3))
        with self.assertRaisesRegex(ContractError, "fixture mode"):
            load_catalog_snapshot(PRODUCER, expected_fixture=False)

    def test_tampering_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            copy_dir = Path(folder) / "snap"
            shutil.copytree(PRODUCER, copy_dir)
            path = copy_dir / "records-00001.jsonl"
            original = path.read_text()
            record = json.loads(original.splitlines()[0])
            cases = [lambda r: r.update(record_id="rec_" + "0" * 24), lambda r: r.update(extra=1),
                     lambda r: r["preservation"].update(sha256=None), lambda r: r["sources"][0].update(provenance="x")]
            for mutate in cases:
                bad = copy.deepcopy(record)
                mutate(bad)
                with self.subTest(mutation=mutate), self.assertRaises(ContractError):
                    contract.validate_catalog_record(bad)
            path.write_text(original + "\n")
            path.write_text(original.replace("Contract A", "Contract Z"))
            with self.assertRaisesRegex(ContractError, "SHA-256"):
                load_catalog_snapshot(copy_dir)
            path.write_text(original)
            manifest = json.loads((copy_dir / "manifest.json").read_text())
            manifest["excluded_fields"] = []
            (copy_dir / "manifest.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ContractError, "privacy"):
                load_catalog_snapshot(copy_dir)

    def test_log_coverage_is_a_proposal_and_gate_stays_closed(self):
        export = load_export(ROOT / "fixtures" / "aggregator_export")
        report = export.gate_report()
        self.assertEqual(report["contract_version"], "sfl-log-coverage-proposal-1")
        self.assertFalse(report["ext_corpus_satisfied"])


class CensusImportTests(unittest.TestCase):
    def test_import_creates_unverified_candidates_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = Ledger(Path(folder) / "l" / "ledger.sqlite")
            try:
                snapshot = load_catalog_snapshot(PRODUCER)
                result = directory.import_catalog_census(ledger, snapshot)
                self.assertEqual(result["created"], ["fed:example-dept/example-office"])
                self.assertEqual(result["actions"][0]["action"], "verify_contact_routes")
                record = directory.component_record(ledger, "fed:example-dept/example-office")
                self.assertEqual((record["status"], record["name"]), ("unknown", "Example Office"))
                self.assertEqual(directory.routes_for(ledger, "fed:example-dept/example-office"), [])
                self.assertFalse(directory.import_catalog_census(ledger, snapshot)["stored"])
            finally:
                ledger.close()

    def test_existing_directory_component_is_matched_not_downgraded(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = Ledger(Path(folder) / "l" / "ledger.sqlite")
            try:
                found = directory.discover(directory.load_source(ROOT / "fixtures/directory"),
                                           directory.fixture_fetcher(ROOT / "fixtures/directory"))
                directory.persist(ledger, found)
                snapshot = load_catalog_snapshot(PRODUCER)
                renamed = tuple(dict(r, agency={"slug": "doj", "name": "Department of Justice"},
                                     office={"slug": "oip", "name": "OIP (catalog name)"}) for r in snapshot.records)
                result = directory.import_catalog_census(ledger, snapshot.__class__(
                    snapshot.snapshot_id, "f" * 64, snapshot.generated_at, True, renamed))
                self.assertEqual(result["matched"], ["fed:doj/oip"])
                record = directory.component_record(ledger, "fed:doj/oip")
                self.assertEqual(record["status"], "current")
                self.assertIn("OIP (catalog name)", record["aliases"])
            finally:
                ledger.close()

    def test_cli_catalog_import(self):
        with tempfile.TemporaryDirectory() as folder:
            result = subprocess.run([sys.executable, "-m", "foia_requests", "directory", "--catalog", str(PRODUCER),
                                     "--ledger", str(Path(folder) / "s" / "ledger.sqlite")],
                                    cwd=ROOT, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["created"], 1)


if __name__ == "__main__":
    unittest.main()
