"""End-to-end tests of the owner-gated sending path.

Every message goes to a local fake SMTP server on 127.0.0.1 or to local
manual-filing files. Recipients are the synthetic ``.gov.example`` fixture
routes; no real agency is ever contacted.
"""
import copy
import email
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from email import policy
from pathlib import Path

from foia_requests import directory, sending
from foia_requests.aggregator_contract import load_export
from foia_requests.authorization import (authorize_batch, grant_role, queue_authorized, set_stop, stopped)
from foia_requests.config import Config
from foia_requests.dispatch import RateLimits
from foia_requests.dryrun import record_review, run_dry_run
from foia_requests.ledger import Ledger
from foia_requests.manual_filing import ManualFilingError, record_manual_filing
from foia_requests.operations import health_report
from foia_requests.pdf import text_pdf
from foia_requests.periods import calendar_year
from foia_requests.sending import SendingBlocked, blockers, send_batch
from foia_requests.smtp_transport import SmtpSender, SmtpSettings
from foia_requests.templates import (TEMPLATE_DIR, RequesterProfile, TemplateError, approval_phrase,
                                     approve_template, load_active_template, load_template)
from tests.smtp_fixture import CertFolder, FakeSmtpServer, smtp_env

ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-11-16T00:00:00Z"
PROFILE = json.loads((ROOT / "fixtures/profiles/synthetic-requester.json").read_text())


def live_profile(**changes):
    data = copy.deepcopy(PROFILE)
    data.update(profile_id="test-live-1", synthetic=False, verified=True, name="Test Requester (fixture identity)",
                reply_email="replies@sender.example")
    data.update(changes)
    return RequesterProfile.from_dict(data)


def approved_copy(folder):
    target = Path(folder) / "templates"
    shutil.copytree(TEMPLATE_DIR, target)
    base = load_active_template(directory=target)
    return approve_template(directory=target, reviewer_role="test reviewer role", reviewed_on="2026-11-16",
                            scope="test-only approval", confirm=approval_phrase(base))


class Clock:
    def __init__(self, value=NOW):
        self.value = value

    def __call__(self):
        return self.value


def build_batch(test, transport="smtp", components=("fed:doj/oip", "fed:doj/foia-unit"), years=(2021,)):
    test.folder = tempfile.TemporaryDirectory()
    test.tmp = Path(test.folder.name)
    test.clock = Clock()
    test.ledger = Ledger(test.tmp / "state" / "ledger.sqlite", clock=test.clock)
    directory.persist(test.ledger, directory.discover(directory.load_source(ROOT / "fixtures/directory"),
                                                      directory.fixture_fetcher(ROOT / "fixtures/directory")))
    test.template = approved_copy(test.tmp)
    test.profile = live_profile()
    selection = [(c, calendar_year(y)) for c in components for y in years]
    ids = []
    for start in range(0, len(selection), 3):
        packet, folder = run_dry_run(test.ledger, Config(), selection=selection[start:start + 3],
                                     export=load_export(ROOT / "fixtures/aggregator_export"), profile=test.profile,
                                     template=test.template, output_dir=test.tmp / "packets", as_of=NOW)
        record_review(test.ledger, folder / "packet.json", "owner", "reviewed")
        ids += [e["request_id"] for e in packet["entries"] if e["decision"] == "draft"]
    for role in ("authorizer", "dispatcher", "safety"):
        grant_role(test.ledger, "owner", role, "owner")
    test.ids = ids
    test.auth_id, _ = authorize_batch(test.ledger, request_ids=ids, authorizer="owner", reference="TEST-BATCH",
                                      expires_at="2026-11-30T00:00:00Z", profile=test.profile,
                                      template=test.template, transport=transport, max_messages=len(ids), now=NOW)
    queue_authorized(test.ledger, test.auth_id, "owner", now=NOW)


class SmtpSendingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.certs = CertFolder()

    @classmethod
    def tearDownClass(cls):
        cls.certs.cleanup()

    def setUp(self):
        build_batch(self)

    def tearDown(self):
        self.ledger.close()
        self.folder.cleanup()

    def server(self, mode="normal"):
        return FakeSmtpServer(self.certs.cert, self.certs.key, mode=mode)

    def send(self, server, confirm="auto", env_changes=None, **kwargs):
        env = smtp_env(server, self.certs.cert)
        env.update(env_changes or {})
        if confirm == "auto":
            confirm = sending.batch_plan(self.ledger, self.auth_id)["confirmation_phrase"]
        kwargs.setdefault("limits", RateLimits(10, 10))
        return send_batch(self.ledger, auth_id=self.auth_id, env=env, profile=self.profile, template=self.template,
                          confirm=confirm, **kwargs)

    def states(self):
        return [self.ledger.get(r)["state"] for r in self.ids]

    def test_dry_run_by_default_sends_nothing(self):
        with self.server() as server:
            result = self.send(server, confirm=None)
        self.assertEqual((result["mode"], result["sent"], result["blockers"]), ("dry_run", 0, []))
        self.assertEqual(result["confirmation_phrase"], f"SEND 2 {self.auth_id}")
        self.assertEqual(server.connections, 0)
        self.assertEqual(self.states(), ["queued", "queued"])

    def test_every_precondition_is_required(self):
        cases = {
            "env flag missing": dict(env_changes={"FOIA_SENDING_ENABLED": "1"}),
            "env flag false": dict(env_changes={"FOIA_SENDING_ENABLED": "false"}),
            "no transport": dict(env_changes={"FOIA_TRANSPORT_PROVIDER": "disabled"}),
            "transport mismatch": dict(env_changes={"FOIA_TRANSPORT_PROVIDER": "manual"}),
            "smtp password unset": dict(env_changes={"FOIA_SMTP_PASSWORD": ""}),
            "smtp ip literal": dict(env_changes={"FOIA_SMTP_HOST": "127.0.0.1"}),
            "wrong confirmation": dict(confirm="SEND 2 auth-wrong"),
            "lowercase confirmation": dict(confirm=f"send 2 {self.auth_id}"),
        }
        with self.server() as server:
            for name, kwargs in cases.items():
                with self.subTest(name), self.assertRaises(SendingBlocked):
                    self.send(server, **kwargs)
            unapproved = load_template(directory=self.template.source_dir, revision="1")
            for name, kwargs in {
                    "synthetic profile": dict(profile=RequesterProfile.from_dict(PROFILE)),
                    "unverified profile": dict(profile=live_profile(verified=False)),
                    "other profile": dict(profile=live_profile(profile_id="someone-else")),
                    "unapproved template": dict(template=unapproved)}.items():
                with self.subTest(name), self.assertRaises(SendingBlocked):
                    env = smtp_env(server, self.certs.cert)
                    send_batch(self.ledger, auth_id=self.auth_id, env=env,
                               **{"profile": self.profile, "template": self.template,
                                  "confirm": f"SEND 2 {self.auth_id}", **kwargs})
        self.assertEqual(server.connections, 0)
        self.assertEqual(self.states(), ["queued", "queued"])

    def test_lock_mismatch_blocks(self):
        lock = Path(self.template.source_dir) / "LOCK.json"
        data = json.loads(lock.read_text())
        data[self.template.key] = "0" * 64
        lock.write_text(json.dumps(data))
        with self.server() as server, self.assertRaisesRegex(SendingBlocked, "not pinned in LOCK.json"):
            self.send(server)
        self.assertEqual(server.connections, 0)

    def test_confirmed_send_delivers_exact_content_with_receipts_once(self):
        with self.server() as server:
            result = self.send(server)
            again = self.send(server, confirm=f"SEND 2 {self.auth_id}")
        self.assertEqual((result["status"], result["sent"], result["remaining"]), ("batch_processed", 2, 0))
        self.assertEqual(again["status"], "nothing_pending")
        self.assertEqual(len(server.messages), 2)
        self.assertEqual(self.states(), ["submitted", "submitted"])
        for stored in server.messages:
            parsed = email.message_from_bytes(stored["raw"], policy=policy.default)
            self.assertTrue(stored["to"][0].endswith(".gov.example"))
            self.assertEqual(parsed["Reply-To"], "replies@sender.example")
            request_id = next(r for r in self.ids if self.ledger.latest_revision(r)["subject"] == parsed["Subject"])
            self.assertEqual(parsed.get_content().replace("\r\n", "\n").rstrip("\n"),
                             self.ledger.latest_revision(request_id)["body"].rstrip("\n"))
        rows = self.ledger.connection.execute("SELECT * FROM receipts ORDER BY request_id").fetchall()
        self.assertEqual(len(rows), 2)
        receipt = json.loads(rows[0]["receipt_json"])
        self.assertEqual(receipt["smtp_reply_code"], 250)
        self.assertIn("queued as FAKE", receipt["smtp_reply"])
        self.assertEqual(receipt["tls"], "starttls")
        import hashlib
        self.assertIn(receipt["rfc822_sha256"], {hashlib.sha256(m["raw"]).hexdigest() for m in server.messages})
        self.assertEqual(rows[0]["provider_message_id"], receipt["message_id"])
        self.assertNotIn("test-password", json.dumps([dict(r) for r in rows]))

    def test_rate_limits_and_max_are_honoured(self):
        with self.server() as server:
            result = self.send(server, limits=RateLimits(per_destination_per_hour=1, global_per_hour=10))
        # Both fixture routes share the doj.gov.example host: one message, the other waits an hour.
        self.assertEqual((result["sent"], result["remaining"]), (1, 1))
        self.assertEqual(result["results"][-1]["status"], "rate_limited_local")
        with self.server() as server:
            with self.assertRaisesRegex(SendingBlocked, "rate limits"):
                self.send(server, limits=RateLimits(per_destination_per_hour=100, global_per_hour=1000))

    def test_max_and_interval(self):
        pauses = []
        with self.server() as server:
            result = self.send(server, max_messages=1, min_interval_seconds=5, sleep=pauses.append)
            self.assertEqual((result["sent"], result["remaining"], len(server.messages)), (1, 1, 1))
            result = self.send(server, min_interval_seconds=5, sleep=pauses.append)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(pauses, [])  # one message per run here, so no pause was needed

    def test_emergency_stop_blocks_and_halts(self):
        set_stop(self.ledger, "owner", "test stop")
        with self.server() as server, self.assertRaisesRegex(SendingBlocked, "emergency stop"):
            self.send(server)
        self.assertEqual(server.connections, 0)

    def test_server_without_starttls_is_refused_before_credentials(self):
        with self.server("no_starttls") as server:
            result = self.send(server)
        self.assertEqual(result["sent"], 0)
        self.assertNotIn("AUTH", server.commands)
        self.assertNotIn("MAIL", server.commands)
        self.assertEqual(server.messages, [])
        self.assertIn("delivery_failed", self.states())

    def test_bad_credentials_stop_dispatch(self):
        server = FakeSmtpServer(self.certs.cert, self.certs.key, password="different")
        with server:
            result = self.send(server)
        self.assertEqual(result["results"][0]["outcome"], "auth_failed")
        self.assertTrue(stopped(self.ledger))
        self.assertEqual(server.messages, [])

    def test_recipient_refusal_and_tempfail(self):
        with self.server("reject_rcpt") as server:
            result = self.send(server, max_messages=1)
        self.assertEqual(result["results"][0]["outcome"], "rejected")
        with self.server("tempfail_rcpt") as server:
            result = self.send(server, max_messages=1)
        self.assertEqual(result["results"][0]["status"], "retry_scheduled")

    def test_drop_after_data_is_unknown_and_blocks_further_sends(self):
        with self.server("drop_after_data") as server:
            result = self.send(server)
            self.assertEqual(result["results"][0]["status"], "submission_unknown")
            self.assertEqual(result["sent"], 0)
            self.assertEqual(len(server.messages), 1)
            with self.assertRaisesRegex(SendingBlocked, "unknown outcome"):
                self.send(server, confirm=f"SEND 1 {self.auth_id}")
        self.assertIn("submission_unknown", self.states())

    def test_env_flag_removed_mid_batch_halts(self):
        env = None
        with self.server() as server:
            env = smtp_env(server, self.certs.cert)

            class Flip(SmtpSender):
                def send(inner, message):
                    outcome = SmtpSender.send(inner, message)
                    env["FOIA_SENDING_ENABLED"] = "no"
                    return outcome

            sender = Flip(SmtpSettings.from_env(env), reply_to="replies@sender.example")
            result = send_batch(self.ledger, auth_id=self.auth_id, env=env, profile=self.profile,
                                template=self.template, confirm=f"SEND 2 {self.auth_id}", sender=sender,
                                limits=RateLimits(10, 10))
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["results"][-1]["status"], "halted")


class ManualFilingTests(unittest.TestCase):
    def setUp(self):
        build_batch(self, transport="manual", components=("fed:doj/oip",), years=(2021, 2022))

    def tearDown(self):
        self.ledger.close()
        self.folder.cleanup()

    def test_manual_packets_checklist_and_receipt(self):
        env = {"FOIA_SENDING_ENABLED": "true", "FOIA_TRANSPORT_PROVIDER": "manual"}
        out = self.tmp / "manual"
        dry = send_batch(self.ledger, auth_id=self.auth_id, env=env, profile=self.profile, template=self.template)
        self.assertEqual(dry["mode"], "dry_run")
        self.assertFalse(out.exists())
        result = send_batch(self.ledger, auth_id=self.auth_id, env=env, profile=self.profile,
                            template=self.template, confirm=dry["confirmation_phrase"], manual_dir=out,
                            ledger_path="ledger.sqlite")
        self.assertEqual((result["sent"], result["manual_copies"]), (0, 2))
        checklist = Path(result["manual_checklist"]).read_text()
        self.assertIn("receipt manual", checklist)
        texts = sorted(out.glob("*.txt"))
        self.assertEqual(len(texts), 2)
        self.assertIn("=== REQUEST TEXT", texts[0].read_text())
        self.assertTrue(all(p.read_bytes().startswith(b"%PDF-1.4") for p in out.glob("*.pdf")))
        self.assertEqual(oct(out.stat().st_mode & 0o777), "0o700")
        self.assertEqual({self.ledger.get(r)["state"] for r in self.ids}, {"submitting"})
        report = health_report(self.ledger, NOW)
        self.assertEqual((report["awaiting_manual_filing"], report["stuck_attempts"]), (2, 0))
        again = send_batch(self.ledger, auth_id=self.auth_id, env=env, profile=self.profile, template=self.template,
                           confirm=dry["confirmation_phrase"], manual_dir=out)
        self.assertEqual(again["status"], "nothing_pending")
        attempts = [r["attempt_id"] for r in self.ledger.connection.execute("SELECT attempt_id FROM outbox")]
        evidence = self.tmp / "confirmation.pdf"
        evidence.write_bytes(b"portal confirmation page")
        with self.assertRaises(ManualFilingError):
            record_manual_filing(self.ledger, attempts[0], "owner", confirmation="X")
        done = record_manual_filing(self.ledger, attempts[0], "owner", confirmation="PORTAL-1",
                                    evidence_path=evidence, tracking_number="2026-FOIA-0001")
        self.assertEqual((done["status"], done["acknowledgment"]), ("submitted", "recorded"))
        with self.assertRaisesRegex(ManualFilingError, "already recorded"):
            record_manual_filing(self.ledger, attempts[0], "owner", confirmation="PORTAL-1", evidence_path=evidence)
        failed = record_manual_filing(self.ledger, attempts[1], "owner", failed=True, reason="portal down")
        self.assertEqual(failed["status"], "delivery_failed")
        states = sorted(self.ledger.get(r)["state"] for r in self.ids)
        self.assertEqual(states, ["acknowledged", "delivery_failed"])

    def test_smtp_cannot_take_portal_routes(self):
        with self.assertRaisesRegex(Exception, "transport must be one of"):
            authorize_batch(self.ledger, request_ids=self.ids, authorizer="owner", reference="X",
                            expires_at="2026-11-30T00:00:00Z", profile=self.profile, template=self.template,
                            transport="carrier-pigeon", max_messages=2, now=NOW)


class TemplateApprovalTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.dir = Path(self.folder.name) / "templates"
        shutil.copytree(TEMPLATE_DIR, self.dir)

    def tearDown(self):
        self.folder.cleanup()

    def test_approval_writes_next_locked_revision(self):
        base = load_active_template(directory=self.dir)
        with self.assertRaisesRegex(TemplateError, "confirmation must be exactly"):
            approve_template(directory=self.dir, reviewer_role="counsel", reviewed_on="2026-11-16", scope="all",
                             confirm="yes")
        with self.assertRaisesRegex(TemplateError, "role label"):
            approve_template(directory=self.dir, reviewer_role="a@b.example", reviewed_on="2026-11-16", scope="all",
                             confirm=approval_phrase(base))
        approved = approve_template(directory=self.dir, reviewer_role="counsel", reviewed_on="2026-11-16",
                                    scope="all", confirm=approval_phrase(base))
        self.assertEqual((approved.revision, approved.status), ("2", "approved"))
        self.assertEqual(load_active_template(directory=self.dir).key, "annual_foia_log_request@2")
        self.assertEqual(approved.body, base.body)
        self.assertEqual(blockers({}, profile=None, template=approved)[:1], ["FOIA_SENDING_ENABLED is not set to 'true'"])
        self.assertFalse(any("not approved" in b for b in blockers({}, profile=None, template=approved)))
        # Editing the approved file without a new revision breaks the lock.
        path = self.dir / "annual_foia_log_request.v2.json"
        path.write_text(path.read_text().replace("existing FOIA request log", "FOIA request log"))
        with self.assertRaisesRegex(TemplateError, "LOCK.json mismatch"):
            load_template(revision="2", directory=self.dir)

    def test_repository_templates_are_consistent(self):
        draft = load_template(directory=TEMPLATE_DIR, revision="1")
        self.assertNotEqual(draft.status, "approved")
        self.assertTrue(any("not approved" in b for b in blockers({}, profile=None, template=draft)))
        active = load_active_template(directory=TEMPLATE_DIR)
        if active.status == "approved":  # an owner-approved revision must carry its approval record
            self.assertTrue(active.approval)


class PdfTests(unittest.TestCase):
    def test_pdf_structure_and_xref_offsets(self):
        data = text_pdf("Line (one) \\ back\n" + "word " * 2000 + "\nunicode ☃", title="T")
        self.assertTrue(data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF"))
        xref = int(data.rsplit(b"startxref\n", 1)[1].split(b"\n")[0])
        self.assertTrue(data[xref:].startswith(b"xref"))
        entries = data[xref:].split(b"\n")[3:]
        for line in entries:
            if not line.endswith(b" n "):
                break
            offset = int(line[:10])
            self.assertRegex(data[offset:offset + 12].decode("latin-1"), r"^\d+ 0 obj")
        self.assertGreater(data.count(b"/Type /Page "), 1)


class CliSendingTests(unittest.TestCase):
    def test_cli_send_is_dry_run_without_confirmation_and_blocked_without_flag(self):
        with tempfile.TemporaryDirectory() as folder:
            tmp = Path(folder)
            templates = approved_copy(tmp)
            profile_path = tmp / "requester.json"
            data = copy.deepcopy(PROFILE)
            data.update(profile_id="test-live-1", synthetic=False, verified=True, name="Test Requester",
                        reply_email="replies@sender.example")
            profile_path.write_text(json.dumps(data))
            env = {k: v for k, v in os.environ.items() if not k.startswith("FOIA_")}
            env.update(FOIA_TEMPLATE_DIR=templates.source_dir)

            def cli(*args, extra=None):
                return subprocess.run([sys.executable, "-m", "foia_requests", *args], cwd=ROOT, text=True,
                                      capture_output=True, env=dict(env, **(extra or {})))

            ledger = str(tmp / "state" / "ledger.sqlite")
            draft = cli("draft", "--ledger", ledger, "--directory-source", "fixtures/directory", "--coverage",
                        "fixtures/aggregator_export", "--profile", str(profile_path), "--component", "fed:doj/oip",
                        "--calendar-year", "2021", "--out", str(tmp / "packets"), "--as-of", NOW)
            self.assertEqual(draft.returncode, 0, draft.stderr)
            packet = json.loads(draft.stdout)["packet"]
            self.assertEqual(cli("review", "--ledger", ledger, "--packet", packet, "--actor", "owner", "--note",
                                 "ok").returncode, 0)
            self.assertEqual(cli("operator", "--ledger", ledger, "--id", "owner", "--role", "all").returncode, 0)
            approve = cli("approve", "batch", "--ledger", ledger, "--operator", "owner", "--profile",
                          str(profile_path), "--transport", "smtp", "--reference", "T-1", "--packet", packet)
            self.assertEqual(approve.returncode, 0, approve.stderr)
            auth_id = json.loads(approve.stdout)["auth_id"]
            queued = cli("queue", "--ledger", ledger, "--operator", "owner", "--auth-id", auth_id)
            self.assertEqual(json.loads(queued.stdout)["confirmation_phrase"], f"SEND 1 {auth_id}")
            dry = cli("send", "--ledger", ledger, "--auth-id", auth_id, "--profile", str(profile_path))
            self.assertEqual(dry.returncode, 0, dry.stderr)
            summary = json.loads(dry.stdout)
            self.assertEqual((summary["mode"], summary["sent"]), ("dry_run", 0))
            self.assertIn("FOIA_SENDING_ENABLED is not set to 'true'", summary["blockers"])
            blocked = cli("send", "--ledger", ledger, "--auth-id", auth_id, "--profile", str(profile_path),
                          "--confirm", f"SEND 1 {auth_id}", extra={"FOIA_TRANSPORT_PROVIDER": "smtp"})
            self.assertEqual(blocked.returncode, 3)
            self.assertEqual(json.loads(blocked.stdout)["sent"], 0)
            certs = CertFolder()
            try:
                with FakeSmtpServer(certs.cert, certs.key) as server:
                    live = cli("send", "--ledger", ledger, "--auth-id", auth_id, "--profile", str(profile_path),
                               "--confirm", f"SEND 1 {auth_id}", "--min-interval-seconds", "0",
                               extra=smtp_env(server, certs.cert))
                    self.assertEqual(live.returncode, 0, live.stderr + live.stdout)
                    self.assertEqual(json.loads(live.stdout)["sent"], 1)
                    self.assertEqual(len(server.messages), 1)
                    self.assertTrue(server.messages[0]["to"][0].endswith(".gov.example"))
                    repeat = cli("send", "--ledger", ledger, "--auth-id", auth_id, "--profile", str(profile_path),
                                 "--confirm", f"SEND 1 {auth_id}", extra=smtp_env(server, certs.cert))
                    self.assertEqual(json.loads(repeat.stdout)["status"], "nothing_pending")
                    self.assertEqual(len(server.messages), 1)
            finally:
                certs.cleanup()
            request_id = json.loads(dry.stdout)["items"][0]["request_id"]
            evidence = tmp / "ack.eml"
            evidence.write_text("Your request was received. Tracking 2026-0001.")
            ack = cli("receipt", "ack", "--ledger", ledger, "--request", request_id, "--operator", "owner",
                      "--tracking-number", "2026-0001", "--evidence", str(evidence))
            self.assertEqual(json.loads(ack.stdout)["status"], "recorded", ack.stderr)
            stop = cli("stop", "--ledger", ledger, "--operator", "owner", "--reason", "test")
            self.assertTrue(json.loads(stop.stdout)["dispatch_stopped"])
            resume = cli("resume", "--ledger", ledger, "--operator", "owner", "--reason", "test")
            self.assertFalse(json.loads(resume.stdout)["dispatch_stopped"])
            self.assertNotEqual(cli("dispatch").returncode, 0)


class RepositoryHygieneTests(unittest.TestCase):
    def test_no_real_identity_or_credentials_committed(self):
        tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True)
        if tracked.returncode != 0:
            self.skipTest("not a git checkout")
        for name in tracked.stdout.splitlines():
            path = ROOT / name
            if path.suffix == ".json" and path.is_file() and path.stat().st_size < 1_000_000:
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except ValueError:
                    continue
                if isinstance(data, dict) and {"requester_principal", "synthetic"} <= set(data):
                    self.assertIs(data["synthetic"], True, f"{name} looks like a real requester profile")
            if path.is_file() and path.suffix in (".env", ".pem", ".key"):
                self.fail(f"{name} must not be committed")
            if path.is_file() and path.suffix in (".py", ".yml", ".yaml", ".md", ".json", ".toml", ".cfg"):
                text = path.read_text(encoding="utf-8", errors="replace")
                for line in text.splitlines():
                    stripped = line.strip()
                    if stripped.startswith(("FOIA_SMTP_PASSWORD=", "export FOIA_SMTP_PASSWORD=")):
                        value = stripped.split("=", 1)[1].strip().strip("'\"")
                        self.assertTrue(value == "" or value.startswith(("<", "$")),
                                        f"{name} appears to contain an SMTP password")


if __name__ == "__main__":
    unittest.main()
