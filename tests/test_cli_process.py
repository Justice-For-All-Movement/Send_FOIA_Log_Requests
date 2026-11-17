import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CliProcessTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "foia_requests", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_fixture_and_help(self):
        help_result = self.run_cli("--help")
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("dispatch", help_result.stdout)

        campaign = self.run_cli("--config", "fixtures/offline-config.json", "campaign")
        self.assertEqual(campaign.returncode, 0)
        self.assertEqual(json.loads(campaign.stdout), {
            "command": "campaign", "mode": "dry_run", "status": "not_implemented"
        })

    def test_fixture_dry_run_end_to_end_sends_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = str(Path(directory) / "state" / "ledger.sqlite")
            out = str(Path(directory) / "packets")
            draft = self.run_cli(
                "--config", "fixtures/offline-config.json", "draft", "--ledger", ledger,
                "--directory-source", "fixtures/directory", "--coverage", "fixtures/aggregator_export",
                "--profile", "fixtures/profiles/synthetic-requester.json",
                "--select", "fed:doj/oip:calendar:2024", "--select", "fed:doj/oip:calendar:2023",
                "--out", out, "--as-of", "2026-11-15T00:00:00Z")
            self.assertEqual(draft.returncode, 0, draft.stderr)
            summary = json.loads(draft.stdout)
            self.assertEqual(summary["outbound_messages_sent"], 0)
            self.assertEqual(summary["decisions"], ["draft", "suppressed_public_coverage"])
            self.assertFalse(summary["ext_corpus_satisfied"])
            too_many = self.run_cli(
                "--config", "fixtures/offline-config.json", "draft", "--ledger", ledger,
                "--coverage", "fixtures/aggregator_export", "--profile", "fixtures/profiles/synthetic-requester.json",
                "--select", "fed:doj/oip:calendar:2020", "--select", "fed:doj/oip:calendar:2021",
                "--select", "fed:doj/oip:calendar:2022", "--out", out)
            self.assertEqual(too_many.returncode, 2)
            self.assertIn("exceeds dry-run cap 2", too_many.stderr)
            review = self.run_cli("review", "--ledger", ledger, "--packet", summary["packet"],
                                  "--actor", "operator:test", "--note", "fixture review")
            self.assertEqual(review.returncode, 0, review.stderr)
            self.assertFalse(json.loads(review.stdout)["dispatch_authorized"])

    def test_draft_with_custom_range_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            base = ["--config", "fixtures/offline-config.json", "draft", "--ledger",
                    str(Path(directory) / "state" / "ledger.sqlite"), "--directory-source", "fixtures/directory",
                    "--coverage", "fixtures/aggregator_export", "--profile",
                    "fixtures/profiles/synthetic-requester.json", "--out", str(Path(directory) / "packets"),
                    "--as-of", "2026-11-16T00:00:00Z", "--today", "2026-11-16"]
            week = self.run_cli(*base, "--component", "fed:doj/oip", "--last-days", "7")
            self.assertEqual(week.returncode, 0, week.stderr)
            summary = json.loads(week.stdout)
            self.assertEqual((summary["decisions"], summary["outbound_messages_sent"]), (["draft"], 0))
            self.assertEqual(len(summary["period_warnings"]), 1)
            packet = json.loads(Path(summary["packet"]).read_text())
            self.assertEqual(packet["entries"][0]["period"], ["2026-11-09", "2026-11-15", "custom"])
            future = self.run_cli(*base, "--component", "fed:doj/oip", "--start", "2026-11-01", "--end", "2026-12-01")
            self.assertEqual(future.returncode, 2)
            self.assertIn("future", future.stderr)
            inverted = self.run_cli(*base, "--component", "fed:doj/oip", "--start", "2026-02-01",
                                    "--end", "2026-01-01")
            self.assertEqual(inverted.returncode, 2)
            self.assertIn("inverted", inverted.stderr)
            orphan = self.run_cli(*base, "--last-days", "7")
            self.assertEqual(orphan.returncode, 2)
            self.assertIn("--component", orphan.stderr)

    def test_prepare_one_command_packet(self):
        with tempfile.TemporaryDirectory() as directory:
            base = ["--config", "fixtures/offline-config.json", "prepare", "--ledger",
                    str(Path(directory) / "state" / "ledger.sqlite"), "--directory-source", "fixtures/directory",
                    "--coverage", "fixtures/aggregator_export", "--profile",
                    "fixtures/profiles/synthetic-requester.json", "--today", "2026-11-16",
                    "--as-of", "2026-11-16T00:00:00Z"]
            result = self.run_cli(*base, "--last-days", "7", "--out", str(Path(directory) / "week"))
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads(result.stdout)
            self.assertEqual((summary["drafts"], summary["excluded"], summary["outbound_messages_sent"]), (4, 3, 0))
            self.assertIn("November 15, 2026", Path(summary["packet"]).read_text(encoding="utf-8"))
            doj = self.run_cli(*base, "--agency", "doj", "--start", "2025-01-01", "--end", "2025-03-31",
                               "--out", str(Path(directory) / "q1"))
            self.assertEqual(doj.returncode, 0, doj.stderr)
            self.assertEqual(json.loads(doj.stdout)["drafts"], 2)
            future = self.run_cli(*base, "--start", "2026-11-01", "--end", "2026-11-30",
                                  "--out", str(Path(directory) / "x"))
            self.assertEqual(future.returncode, 2)
            self.assertIn("future", future.stderr)
            self.assertFalse((Path(directory) / "x").exists())

    def test_plan_logs_and_followups_send_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = str(Path(directory) / "state" / "ledger.sqlite")
            profile = "fixtures/profiles/synthetic-journalist.json"
            plan = self.run_cli("--config", "fixtures/offline-config.json", "plan-logs", "--ledger", ledger,
                                "--directory-source", "fixtures/directory", "--profile", profile,
                                "--published-logs", "fixtures/published_logs.synthetic.json",
                                "--today", "2026-11-17", "--as-of", "2026-11-17T00:00:00Z",
                                "--out", str(Path(directory) / "plan"))
            self.assertEqual(plan.returncode, 0, plan.stderr)
            summary = json.loads(plan.stdout)
            self.assertEqual((summary["drafts"], summary["outbound_messages_sent"]), (2, 0))
            self.assertEqual(summary["decisions"]["fed:epa/region-x"], "excluded_publishes_logs")
            self.assertIn("closed between", Path(summary["plan"]).read_text(encoding="utf-8"))
            again = self.run_cli("plan-logs", "--ledger", ledger, "--profile", profile, "--today", "2026-11-17",
                                 "--published-logs", "fixtures/published_logs.synthetic.json",
                                 "--out", str(Path(directory) / "plan2"))
            self.assertEqual(json.loads(again.stdout)["drafts"], 0)  # next periods have not ended
            bad = self.run_cli("plan-logs", "--ledger", ledger, "--profile", profile, "--cadence", "hourly",
                               "--out", str(Path(directory) / "x"))
            self.assertEqual(bad.returncode, 2)
            follow = self.run_cli("--config", "fixtures/offline-config.json", "plan-followups", "--ledger", ledger,
                                  "--gaps", "fixtures/release_gaps", "--profile", profile, "--group",
                                  "--as-of", "2026-11-17T00:00:00Z", "--out", str(Path(directory) / "fu"))
            self.assertEqual(follow.returncode, 0, follow.stderr)
            summary = json.loads(follow.stdout)
            self.assertEqual((summary["letters"], summary["records_requested"], summary["outbound_messages_sent"]),
                             (3, 4, 0))

    def test_invalid_config_fails_before_command_and_dispatch_denied(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            config.write_text('{"transport_enabled":true}', encoding="utf8")
            result = self.run_cli("--config", str(config), "draft")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")
            self.assertIn("disabled", result.stderr.lower())

        dispatch = self.run_cli("dispatch")
        self.assertNotEqual(dispatch.returncode, 0)
        self.assertEqual(dispatch.stdout, "")
        self.assertIn("disabled", dispatch.stderr.lower())
