"""The sending gate and the per-batch send loop.

Sending is possible only when ALL of these hold at the moment of sending:

1. the requester profile is verified and not synthetic, and lives outside
   this source checkout (identity is never committed);
2. the template is ``status: approved`` with approval metadata, and its hash
   is still pinned in LOCK.json; the batch was authorized with that exact
   template hash and profile;
3. a live transport is configured: ``FOIA_TRANSPORT_PROVIDER`` is ``smtp``
   (all ``FOIA_SMTP_*`` settings present; STARTTLS enforced by the sender)
   or ``manual`` (writes filing copies; transmits nothing), and it matches
   the batch's authorized transport;
4. ``FOIA_SENDING_ENABLED=true`` is set in the environment;
5. the operator typed the per-batch confirmation phrase
   ``SEND <pending-count> <auth-id>`` exactly.

Without the phrase, ``send_batch`` is a dry run: it lists what would be sent
and every unmet precondition, and transmits nothing. The loop re-checks the
environment flag and the ledger emergency stop before every message, honours
rate limits, and never re-sends an attempt that was submitted, failed or has
an unknown outcome (the outbox allows one live intent per request).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from . import directory
from .authorization import AuthorizationError, envelope, stopped
from .dispatch import RateLimits, run_once
from .templates import lock_matches

SENDING_FLAG = "FOIA_SENDING_ENABLED"
LIVE_TRANSPORTS = ("smtp", "manual")
SOURCE_ROOT = Path(__file__).resolve().parents[1]
MAX_PER_HOST_PER_HOUR = 20
MAX_GLOBAL_PER_HOUR = 60


class SendingBlocked(RuntimeError):
    def __init__(self, blockers):
        self.blockers = list(blockers)
        super().__init__("sending is blocked: " + "; ".join(self.blockers))


@dataclass(frozen=True)
class SendingAuthority:
    """Stands in for ``Config`` in ``dispatch.run_once``; only ``open_gate`` creates one."""
    transport: str
    template_key: str
    template_sha256: str
    profile_id: str
    mode: str = "live"

    @property
    def transport_enabled(self):
        return True


def _inside_checkout(path):
    if path is None:
        return False
    try:
        resolved = Path(path).resolve()
    except OSError:
        return False
    git_checkout = (SOURCE_ROOT / ".git").exists() or (SOURCE_ROOT / "pyproject.toml").exists()
    return git_checkout and (resolved == SOURCE_ROOT or SOURCE_ROOT in resolved.parents)


def blockers(env, *, profile, template, transport=None, profile_path=None, batch=None):
    """Every unmet sending precondition, as readable strings. Empty means the gate may open."""
    out = []
    if env.get(SENDING_FLAG) != "true":
        out.append(f"{SENDING_FLAG} is not set to 'true'")
    if profile is None:
        out.append("no requester profile given")
    else:
        if profile.synthetic:
            out.append("requester profile is synthetic (test fixture)")
        if not profile.verified:
            out.append("requester profile is not marked verified")
        if not (profile.reply_email or profile.reply_postal):
            out.append("requester profile has no reply route")
        if not profile.name:
            out.append("requester profile has no name")
        if _inside_checkout(profile_path):
            out.append("requester profile is inside the source checkout; keep it outside the repository")
    if template is None:
        out.append("no template loaded")
    else:
        if template.status != "approved" or not template.approval:
            out.append(f"template {template.key} is not approved (run `foia-requests approve template`)")
        if not lock_matches(template):
            out.append(f"template {template.key} hash is not pinned in LOCK.json")
    provider = env.get("FOIA_TRANSPORT_PROVIDER", "disabled")
    transport = transport or provider
    if provider not in LIVE_TRANSPORTS:
        out.append(f"FOIA_TRANSPORT_PROVIDER is {provider!r}; set it to smtp or manual")
    elif provider != transport:
        out.append(f"FOIA_TRANSPORT_PROVIDER is {provider!r} but the batch is authorized for {transport!r}")
    if provider == "smtp":
        from .smtp_transport import SmtpSettings
        out.extend(SmtpSettings.problems(env))
    if batch is not None:
        if batch.get("transport") not in LIVE_TRANSPORTS:
            out.append(f"batch transport {batch.get('transport')!r} is not a live transport")
        if template is not None and batch.get("template_sha256") != template.sha256:
            out.append("batch was authorized with a different template revision")
        if profile is not None and (batch.get("sender_profile"), batch.get("requester")) != (
                profile.profile_id, profile.requester_principal):
            out.append("batch was authorized for a different requester profile")
    return out


def open_gate(env, *, profile, template, transport=None, profile_path=None, batch=None) -> SendingAuthority:
    found = blockers(env, profile=profile, template=template, transport=transport, profile_path=profile_path,
                     batch=batch)
    if found:
        raise SendingBlocked(found)
    return SendingAuthority(transport or env["FOIA_TRANSPORT_PROVIDER"], template.key, template.sha256,
                            profile.profile_id)


def build_sender(authority: SendingAuthority, env, *, profile, ledger=None, manual_dir=None):
    if authority.transport == "smtp":
        from .smtp_transport import SmtpSender, SmtpSettings
        return SmtpSender(SmtpSettings.from_env(env), reply_to=profile.reply_email)
    if authority.transport == "manual":
        from .manual_filing import ManualFilingSender
        if manual_dir is None:
            raise SendingBlocked(["manual transport needs --manual-out (a private folder for filing copies)"])
        return ManualFilingSender(manual_dir, component_lookup=_component_lookup(ledger))
    raise SendingBlocked([f"unknown transport {authority.transport}"])


def _component_lookup(ledger):
    def lookup(request_id):
        if ledger is None:
            return None
        request = ledger.get(request_id)
        return directory.component_record(ledger, request["component_id"])
    return lookup


def confirmation_phrase(auth_id, pending):
    return f"SEND {pending} {auth_id}"


def batch_plan(ledger, auth_id):
    """Read-only description of a batch: what is pending, what is done, what needs reconciliation."""
    env_ = envelope(ledger, auth_id)
    rows = ledger.connection.execute(
        "SELECT o.*, c.kind FROM outbox o JOIN contact_routes c ON c.route_id=o.route_id WHERE o.auth_id=? "
        "ORDER BY o.created_at, o.attempt_id", (auth_id,)).fetchall()
    auth = ledger.connection.execute("SELECT revoked_at, revoked_reason, expires_at FROM authorizations "
                                     "WHERE auth_id=?", (auth_id,)).fetchone()
    items = []
    for row in rows:
        request = ledger.get(row["request_id"])
        revision = ledger.latest_revision(row["request_id"])
        component = directory.component_record(ledger, request["component_id"]) or {}
        items.append({"request_id": row["request_id"], "attempt_id": row["attempt_id"], "outbox": row["state"],
                      "request_state": request["state"], "kind": row["kind"], "destination": row["destination"],
                      "agency": component.get("agency_name"), "component": component.get("name"),
                      "period": [request["period_start"], request["period_end"]],
                      "subject": revision["subject"], "content_sha256": row["content_sha256"]})
    pending = [i for i in items if i["outbox"] in ("queued", "leased")]
    unresolved = [i for i in items if i["outbox"] in ("unknown",) or i["request_state"] == "submission_unknown"]
    return {"auth_id": auth_id, "transport": env_["transport"], "reference": env_["reference"],
            "expires_at": auth["expires_at"], "revoked": auth["revoked_at"] is not None,
            "revoked_reason": auth["revoked_reason"], "authorized": len(env_["items"]),
            "queued_total": len(items), "pending": len(pending), "unresolved": len(unresolved),
            "items": items, "envelope": env_,
            "confirmation_phrase": confirmation_phrase(auth_id, len(pending)) if pending else None}


def send_batch(ledger, *, auth_id, env, profile, template, confirm=None, owner="send", profile_path=None,
               limits=RateLimits(), max_messages=None, manual_dir=None, ledger_path=None, sender=None,
               min_interval_seconds=0, sleep=time.sleep):
    """Dry-run by default; sends only with every gate open and the exact typed confirmation."""
    plan = batch_plan(ledger, auth_id)
    batch = plan["envelope"]
    found = blockers(env, profile=profile, template=template, transport=batch["transport"],
                     profile_path=profile_path, batch=batch)
    if plan["revoked"]:
        found.append(f"authorization is revoked: {plan['revoked_reason']}")
    if stopped(ledger):
        found.append("the emergency stop is active (foia-requests resume clears it)")
    if plan["unresolved"]:
        found.append(f"{plan['unresolved']} attempt(s) in this batch have an unknown outcome; reconcile them "
                     "(foia-requests receipt reconcile) before sending more")
    if not (1 <= limits.per_destination_per_hour <= MAX_PER_HOST_PER_HOUR
            and 1 <= limits.global_per_hour <= MAX_GLOBAL_PER_HOUR):
        found.append(f"rate limits must be 1..{MAX_PER_HOST_PER_HOUR} per host and 1..{MAX_GLOBAL_PER_HOUR} "
                     "overall per hour")
    summary = {"command": "send", "auth_id": auth_id, "transport": batch["transport"],
               "pending": plan["pending"], "items": [{k: i[k] for k in (
                   "request_id", "agency", "component", "kind", "destination", "subject", "outbox",
                   "request_state")} for i in plan["items"]],
               "blockers": found, "confirmation_phrase": plan["confirmation_phrase"], "sent": 0, "results": []}
    if confirm is None:
        summary.update(mode="dry_run", status="dry_run_nothing_sent")
        return summary
    if not plan["pending"]:
        summary.update(mode="live", status="nothing_pending")
        return summary
    if confirm != plan["confirmation_phrase"]:
        found.append(f"confirmation does not match; type exactly: {plan['confirmation_phrase']}")
    if found:
        raise SendingBlocked(found)
    authority = open_gate(env, profile=profile, template=template, transport=batch["transport"],
                          profile_path=profile_path, batch=batch)
    if sender is None:
        sender = build_sender(authority, env, profile=profile, ledger=ledger, manual_dir=manual_dir)
    elif getattr(sender, "name", None) != authority.transport:
        raise SendingBlocked([f"sender {getattr(sender, 'name', None)!r} does not match {authority.transport!r}"])
    limit = plan["pending"] if max_messages is None else min(max_messages, plan["pending"])
    results, sent = [], 0
    for index in range(limit):
        # Re-check the cheap gates before every message: flag removed or stop set halts the batch.
        if env.get(SENDING_FLAG) != "true":
            results.append({"status": "halted", "reason": f"{SENDING_FLAG} was unset"})
            break
        if stopped(ledger):
            results.append({"status": "halted", "reason": "emergency stop is active"})
            break
        if index and min_interval_seconds and sent:
            sleep(min_interval_seconds)
        try:
            result = run_once(ledger, sender, authority, owner=owner, now=ledger.clock(), limits=limits,
                              auth_id=auth_id)
        except AuthorizationError as exc:
            results.append({"status": "blocked", "reason": str(exc)})
            break
        results.append(result)
        sent += result.get("sent") or 0
        if result["status"] in ("idle", "rate_limited_local", "submission_unknown", "blocked"):
            break
        if result.get("outcome") == "auth_failed":
            break
    summary.update(mode="live", status="batch_processed", sent=sent, results=results,
                   remaining=batch_plan(ledger, auth_id)["pending"])
    if authority.transport == "manual":
        from .manual_filing import checklist_markdown, write_private
        written = [e for e in getattr(sender, "written", [])]
        if written:
            path = Path(sender.out_dir) / f"CHECKLIST-{auth_id}.md"
            write_private(path, checklist_markdown(written, auth_id, ledger_path or "<ledger>").encode("utf-8"))
            summary["manual_checklist"] = str(path)
            summary["manual_copies"] = len(written)
    return summary

