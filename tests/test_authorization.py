import copy
import json
import shutil
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from foia_requests import directory
from foia_requests.aggregator_contract import load_export
from foia_requests.authorization import (AuthorizationError, authorize_batch, begin_submission, claim_next,
                                         clear_stop, envelope, grant_role, outbox_rows, queue_authorized, revoke,
                                         set_stop)
from foia_requests.config import Config
from foia_requests.dryrun import record_review, run_dry_run
from foia_requests.ledger import Ledger, canonical_json, sha256_text
from foia_requests.periods import calendar_year
from foia_requests.templates import TEMPLATE_DIR, RequesterProfile, load_template

ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-11-15T00:00:00Z"
LATER = "2026-11-20T00:00:00Z"
PROFILE = json.loads((ROOT / "fixtures/profiles/synthetic-requester.json").read_text())


def approved_template(folder):
    """Test-only copy of the template marked approved so the live-ready path can be exercised."""
    target = Path(folder) / "approved-templates"
    shutil.copytree(TEMPLATE_DIR, target)
    path = target / "annual_foia_log_request.v1.json"
    data = json.loads(path.read_text())
    data["status"] = "approved"
    path.write_text(json.dumps(data))
    (target / "LOCK.json").write_text(json.dumps({"annual_foia_log_request@1": sha256_text(canonical_json(data))}))
    return load_template(directory=target)


def test_profile(**changes):
    data = copy.deepcopy(PROFILE)
    data.update(synthetic=False, verified=True, name="Test Operator (fixture identity)")
    data.update(changes)
    return RequesterProfile.from_dict(data)


class AuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.tmp = Path(self.folder.name)
        self.ledger = Ledger(self.tmp / "state" / "ledger.sqlite", clock=lambda: NOW)
        found = directory.discover(directory.load_source(ROOT / "fixtures/directory"),
                                   directory.fixture_fetcher(ROOT / "fixtures/directory"))
        directory.persist(self.ledger, found)
        self.template = approved_template(self.tmp)
        self.profile = test_profile()
        packet, folder = run_dry_run(self.ledger, Config(), selection=[("fed:doj/oip", calendar_year(2024)),
                                                                       ("fed:doj/oip", calendar_year(2022))],
                                     export=load_export(ROOT / "fixtures/aggregator_export"), profile=self.profile,
                                     template=self.template, output_dir=self.tmp / "packets", as_of=NOW)
        self.assertTrue(all(e["live_ready"] for e in packet["entries"] if e["decision"] == "draft"))
        self.request_ids = [e["request_id"] for e in packet["entries"] if e["decision"] == "draft"]
        record_review(self.ledger, folder / "packet.json", "reviewer:a", "fixture review")
        for operator, role in (("auth:a", "authorizer"), ("disp:a", "dispatcher"), ("safety:a", "safety")):
            grant_role(self.ledger, operator, role, "owner:test")

    def tearDown(self):
        self.ledger.close()
        self.folder.cleanup()

    def authorize(self, **overrides):
        kwargs = dict(request_ids=self.request_ids, authorizer="auth:a", reference="TEST-APPROVAL-1",
                      expires_at=LATER, profile=self.profile, template=self.template, transport="fake",
                      max_messages=2, now=NOW)
        kwargs.update(overrides)
        return authorize_batch(self.ledger, **kwargs)

    def test_authorization_preconditions(self):
        with self.assertRaisesRegex(AuthorizationError, "lacks the authorizer role"):
            self.authorize(authorizer="someone")
        with self.assertRaisesRegex(AuthorizationError, "verified, non-synthetic"):
            self.authorize(profile=RequesterProfile.from_dict(PROFILE))
        with self.assertRaisesRegex(AuthorizationError, "max_messages"):
            self.authorize(max_messages=1)
        with self.assertRaisesRegex(AuthorizationError, "future"):
            self.authorize(expires_at=NOW)
        with self.assertRaisesRegex(AuthorizationError, "not live-ready"):
            self.authorize(template=load_template())  # unapproved template: content identical, not live-ready
        with self.assertRaisesRegex(AuthorizationError, "differs"):
            self.authorize(profile=test_profile(delivery_format="PDF only"))
        self.assertTrue(all(self.ledger.get(r)["state"] == "dry_run_reviewed" for r in self.request_ids))

    def test_happy_path_to_submitting_binds_exact_content(self):
        auth_id, digest = self.authorize()
        env = envelope(self.ledger, auth_id)
        self.assertEqual(sha256_text(canonical_json(env)), digest)
        self.assertEqual(len(env["items"]), 2)
        queued = queue_authorized(self.ledger, auth_id, "disp:a", now=NOW)
        self.assertEqual(len(queued), 2)
        self.assertEqual(queue_authorized(self.ledger, auth_id, "disp:a", now=NOW), [])
        attempt = claim_next(self.ledger, "worker-1", NOW)
        message = begin_submission(self.ledger, attempt, "worker-1", NOW)
        revision = self.ledger.latest_revision(message.request_id)
        self.assertEqual((message.subject, message.body, message.content_sha256),
                         (revision["subject"], revision["body"], revision["content_sha256"]))
        self.assertEqual(message.destination, "foia-oip@doj.gov.example")
        self.assertEqual(self.ledger.get(message.request_id)["state"], "submitting")
        # An attempt that reached submitting is never reclaimed, even after its lease expires.
        other = claim_next(self.ledger, "worker-2", "2026-11-16T00:00:00Z")
        self.assertNotEqual(other, attempt)

    def test_content_edit_after_authorization_invalidates(self):
        auth_id, _ = self.authorize()
        self.ledger.add_revision(self.request_ids[0], "annual_foia_log_request", "1", "S", "edited", None)
        self.assertEqual(self.ledger.get(self.request_ids[0])["state"], "draft")
        with self.assertRaisesRegex(AuthorizationError, "content or route changed"):
            queue_authorized(self.ledger, auth_id, "disp:a", now=NOW)
        row = self.ledger.connection.execute("SELECT revoked_at, revoked_reason FROM authorizations").fetchone()
        self.assertIsNotNone(row["revoked_at"])
        self.assertEqual(self.ledger.get(self.request_ids[1])["state"], "draft")

    def test_route_change_after_queue_blocks_submission(self):
        auth_id, _ = self.authorize()
        queue_authorized(self.ledger, auth_id, "disp:a", now=NOW)
        attempt = claim_next(self.ledger, "worker-1", NOW)
        self.ledger.connection.execute("UPDATE contact_routes SET state='stale'")
        with self.assertRaisesRegex(AuthorizationError, "route no longer verified"):
            begin_submission(self.ledger, attempt, "worker-1", NOW)
        self.assertTrue(all(r["state"] == "cancelled" for r in outbox_rows(self.ledger, auth_id)))
        self.assertTrue(all(self.ledger.get(r)["state"] == "draft" for r in self.request_ids))

    def test_expiry_revocation_and_stop(self):
        auth_id, _ = self.authorize()
        queue_authorized(self.ledger, auth_id, "disp:a", now=NOW)
        set_stop(self.ledger, "safety:a", "incident drill")
        self.assertIsNone(claim_next(self.ledger, "w", NOW))
        with self.assertRaises(AuthorizationError):
            set_stop(self.ledger, "disp:a", "no role")
        clear_stop(self.ledger, "safety:a", "drill over")
        attempt = claim_next(self.ledger, "w", NOW)
        self.assertIsNotNone(attempt)
        set_stop(self.ledger, "safety:a", "stop before send")
        with self.assertRaisesRegex(AuthorizationError, "stop"):
            begin_submission(self.ledger, attempt, "w", NOW)
        clear_stop(self.ledger, "safety:a", "resume")
        revoke(self.ledger, auth_id, "auth:a", "operator revoked")
        self.assertTrue(all(r["state"] == "cancelled" for r in outbox_rows(self.ledger, auth_id)))
        with self.assertRaisesRegex(AuthorizationError, "revoked"):
            begin_submission(self.ledger, attempt, "w", NOW)

    def test_expired_authorization_is_never_claimed(self):
        auth_id, _ = self.authorize()
        queue_authorized(self.ledger, auth_id, "disp:a", now=NOW)
        self.assertIsNone(claim_next(self.ledger, "w", "2026-11-21T00:00:00Z"))
        reason = self.ledger.connection.execute("SELECT revoked_reason FROM authorizations").fetchone()[0]
        self.assertEqual(reason, "expired")
        self.assertIsNone(claim_next(self.ledger, "w", NOW))

    def test_concurrent_claims_never_share_an_attempt(self):
        auth_id, _ = self.authorize()
        queue_authorized(self.ledger, auth_id, "disp:a", now=NOW)
        claims = []

        def worker(i):
            ledger = Ledger(self.ledger.path)
            try:
                claims.append(claim_next(ledger, f"worker-{i}", NOW))
            finally:
                ledger.close()
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        taken = [c for c in claims if c]
        self.assertEqual(len(taken), 2)
        self.assertEqual(len(set(taken)), 2)

    def test_one_live_intent_per_request(self):
        auth_id, _ = self.authorize()
        queue_authorized(self.ledger, auth_id, "disp:a", now=NOW)
        row = outbox_rows(self.ledger)[0]
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.connection.execute(
                "INSERT INTO outbox(attempt_id, auth_id, request_id, content_sha256, route_id, destination, state, "
                "created_at, updated_at) VALUES ('dup', ?, ?, 'x', 'r', 'd', 'queued', ?, ?)",
                (auth_id, row["request_id"], NOW, NOW))
        with self.assertRaises(sqlite3.DatabaseError):
            self.ledger.connection.execute("DELETE FROM outbox")


if __name__ == "__main__":
    unittest.main()
