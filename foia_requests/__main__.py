"""Offline CLI; all outbound sends are forbidden.

Implemented: ``directory`` (fixture discovery into the ledger), ``draft``
(bounded dry-run review packet), ``review`` (record a verified packet review).
``dispatch`` always fails closed. Other families report not_implemented.
"""
import argparse
import json
import sys
from datetime import datetime, timezone

from .config import Config, ConfigurationError

COMMANDS = ("directory", "campaign", "draft", "review", "dispatch", "receipt", "response", "continue", "export",
            "status")
IMPLEMENTED = ("directory", "draft", "review", "status")


def _period(text):
    from .periods import calendar_year, custom, fiscal_year
    # Component IDs contain ':' (e.g. fed:doj/oip), so parse from the right.
    four = text.rsplit(":", 3)
    three = text.rsplit(":", 2)
    try:
        if len(four) == 4 and four[1] == "custom" and four[0]:
            return four[0], custom(four[2], four[3])
        if len(three) == 3 and three[1] in ("calendar", "fiscal") and three[0]:
            year = int(three[2])
            return three[0], (calendar_year(year) if three[1] == "calendar" else fiscal_year(year))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    raise argparse.ArgumentTypeError("use COMPONENT:calendar:YYYY, COMPONENT:fiscal:YYYY or "
                                     "COMPONENT:custom:YYYY-MM-DD:YYYY-MM-DD")


def build_parser():
    parser = argparse.ArgumentParser(prog="foia-requests")
    parser.add_argument("--config", help="JSON configuration path")
    sub = parser.add_subparsers(dest="command")
    for name in COMMANDS:
        command = sub.add_parser(name)
        if name == "directory":
            group = command.add_mutually_exclusive_group(required=True)
            group.add_argument("--source", help="Directory fixture folder (source.json + pages)")
            group.add_argument("--catalog", help="FC-13 export-catalog snapshot folder (agency/office census)")
            command.add_argument("--live-catalog", action="store_true",
                                 help="Expect a live (non-fixture) FC-13 snapshot")
            command.add_argument("--ledger", required=True, help="Private ledger database path")
        elif name == "draft":
            command.add_argument("--ledger", required=True)
            command.add_argument("--directory-source", help="Discover and persist this fixture directory first")
            command.add_argument("--coverage", required=True, help="Aggregator log_coverage export folder")
            command.add_argument("--profile", required=True, help="Requester profile JSON")
            command.add_argument("--select", required=True, action="append", type=_period,
                                 help="COMPONENT:calendar:YYYY | COMPONENT:fiscal:YYYY | COMPONENT:custom:START:END")
            command.add_argument("--out", required=True, help="Private packet output folder")
            command.add_argument("--as-of", help="UTC timestamp recorded in the packet (default: now)")
        elif name == "status":
            command.add_argument("--ledger", required=True)
            command.add_argument("--backup-manifest", help="Manifest written by a verified backup")
        elif name == "review":
            command.add_argument("--ledger", required=True)
            command.add_argument("--packet", required=True)
            command.add_argument("--actor", required=True)
            command.add_argument("--note", required=True)
    return parser


def main(argv=None):
    parser = build_parser()
    # Validate configuration before any command arguments, so an invalid or
    # unsafe configuration always fails first and without side effects.
    early = argparse.ArgumentParser(add_help=False)
    early.add_argument("--config")
    known, _ = early.parse_known_args(argv)
    try:
        cfg = Config.from_file(known.config) if known.config else Config()
    except ConfigurationError as exc:
        parser.error(str(exc))
    args = parser.parse_args(argv)
    if args.command == "dispatch":
        print("Dispatch disabled; no authorized live transport", file=sys.stderr)
        return 2
    if args.command is None:
        parser.print_help()
        return 0
    if args.command not in IMPLEMENTED:
        print(json.dumps({"command": args.command, "mode": cfg.mode, "status": "not_implemented"}))
        return 0
    try:
        result = _run(args, cfg)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"{args.command} failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


def _run(args, cfg):
    from .ledger import Ledger
    from . import directory
    with Ledger(args.ledger) as ledger:
        if args.command == "directory" and args.catalog:
            from .aggregator_contract import load_catalog_snapshot
            snapshot = load_catalog_snapshot(args.catalog, expected_fixture=not args.live_catalog)
            summary = directory.import_catalog_census(ledger, snapshot)
            return {"command": "directory", "source": "fc13-catalog", "snapshot_id": snapshot.snapshot_id,
                    "created": len(summary["created"]), "matched": len(summary["matched"]),
                    "actions": summary["actions"], "stored": summary["stored"]}
        if args.command == "directory":
            result = directory.discover(directory.load_source(args.source), directory.fixture_fetcher(args.source))
            stored = directory.persist(ledger, result)
            return {"command": "directory", "snapshot_sha256": result.snapshot_sha256, "complete": result.complete,
                    "components": len(result.components), "actions": result.actions, "stored": stored}
        if args.command == "draft":
            from .aggregator_contract import load_export
            from .dryrun import run_dry_run
            from .templates import RequesterProfile, load_template
            if args.directory_source:
                found = directory.discover(directory.load_source(args.directory_source),
                                           directory.fixture_fetcher(args.directory_source))
                directory.persist(ledger, found)
            as_of = args.as_of or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
                "+00:00", "Z")
            packet, folder = run_dry_run(
                ledger, cfg, selection=args.select, export=load_export(args.coverage),
                profile=RequesterProfile.from_file(args.profile), template=load_template(),
                output_dir=args.out, as_of=as_of)
            return {"command": "draft", "mode": cfg.mode, "status": "dry_run_packet_written",
                    "packet": str(folder / "packet.json"), "packet_sha256": packet["packet_sha256"],
                    "outbound_messages_sent": packet["outbound_messages_sent"],
                    "decisions": [e["decision"] for e in packet["entries"]],
                    "ext_corpus_satisfied": packet["corpus_gate"]["ext_corpus_satisfied"]}
        if args.command == "status":
            from .operations import health_report
            manifest = json.loads(open(args.backup_manifest, encoding="utf-8").read()) if args.backup_manifest else None
            now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            return health_report(ledger, now, backup_manifest=manifest)
        from .dryrun import record_review
        reviewed = record_review(ledger, args.packet, args.actor, args.note)
        return {"command": "review", "reviewed_requests": reviewed, "dispatch_authorized": False}


if __name__ == "__main__":
    raise SystemExit(main())
