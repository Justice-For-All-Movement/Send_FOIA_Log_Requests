import itertools
import random
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from foia_requests.ledger import (Ledger, LedgerError, RequestScope, TransitionError, dedupe_key,
                                  restore)

ROOT = Path(__file__).resolve().parents[1]


def scope(component="fed:doj/oip", start="2024-01-01", end="2024-12-31", requester="requester:synthetic-1",
          basis="calendar"):
    return RequestScope(requester, "us-federal", component, "foia_request_log", start, end, basis)


def fixed_clock():
    counter = itertools.count()
    return lambda: "2026-11-15T00:00:%02dZ" % (next(counter) % 60)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "private" / "ledger.sqlite"
        self.ledger = Ledger(self.path, clock=fixed_clock())

    def tearDown(self):
        self.ledger.close()
        self.folder.cleanup()

    def test_in_memory_ledger_rejected(self):
        with self.assertRaises(LedgerError):
            Ledger(":memory:")

    def test_semantic_key_ignores_wording_basis_and_route(self):
        first = self.ledger.create_draft(scope())
        again = self.ledger.create_draft(scope(basis="custom"))
        self.assertTrue(first.created)
        self.assertFalse(again.created)
        self.assertEqual(first.request_id, again.request_id)
        self.ledger.add_revision(first.request_id, "annual-log", "1", "Subject", "Body one", "route:a")
        self.ledger.add_revision(first.request_id, "annual-log", "2", "Subject", "Body two", "route:b")
        self.assertEqual(len(self.ledger.list_requests()), 1)
        self.assertEqual(self.ledger.latest_revision(first.request_id)["revision"], 2)

    def test_distinct_scope_dimensions_produce_distinct_keys(self):
        base = scope()
        variants = [scope(component="fed:doj/fbi"), scope(start="2023-01-01", end="2023-12-31"),
                    scope(requester="requester:synthetic-2"),
                    RequestScope(base.requester, "us-state-ca", base.component_id, base.record_class,
                                 base.period_start, base.period_end)]
        keys = {dedupe_key(base)} | {dedupe_key(v) for v in variants}
        self.assertEqual(len(keys), 5)

    def test_invalid_scope_rejected(self):
        for kwargs in ({"start": "2024-13-01"}, {"start": "2024-12-31", "end": "2024-01-01"},
                       {"start": "2024-1-1"}, {"component": ""}, {"requester": "a\nb"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(LedgerError):
                scope(**kwargs)
        with self.assertRaises(LedgerError):
            RequestScope("r", "us-federal", "c", "annual_report", "2024-01-01", "2024-12-31")

    def test_overlap_is_flagged_and_blocks_validation_until_resolved(self):
        calendar = self.ledger.create_draft(scope())
        fiscal = self.ledger.create_draft(scope(start="2023-10-01", end="2024-09-30", basis="fiscal"))
        self.assertTrue(fiscal.created)
        self.assertEqual(fiscal.overlaps, (calendar.request_id,))
        self.ledger.add_revision(fiscal.request_id, "annual-log", "1", "S", "B")
        with self.assertRaisesRegex(TransitionError, "flags"):
            self.ledger.transition(fiscal.request_id, "validated", "operator", "check")
        self.ledger.resolve_flag(fiscal.request_id, "period_overlap", calendar.request_id,
                                 "agency reports by fiscal year", "operator")
        self.ledger.transition(fiscal.request_id, "validated", "operator", "check")
        self.assertEqual(self.ledger.get(fiscal.request_id)["state"], "validated")

    def test_state_machine_rejects_skips_and_requires_reason(self):
        result = self.ledger.create_draft(scope())
        rid = result.request_id
        with self.assertRaisesRegex(TransitionError, "revision"):
            self.ledger.transition(rid, "validated", "operator", "no content yet")
        self.ledger.add_revision(rid, "annual-log", "1", "S", "B")
        with self.assertRaises(TransitionError):
            self.ledger.transition(rid, "submitted", "operator", "skip")
        with self.assertRaises(LedgerError):
            self.ledger.transition(rid, "validated", "operator", "")
        with self.assertRaises(TransitionError):
            self.ledger.transition(rid, "validated", "operator", "x", expected_state="queued")
        for state in ("validated", "dry_run_reviewed", "authorized", "queued", "submitting", "submission_unknown"):
            self.ledger.transition(rid, state, "operator", "fixture step")
        # An unknown outcome is never automatically re-queued.
        with self.assertRaises(TransitionError):
            self.ledger.transition(rid, "queued", "operator", "retry")
        self.ledger.transition(rid, "submitted", "operator", "provider status shows accepted",
                               evidence={"receipt": "synthetic"})
        with self.assertRaises(LedgerError):
            self.ledger.add_revision(rid, "annual-log", "2", "S", "changed after send")
        self.assertEqual([e["to_state"] for e in self.ledger.events(rid)][-3:],
                         ["submitting", "submission_unknown", "submitted"])

    def test_content_change_after_review_invalidates_review(self):
        rid = self.ledger.create_draft(scope()).request_id
        self.ledger.add_revision(rid, "annual-log", "1", "S", "B")
        self.ledger.transition(rid, "validated", "operator", "ok")
        self.ledger.transition(rid, "dry_run_reviewed", "operator", "ok")
        self.assertEqual(self.ledger.add_revision(rid, "annual-log", "1", "S", "B"), (1, False))
        self.assertEqual(self.ledger.get(rid)["state"], "dry_run_reviewed")
        self.ledger.add_revision(rid, "annual-log", "2", "S", "B2")
        self.assertEqual(self.ledger.get(rid)["state"], "draft")
        self.assertIn("invalidated", self.ledger.events(rid)[-1]["reason"])
        with self.assertRaisesRegex(LedgerError, "earlier revision"):
            self.ledger.add_revision(rid, "annual-log", "1", "S", "B")

    def test_events_and_revisions_are_append_only(self):
        rid = self.ledger.create_draft(scope()).request_id
        self.ledger.add_revision(rid, "annual-log", "1", "S", "B")
        conn = self.ledger.connection
        for statement in ("UPDATE request_events SET reason='x'", "DELETE FROM request_events",
                          "UPDATE request_revisions SET body='x'", "DELETE FROM request_revisions",
                          "DELETE FROM requests"):
            with self.subTest(statement=statement), self.assertRaises(sqlite3.DatabaseError):
                conn.execute(statement)

    def test_reordered_repeated_history_import_is_idempotent(self):
        entries = [
            {"requester": "requester:synthetic-1", "jurisdiction": "us-federal", "component_id": c,
             "record_class": "foia_request_log", "period_start": f"{y}-01-01", "period_end": f"{y}-12-31",
             "state": "submitted", "evidence": {"tracking": f"synthetic-{c}-{y}"}}
            for c in ("fed:doj/oip", "fed:dhs/uscis") for y in (2021, 2022, 2023)]
        self.ledger.import_history(entries)
        shuffled = list(entries)
        random.Random(7).shuffle(shuffled)
        self.ledger.import_history(shuffled)
        self.ledger.import_history(entries[:2])
        requests = self.ledger.list_requests()
        self.assertEqual(len(requests), 6)
        self.assertTrue(all(r["state"] == "submitted" and r["origin"] == "historical_import" for r in requests))
        # Generating a draft for an imported scope returns the imported request.
        again = self.ledger.create_draft(scope(start="2022-01-01", end="2022-12-31"))
        self.assertFalse(again.created)
        self.assertEqual(self.ledger.get(again.request_id)["state"], "submitted")

    def test_component_alias_resolution_and_rekey(self):
        old = self.ledger.create_draft(scope(component="fed:doj/old-office")).request_id
        self.assertTrue(self.ledger.add_component_alias("fed:doj/old-office", "fed:doj/oip", "renamed 2025"))
        self.assertFalse(self.ledger.add_component_alias("fed:doj/old-office", "fed:doj/oip", "renamed 2025"))
        self.assertEqual(self.ledger.get(old)["component_id"], "fed:doj/oip")
        again = self.ledger.create_draft(scope(component="fed:doj/oip"))
        self.assertEqual(again.request_id, old)
        self.assertFalse(again.created)
        with self.assertRaises(LedgerError):
            self.ledger.add_component_alias("fed:doj/oip", "fed:doj/old-office", "cycle")

    def test_alias_collision_is_flagged_not_merged(self):
        a = self.ledger.create_draft(scope(component="fed:x/a")).request_id
        b = self.ledger.create_draft(scope(component="fed:x/b")).request_id
        self.ledger.add_component_alias("fed:x/a", "fed:x/b", "merger")
        self.assertEqual(len(self.ledger.list_requests()), 2)
        self.assertEqual(self.ledger.open_flags(a), [{"flag": "alias_duplicate", "detail": b}])
        self.assertEqual(self.ledger.open_flags(b), [{"flag": "alias_duplicate", "detail": a}])

    def test_amendment_links_without_erasing_history(self):
        old = self.ledger.create_draft(scope()).request_id
        self.ledger.add_revision(old, "annual-log", "1", "S", "B")
        new = self.ledger.supersede(old, scope(end="2024-06-30"), "operator", "agency asked to narrow period")
        self.assertTrue(new.created)
        self.assertEqual(self.ledger.get(old)["state"], "cancelled")
        self.assertEqual(self.ledger.open_flags(new.request_id), [])
        links = self.ledger.links(old)
        self.assertEqual([(l["from_request_id"], l["kind"]) for l in links], [(new.request_id, "supersedes")])
        self.assertEqual(self.ledger.latest_revision(old)["body"], "B")
        self.assertGreaterEqual(len(self.ledger.events(old)), 3)
        with self.assertRaises(LedgerError):
            self.ledger.supersede(new.request_id, scope(end="2024-06-30"), "operator", "same scope")

    def test_backup_restore_preserves_everything_and_dedupe(self):
        rid = self.ledger.create_draft(scope()).request_id
        self.ledger.add_revision(rid, "annual-log", "1", "S", "B")
        self.ledger.import_history([{"requester": "requester:synthetic-1", "jurisdiction": "us-federal",
                                     "component_id": "fed:dhs/uscis", "record_class": "foia_request_log",
                                     "period_start": "2020-01-01", "period_end": "2020-12-31"}])
        backup_path = Path(self.folder.name) / "backups" / "ledger-backup.sqlite"
        manifest = self.ledger.backup(backup_path)
        self.assertEqual(manifest["digest"], self.ledger.digest())
        self.assertEqual(backup_path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(LedgerError):
            self.ledger.backup(backup_path)
        restored = restore(backup_path, Path(self.folder.name) / "restore" / "ledger.sqlite")
        try:
            self.assertEqual(restored.digest(), self.ledger.digest())
            self.assertFalse(restored.create_draft(scope()).created)
            self.assertEqual(len(restored.list_requests()), 2)
        finally:
            restored.close()

    def test_concurrent_processes_create_one_canonical_request(self):
        self.ledger.close()
        code = ("import sys\n"
                "from foia_requests.ledger import Ledger, RequestScope\n"
                "l = Ledger(sys.argv[1])\n"
                "for y in range(2015, 2025):\n"
                "    l.create_draft(RequestScope('requester:synthetic-1', 'us-federal', 'fed:doj/oip',"
                " 'foia_request_log', f'{y}-01-01', f'{y}-12-31'))\n"
                "l.close()\n")
        children = [subprocess.Popen([sys.executable, "-c", code, str(self.path)], cwd=ROOT,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    for _ in range(4)]
        for child in children:
            _, stderr = child.communicate(timeout=30)
            self.assertEqual(child.returncode, 0, stderr)
        self.ledger = Ledger(self.path)
        self.assertEqual(len(self.ledger.list_requests()), 10)


if __name__ == "__main__":
    unittest.main()
