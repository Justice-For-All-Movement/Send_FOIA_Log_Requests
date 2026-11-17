"""foia-requests command line.

Preparation (never sends): ``directory``, ``draft``, ``prepare``, ``plan-logs`` (periodic closed-request log
drafts), ``plan-followups`` (released-records follow-up drafts), ``review``, ``status``.
Owner-gated sending path: ``operator`` (grant roles), ``approve template``,
``approve batch``, ``queue``, ``send`` (dry run unless every gate in
``sending.py`` holds and the typed confirmation matches), ``stop``/``resume``
(emergency stop), ``receipt manual|ack|reconcile``.
``dispatch`` is retired and always fails closed; use ``send``.
"""
import argparse
import json
import sys
from datetime import datetime, timezone

from .config import Config, ConfigurationError

COMMANDS = ("directory", "campaign", "draft", "prepare", "plan-logs", "plan-followups", "review", "operator", "approve", "queue", "send", "stop",
            "resume", "dispatch", "receipt", "response", "continue", "export", "status")
IMPLEMENTED = ("directory", "draft", "prepare", "plan-logs", "plan-followups", "review", "status", "operator", "approve", "queue", "send", "stop",
               "resume", "receipt")
EXIT_BLOCKED = 3


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


def _add_period_args(command):
    group = command.add_argument_group("period (choose one; custom ranges are validated against --today)")
    group.add_argument("--start", help="Custom range start, YYYY-MM-DD (with --end)")
    group.add_argument("--end", help="Custom range end, YYYY-MM-DD, inclusive; must not be after today")
    group.add_argument("--last-days", type=int, help="The N complete days ending yesterday (7 = the last week)")
    group.add_argument("--calendar-year", type=int, help="Calendar year YYYY")
    group.add_argument("--fiscal-year", type=int, help="US federal fiscal year YYYY (Oct 1 - Sep 30)")
    command.add_argument("--today", help="Reference date YYYY-MM-DD for validation (default: today, UTC)")


def _period_args_given(args):
    return any(getattr(args, k) is not None for k in ("start", "end", "last_days", "calendar_year", "fiscal_year"))


def _selected_period(args):
    from datetime import date
    from .periods import select_period
    today = date.fromisoformat(args.today) if args.today else datetime.now(timezone.utc).date()
    return select_period(start=args.start, end=args.end, last=args.last_days, calendar=args.calendar_year,
                         fiscal=args.fiscal_year, today=today)


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
            command.add_argument("--select", action="append", type=_period,
                                 help="COMPONENT:calendar:YYYY | COMPONENT:fiscal:YYYY | COMPONENT:custom:START:END")
            command.add_argument("--component", action="append",
                                 help="Component ID for the period chosen with --start/--end, --last-days, ...")
            _add_period_args(command)
            command.add_argument("--out", required=True, help="Private packet output folder")
            command.add_argument("--as-of", help="UTC timestamp recorded in the packet (default: now)")
        elif name == "prepare":
            command.add_argument("--ledger", required=True, help="Private ledger database path")
            command.add_argument("--directory-source", help="Discover and persist this fixture directory first")
            command.add_argument("--coverage", required=True, help="Aggregator log_coverage export folder")
            command.add_argument("--profile", required=True, help="Requester profile JSON")
            command.add_argument("--agency", action="append", help="Agency abbreviation filter, e.g. doj (repeatable)")
            command.add_argument("--component", action="append", help="Component ID filter (repeatable)")
            command.add_argument("--out", required=True, help="Private output folder for review-packet.md")
            command.add_argument("--as-of", help="UTC timestamp recorded in the packet (default: now)")
            _add_period_args(command)
        elif name == "plan-logs":
            command.add_argument("--ledger", required=True, help="Private ledger database path")
            command.add_argument("--directory-source", help="Discover and persist this fixture directory first")
            command.add_argument("--profile", required=True, help="Requester profile JSON")
            command.add_argument("--cadence", choices=("monthly", "quarterly"),
                                 help="Cadence for this run (default: tier 1 monthly, tiers 2-3 quarterly)")
            command.add_argument("--cadence-overrides", help="JSON list of {agency_abbr, component_abbr|null, cadence}")
            command.add_argument("--published-logs",
                                 help="JSON list of {agency_abbr, component_abbr|null, index_url, cadence}; skipped")
            command.add_argument("--priority", help="Priority list (default: packaged data/priority.json)")
            command.add_argument("--agency", action="append", help="Agency abbreviation filter (repeatable)")
            command.add_argument("--component", action="append", help="Component ID filter (repeatable)")
            command.add_argument("--since", help="First period start for components with no prior request, "
                                                 "YYYY-MM-DD (default: only the latest ended period)")
            command.add_argument("--max", type=int, help="Draft at most N requests in this run (default: no cap)")
            command.add_argument("--no-record", action="store_true",
                                 help="Do not store drafts in the ledger (a later run plans the same periods)")
            command.add_argument("--today", help="Reference date YYYY-MM-DD (default: today, UTC)")
            command.add_argument("--out", required=True, help="Private output folder for plan.md/plan.json")
            command.add_argument("--as-of", help="UTC timestamp recorded in the plan (default: now)")
        elif name == "plan-followups":
            command.add_argument("--ledger", required=True, help="Private ledger database path")
            command.add_argument("--directory-source", help="Discover and persist this fixture directory first")
            command.add_argument("--gaps", required=True, help="foia-release-gaps/1 export folder")
            command.add_argument("--profile", required=True, help="Requester profile JSON")
            command.add_argument("--agency", action="append", help="agency_slug filter (repeatable)")
            command.add_argument("--max", type=int, help="Draft at most N letters in this run")
            command.add_argument("--group", action="store_true",
                                 help="One letter per agency component (at most 25 tracking numbers per letter)")
            command.add_argument("--no-record", action="store_true", help="Do not record drafts in the ledger")
            command.add_argument("--out", required=True, help="Private output folder for followups.md/json")
            command.add_argument("--as-of", help="UTC timestamp recorded in the output (default: now)")
        elif name == "status":
            command.add_argument("--ledger", required=True)
            command.add_argument("--backup-manifest", help="Manifest written by a verified backup")
        elif name == "review":
            command.add_argument("--ledger", required=True)
            command.add_argument("--packet", required=True)
            command.add_argument("--actor", required=True)
            command.add_argument("--note", required=True)
        elif name == "operator":
            command.add_argument("--ledger", required=True)
            command.add_argument("--id", required=True, help="Operator label, e.g. owner")
            command.add_argument("--role", required=True, choices=("reviewer", "authorizer", "dispatcher", "safety",
                                                                   "all"))
            command.add_argument("--by", default="owner", help="Who granted the role (recorded)")
        elif name == "approve":
            kinds = command.add_subparsers(dest="approve_kind", required=True)
            tpl = kinds.add_parser("template", help="Record legal approval as a new locked template revision")
            tpl.add_argument("--template-dir", help="Template folder (default: FOIA_TEMPLATE_DIR or packaged)")
            tpl.add_argument("--reviewer-role", help="Role label of the legal reviewer, e.g. \"owner's counsel\"")
            tpl.add_argument("--reviewed-on", help="Review date YYYY-MM-DD")
            tpl.add_argument("--scope", default="5 U.S.C. 552 citation, log wording, privacy and fee paragraphs",
                             help="What the review covered")
            tpl.add_argument("--source", help="Optional JSON with reviewer-edited subject/body/requested_fields")
            tpl.add_argument("--confirm", help="Exact phrase printed when run without --confirm")
            batch = kinds.add_parser("batch", help="Authorize an exact reviewed batch (does not send)")
            batch.add_argument("--ledger", required=True)
            batch.add_argument("--operator", required=True, help="Operator with the authorizer role")
            batch.add_argument("--profile", required=True, help="Private requester profile JSON")
            batch.add_argument("--transport", required=True, choices=("smtp", "manual"))
            batch.add_argument("--reference", required=True, help="Your approval reference, e.g. OWNER-2026-11-16-1")
            batch.add_argument("--packet", help="Reviewed packet.json from `draft`; authorizes all its drafts")
            batch.add_argument("--request", action="append", help="Request ID (repeatable) instead of --packet")
            batch.add_argument("--expires-in-days", type=int, default=7, help="Authorization lifetime (1..30)")
            batch.add_argument("--max-messages", type=int, help="Cap (default: number of requests)")
        elif name == "queue":
            command.add_argument("--ledger", required=True)
            command.add_argument("--operator", required=True, help="Operator with the dispatcher role")
            command.add_argument("--auth-id", required=True)
        elif name == "send":
            command.add_argument("--ledger", required=True)
            command.add_argument("--auth-id", required=True)
            command.add_argument("--profile", required=True, help="Private requester profile JSON")
            command.add_argument("--operator", default="owner", help="Recorded as the sending worker")
            command.add_argument("--confirm", help="Typed per-batch phrase 'SEND <n> <auth-id>'; omit for a dry run")
            command.add_argument("--max", type=int, help="Send at most this many in this run")
            command.add_argument("--per-host-per-hour", type=int, default=2)
            command.add_argument("--global-per-hour", type=int, default=10)
            command.add_argument("--min-interval-seconds", type=float, default=30.0)
            command.add_argument("--manual-out", help="Private folder for manual-filing copies (manual transport)")
        elif name in ("stop", "resume"):
            command.add_argument("--ledger", required=True)
            command.add_argument("--operator", required=True, help="Operator with the safety role")
            command.add_argument("--reason", required=True)
        elif name == "receipt":
            kinds = command.add_subparsers(dest="receipt_kind", required=True)
            manual = kinds.add_parser("manual", help="Record the outcome of a manual (portal) filing")
            manual.add_argument("--ledger", required=True)
            manual.add_argument("--attempt", required=True)
            manual.add_argument("--operator", required=True)
            manual.add_argument("--confirmation", help="Portal confirmation number")
            manual.add_argument("--evidence", help="Saved confirmation page/e-mail (hashed into the ledger)")
            manual.add_argument("--tracking-number", help="Agency tracking number, if the portal issued one")
            manual.add_argument("--failed", action="store_true")
            manual.add_argument("--reason")
            ack = kinds.add_parser("ack", help="Record an agency acknowledgment with its tracking number")
            ack.add_argument("--ledger", required=True)
            ack.add_argument("--request", required=True)
            ack.add_argument("--operator", required=True)
            ack.add_argument("--tracking-number", required=True)
            ack.add_argument("--evidence", required=True, help="Saved acknowledgment e-mail/letter")
            ack.add_argument("--source", default="agency_email")
            rec = kinds.add_parser("reconcile", help="Resolve an unknown send outcome from evidence")
            rec.add_argument("--ledger", required=True)
            rec.add_argument("--attempt", required=True)
            rec.add_argument("--operator", required=True)
            rec.add_argument("--outcome", required=True, choices=("submitted", "delivery_failed"))
            rec.add_argument("--source", required=True, help="e.g. provider_sent_folder, agency_receipt")
            rec.add_argument("--reference", required=True, help="Where the evidence is, e.g. a Message-ID")
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
        print("dispatch is disabled (retired; it always fails closed); use `foia-requests send`, a dry run by default",
              file=sys.stderr)
        return 2
    if args.command is None:
        parser.print_help()
        return 0
    if args.command not in IMPLEMENTED:
        print(json.dumps({"command": args.command, "mode": cfg.mode, "status": "not_implemented"}))
        return 0
    from .sending import SendingBlocked
    try:
        result = _run(args, cfg)
    except SendingBlocked as exc:
        print(json.dumps({"command": args.command, "status": "blocked", "blockers": exc.blockers, "sent": 0},
                         sort_keys=True, indent=2))
        return EXIT_BLOCKED
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"{args.command} failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


def _run(args, cfg):
    from .ledger import Ledger
    from . import directory
    if args.command == "approve" and args.approve_kind == "template":
        return _approve_template(args)
    with Ledger(args.ledger) as ledger:
        if args.command in ("operator", "approve", "queue", "send", "stop", "resume", "receipt"):
            return _sending_command(args, ledger)
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
            from .templates import RequesterProfile, load_active_template
            if args.directory_source:
                found = directory.discover(directory.load_source(args.directory_source),
                                           directory.fixture_fetcher(args.directory_source))
                directory.persist(ledger, found)
            as_of = args.as_of or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
                "+00:00", "Z")
            selection, warnings = list(args.select or []), []
            if args.component or _period_args_given(args):
                if not args.component or not _period_args_given(args):
                    raise ValueError("--component requires a period option, and a period option requires --component")
                period, warnings = _selected_period(args)
                selection += [(c, period) for c in args.component]
            if not selection:
                raise ValueError("select component-periods with --select or --component plus a period option")
            packet, folder = run_dry_run(
                ledger, cfg, selection=selection, export=load_export(args.coverage),
                profile=RequesterProfile.from_file(args.profile), template=load_active_template(),
                output_dir=args.out, as_of=as_of)
            return {"command": "draft", "mode": cfg.mode, "status": "dry_run_packet_written",
                    "packet": str(folder / "packet.json"), "packet_sha256": packet["packet_sha256"],
                    "outbound_messages_sent": packet["outbound_messages_sent"],
                    "decisions": [e["decision"] for e in packet["entries"]], "period_warnings": warnings,
                    "ext_corpus_satisfied": packet["corpus_gate"]["ext_corpus_satisfied"]}
        if args.command == "prepare":
            from .aggregator_contract import load_export
            from .prepare import prepare_packet
            from .templates import RequesterProfile, load_active_template
            from datetime import date
            if args.directory_source:
                found = directory.discover(directory.load_source(args.directory_source),
                                           directory.fixture_fetcher(args.directory_source))
                directory.persist(ledger, found)
            period, warnings = _selected_period(args)
            today = date.fromisoformat(args.today) if args.today else datetime.now(timezone.utc).date()
            as_of = args.as_of or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
                "+00:00", "Z")
            manifest, folder = prepare_packet(
                ledger, cfg, period=period, warnings=warnings, export=load_export(args.coverage),
                profile=RequesterProfile.from_file(args.profile), template=load_active_template(), output_dir=args.out,
                as_of=as_of, today=today, agencies=args.agency, components=args.component)
            entries = manifest["entries"]
            return {"command": "prepare", "mode": cfg.mode, "status": "review_packet_written",
                    "packet": str(folder / "review-packet.md"), "manifest": str(folder / "prepare-manifest.json"),
                    "manifest_sha256": manifest["manifest_sha256"],
                    "period": [manifest["period"]["start"], manifest["period"]["end"]],
                    "period_warnings": warnings, "drafts": sum(e["included"] for e in entries),
                    "excluded": sum(not e["included"] for e in entries),
                    "outbound_messages_sent": 0, "live_dispatch_authorized": False}
        if args.command in ("plan-logs", "plan-followups"):
            return _plan_command(args, cfg, ledger)
        if args.command == "status":
            from .operations import health_report
            manifest = json.loads(open(args.backup_manifest, encoding="utf-8").read()) if args.backup_manifest else None
            now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            return health_report(ledger, now, backup_manifest=manifest)
        from .dryrun import record_review
        reviewed = record_review(ledger, args.packet, args.actor, args.note)
        return {"command": "review", "reviewed_requests": reviewed, "dispatch_authorized": False}



def _plan_command(args, cfg, ledger):
    from . import directory
    from .templates import RequesterProfile, load_active_template
    from datetime import date
    if args.directory_source:
        found = directory.discover(directory.load_source(args.directory_source),
                                   directory.fixture_fetcher(args.directory_source))
        directory.persist(ledger, found)
    as_of = args.as_of or _now()
    profile = RequesterProfile.from_file(args.profile)
    if args.command == "plan-logs":
        from . import cadence
        today = date.fromisoformat(args.today) if args.today else datetime.now(timezone.utc).date()
        plan, folder = cadence.plan_logs(
            ledger, cfg, today=today, profile=profile,
            template=load_active_template(cadence.TEMPLATE_ID), output_dir=args.out, as_of=as_of,
            cadence=args.cadence,
            overrides=cadence.load_cadence_overrides(args.cadence_overrides) if args.cadence_overrides else (),
            published=cadence.load_published_logs(args.published_logs) if args.published_logs else (),
            priority=cadence.load_priority(args.priority) if args.priority else None,
            agencies=args.agency, components=args.component, max_drafts=args.max,
            since=date.fromisoformat(args.since) if args.since else None, record=not args.no_record)
        return {"command": "plan-logs", "mode": cfg.mode, "status": "plan_written",
                "plan": str(folder / cadence.PLAN_MD), "plan_json": str(folder / cadence.PLAN_JSON),
                "plan_sha256": plan["plan_sha256"], "drafts": plan["drafts"],
                "ledger_drafts_recorded": plan["ledger_drafts_recorded"],
                "decisions": {e["component_id"]: e["decision"] for e in plan["entries"]},
                "outbound_messages_sent": 0, "live_dispatch_authorized": False}
    from . import release_gaps
    plan, folder = release_gaps.plan_followups(
        ledger, cfg, export=release_gaps.load_gaps(args.gaps), profile=profile,
        template=load_active_template(release_gaps.TEMPLATE_ID), output_dir=args.out, as_of=as_of,
        agencies=args.agency, max_letters=args.max, group=args.group, record=not args.no_record)
    return {"command": "plan-followups", "mode": cfg.mode, "status": "drafts_written",
            "drafts": str(folder / release_gaps.OUT_MD), "drafts_json": str(folder / release_gaps.OUT_JSON),
            "plan_sha256": plan["plan_sha256"], "letters": len(plan["letters"]),
            "records_requested": plan["records_requested"], "skipped": len(plan["skipped"]),
            "outbound_messages_sent": 0, "live_dispatch_authorized": False}


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _approve_template(args):
    from .templates import approval_phrase, approve_template, load_active_template, template_dir
    folder = args.template_dir or template_dir()
    base = load_active_template(directory=folder)
    if not args.confirm:
        return {"command": "approve template", "status": "dry_run_nothing_written", "current": base.key,
                "current_status": base.status, "template_dir": str(folder),
                "confirmation_phrase": approval_phrase(base),
                "next": "re-run with --reviewer-role, --reviewed-on and --confirm \"<phrase>\" after legal review"}
    if not (args.reviewer_role and args.reviewed_on):
        raise ValueError("--reviewer-role and --reviewed-on are required to approve")
    approved = approve_template(directory=folder, reviewer_role=args.reviewer_role, reviewed_on=args.reviewed_on,
                                scope=args.scope, confirm=args.confirm, source=args.source)
    return {"command": "approve template", "status": "approved", "template": approved.key,
            "sha256": approved.sha256, "template_dir": str(folder),
            "next": "re-run `draft` and `review` so requests use the approved revision"}


def _sending_command(args, ledger):
    from . import authorization, receipts, sending
    from .dispatch import RateLimits
    from .templates import RequesterProfile, load_active_template
    import hashlib
    import os
    from datetime import timedelta
    if args.command == "operator":
        roles = authorization.ROLES if args.role == "all" else (args.role,)
        for role in roles:
            authorization.grant_role(ledger, args.id, role, args.by)
        return {"command": "operator", "operator": args.id, "granted": list(roles)}
    if args.command == "approve":
        profile = RequesterProfile.from_file(args.profile)
        template = load_active_template()
        if args.packet:
            packet = json.loads(open(args.packet, encoding="utf-8").read())
            ids = [e["request_id"] for e in packet["entries"] if e.get("decision") == "draft"]
        else:
            ids = list(args.request or [])
        if not ids:
            raise ValueError("give --packet or at least one --request")
        if not 1 <= args.expires_in_days <= 30:
            raise ValueError("--expires-in-days must be 1..30")
        now = ledger.clock()
        expires = (datetime.fromisoformat(now.replace("Z", "+00:00")) + timedelta(days=args.expires_in_days))
        auth_id, digest = authorization.authorize_batch(
            ledger, request_ids=ids, authorizer=args.operator, reference=args.reference,
            expires_at=expires.isoformat().replace("+00:00", "Z"), profile=profile, template=template,
            transport=args.transport, max_messages=args.max_messages or len(ids), now=now)
        return {"command": "approve batch", "auth_id": auth_id, "envelope_sha256": digest, "requests": len(ids),
                "transport": args.transport, "sent": 0,
                "next": f"foia-requests queue --ledger {args.ledger} --operator {args.operator} --auth-id {auth_id}"}
    if args.command == "queue":
        plan = sending.batch_plan(ledger, args.auth_id)
        if plan["transport"] not in sending.LIVE_TRANSPORTS:
            raise ValueError(f"batch transport {plan['transport']} cannot be queued from the CLI")
        queued = authorization.queue_authorized(ledger, args.auth_id, args.operator)
        plan = sending.batch_plan(ledger, args.auth_id)
        return {"command": "queue", "auth_id": args.auth_id, "newly_queued": len(queued), "pending": plan["pending"],
                "sent": 0, "confirmation_phrase": plan["confirmation_phrase"]}
    if args.command == "send":
        profile = RequesterProfile.from_file(args.profile)
        return sending.send_batch(
            ledger, auth_id=args.auth_id, env=os.environ, profile=profile, template=load_active_template(),
            confirm=args.confirm, owner=args.operator, profile_path=args.profile,
            limits=RateLimits(args.per_host_per_hour, args.global_per_hour), max_messages=args.max,
            manual_dir=args.manual_out, ledger_path=args.ledger, min_interval_seconds=args.min_interval_seconds)
    if args.command == "stop":
        authorization.set_stop(ledger, args.operator, args.reason)
        return {"command": "stop", "dispatch_stopped": True}
    if args.command == "resume":
        authorization.clear_stop(ledger, args.operator, args.reason)
        return {"command": "resume", "dispatch_stopped": authorization.stopped(ledger)}
    # receipt
    if args.receipt_kind == "manual":
        from .manual_filing import record_manual_filing
        return dict(record_manual_filing(ledger, args.attempt, args.operator, confirmation=args.confirmation,
                                         evidence_path=args.evidence, failed=args.failed, reason=args.reason,
                                         tracking_number=args.tracking_number), command="receipt manual")
    if args.receipt_kind == "ack":
        evidence = hashlib.sha256(open(args.evidence, "rb").read()).hexdigest()
        result = receipts.record_acknowledgment(ledger, args.request, args.operator,
                                                tracking_number=args.tracking_number, evidence_sha256=evidence,
                                                source=args.source)
        return {"command": "receipt ack", "status": result, "evidence_sha256": evidence}
    outcome = receipts.reconcile_unknown(ledger, args.attempt, args.operator, args.outcome,
                                         {"source": args.source, "reference": args.reference})
    return {"command": "receipt reconcile", "status": outcome}


if __name__ == "__main__":
    raise SystemExit(main())
