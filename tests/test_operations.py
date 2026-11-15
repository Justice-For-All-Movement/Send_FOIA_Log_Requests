import json
import subprocess
import sys
import tempfile
import unittest
from collections import namedtuple
from pathlib import Path

from foia_requests.authorization import set_stop
from foia_requests.config import Config
from foia_requests.dispatch import RateLimits, run_once
from foia_requests.fake_transport import FakeSender
from foia_requests.ledger import Ledger, LedgerError
from foia_requests.operations import full_digest, health_report, isolated_restore_check, verified_backup
from tests.test_dispatch import NOW, prepare

ROOT = Path(__file__).resolve().parents[1]
LATER = "2026-11-15T03:00:00Z"


class OperationsTests(unittest.TestCase):
    def setUp(self):
        prepare(self)
        sender = FakeSender(["accepted", "timeout"])
        for _ in range(2):
            run_once(self.ledger, sender, Config(), owner="w", now=NOW,
                     limits=RateLimits(per_destination_per_hour=10, global_per_hour=10))
        self.backup = Path(self.folder.name) / "backups" / "ledger.sqlite"

    def tearDown(self):
        self.ledger.close()
        self.folder.cleanup()

    def test_backup_and_isolated_restore_never_resends(self):
        manifest = verified_backup(self.ledger, self.backup)
        self.assertEqual(manifest["full_digest"], full_digest(self.ledger.connection))
        self.assertEqual(Path(str(self.backup) + ".manifest.json").stat().st_mode & 0o777, 0o600)
        report = isolated_restore_check(self.backup, manifest, self.folder.name, "2026-11-16T00:00:00Z")
        self.assertTrue(report["passed"], report)
        self.assertEqual(report["protected_attempts"], 2)  # submitted + unknown
        self.assertEqual(report["claimable_after_restore"], 1)  # the never-attempted queued one only
        self.assertEqual(report["would_resend"], 0)

    def test_low_disk_refuses_backup(self):
        usage = namedtuple("usage", "total used free")
        with self.assertRaisesRegex(LedgerError, "insufficient free space"):
            verified_backup(self.ledger, self.backup, disk_usage=lambda _: usage(1, 1, 10))
        self.assertFalse(self.backup.exists())

    def test_health_states(self):
        report = health_report(self.ledger, LATER)
        self.assertEqual(report["status"], "degraded")
        self.assertIn("attempts stuck in submitting/unknown need reconciliation", report["problems"])
        self.assertIn("no recent verified backup", report["problems"])
        set_stop(self.ledger, "safety", "drill")
        self.assertTrue(health_report(self.ledger, LATER)["dispatch_stopped"])
        with tempfile.TemporaryDirectory() as other:
            empty = Ledger(Path(other) / "l" / "ledger.sqlite")
            try:
                self.assertEqual(health_report(empty, LATER)["status"], "not_configured")
            finally:
                empty.close()

    def test_cli_status(self):
        self.ledger.close()
        result = subprocess.run([sys.executable, "-m", "foia_requests", "status", "--ledger", str(self.ledger.path)],
                                cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.ledger = Ledger(self.ledger.path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "degraded")


if __name__ == "__main__":
    unittest.main()
