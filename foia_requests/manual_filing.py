"""Manual-filing transport for agencies that only accept web portals.

The "send" step of this transport transmits nothing. For each authorized,
queued request it writes ready-to-paste text and a printable PDF into a
private folder; ``sending.write_checklist`` adds one checklist for the batch.
The request stays ``submitting`` until the owner files it and records the
outcome with ``record_manual_filing`` (CLI: ``foia-requests receipt manual``).
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from .authorization import require_role
from .dispatch import MANUAL_OUTCOME
from .ledger import canonical_json
from .pdf import text_pdf
from .receipts import record_acknowledgment
from .transport import Sender, SendResult


class ManualFilingError(RuntimeError):
    pass


def private_dir(path) -> Path:
    path = Path(os.path.abspath(path))
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ManualFilingError(f"{path} must be a private (0700) real directory")
    return path


def write_private(path: Path, data: bytes):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)


def _stem(message):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", message.request_id)[:80]


def paste_text(message, component=None):
    where = ("Portal: " if message.kind == "portal" else "E-mail to: " if message.kind == "email"
             else "Route: ") + message.destination
    lines = ["MANUAL FILING COPY - file this request yourself; the software sent nothing.", ""]
    if component:
        lines += [f"Agency: {component['agency_name']}", f"Component: {component['name']}"]
    lines += [where, f"Request ID: {message.request_id}", f"Content SHA-256: {message.content_sha256}", "",
              "=== SUBJECT / SHORT DESCRIPTION (paste) ===", message.subject, "",
              "=== REQUEST TEXT (paste into the description field, or attach the PDF) ===", message.body]
    return "\n".join(lines).rstrip("\n") + "\n"


class ManualFilingSender(Sender):
    """Writes filing copies instead of transmitting. Requires the sending gate like any live sender."""
    name = "manual"
    live = True

    def __init__(self, out_dir, component_lookup=None):
        self.out_dir = private_dir(out_dir)
        self.component_lookup = component_lookup or (lambda request_id: None)
        self.attempts = 0
        self.written = []

    def send(self, message) -> SendResult:
        self.attempts += 1
        component = self.component_lookup(message.request_id)
        stem = _stem(message)
        text = paste_text(message, component)
        write_private(self.out_dir / f"{stem}.txt", text.encode("utf-8"))
        letter = f"{message.subject}\n\n{message.body}"
        write_private(self.out_dir / f"{stem}.pdf", text_pdf(letter, title=message.subject))
        entry = {"request_id": message.request_id, "attempt_id": message.attempt_id, "kind": message.kind,
                 "destination": message.destination, "text": f"{stem}.txt", "pdf": f"{stem}.pdf",
                 "agency": component and component["agency_name"], "component": component and component["name"],
                 "subject": message.subject, "content_sha256": message.content_sha256}
        self.written.append(entry)
        return SendResult(MANUAL_OUTCOME, receipt={"provider": "manual", "files": [entry["text"], entry["pdf"]],
                                                   "content_sha256": message.content_sha256})


def checklist_markdown(entries, auth_id, ledger_path):
    lines = [f"# Manual filing checklist - {auth_id}", "",
             "The software sent nothing for these requests. File each one yourself, then record the result.",
             "Do not file a request twice. If you are unsure whether a filing went through, check the portal's",
             "account history before trying again.", ""]
    for number, entry in enumerate(entries, 1):
        lines += [f"## {number}. {entry.get('agency') or ''} - {entry.get('component') or entry['request_id']}", "",
                  f"- [ ] Open {'the portal' if entry['kind'] == 'portal' else 'your mail client'}: "
                  f"`{entry['destination']}`",
                  f"- [ ] Paste the subject and request text from `{entry['text']}` (or attach `{entry['pdf']}`)",
                  "- [ ] Submit, then save the confirmation page or e-mail (PDF or screenshot)",
                  "- [ ] Record it:", "",
                  "```bash",
                  f"foia-requests receipt manual --ledger {ledger_path} --attempt {entry['attempt_id']} \\",
                  "  --operator owner --confirmation <portal-confirmation-number> --evidence <saved-confirmation-file>",
                  "```", "",
                  "  If the portal refused it: add `--failed --reason \"...\"` instead of `--confirmation`.", ""]
    return "\n".join(lines)


def record_manual_filing(ledger, attempt_id, operator, *, confirmation=None, evidence_path=None, failed=False,
                         reason=None, tracking_number=None):
    """Record the owner's manual filing outcome for an attempt left ``submitting`` by the manual transport."""
    require_role(ledger, operator, "dispatcher")
    row = ledger.connection.execute("SELECT * FROM outbox WHERE attempt_id=?", (attempt_id,)).fetchone()
    if row is None:
        raise ManualFilingError("unknown attempt")
    manual = ledger.connection.execute("SELECT 1 FROM dispatch_log WHERE attempt_id=? AND outcome=?",
                                       (attempt_id, MANUAL_OUTCOME)).fetchone()
    if manual is None:
        raise ManualFilingError("attempt was not produced by the manual-filing transport")
    if row["state"] != "submitting":
        raise ManualFilingError(f"attempt is {row['state']}; a filing outcome was already recorded")
    evidence_sha = None
    if evidence_path is not None:
        data = Path(evidence_path).read_bytes()
        evidence_sha = hashlib.sha256(data).hexdigest()
    now = ledger.clock()
    with ledger.transaction() as conn:
        if failed:
            if not reason:
                raise ManualFilingError("a reason is required for a failed filing")
            conn.execute("UPDATE outbox SET state='failed', updated_at=? WHERE attempt_id=?", (now, attempt_id))
            ledger.transition(row["request_id"], "delivery_failed", operator, f"manual filing failed: {reason}",
                              {"attempt_id": attempt_id, "evidence_sha256": evidence_sha})
            return {"status": "delivery_failed", "attempt_id": attempt_id}
        if not confirmation or evidence_sha is None:
            raise ManualFilingError("a portal confirmation and an evidence file are required")
        receipt = {"provider": "manual", "confirmation": confirmation, "destination": row["destination"],
                   "evidence_sha256": evidence_sha, "evidence_name": Path(evidence_path).name,
                   "content_sha256": row["content_sha256"], "recorded_by": operator}
        payload = canonical_json(receipt)
        receipt_id = "rcpt-" + hashlib.sha256(f"{attempt_id}:manual:{payload}".encode()).hexdigest()[:24]
        conn.execute("INSERT INTO receipts VALUES (?,?,?,?,?,?,?,?)",
                     (receipt_id, attempt_id, row["request_id"], "manual_filing", confirmation, payload,
                      hashlib.sha256(payload.encode()).hexdigest(), now))
        conn.execute("UPDATE outbox SET state='submitted', updated_at=? WHERE attempt_id=?", (now, attempt_id))
        ledger.transition(row["request_id"], "submitted", operator, "filed manually by the owner",
                          {"attempt_id": attempt_id, "receipt_id": receipt_id, "confirmation": confirmation})
    result = {"status": "submitted", "attempt_id": attempt_id, "receipt_id": receipt_id}
    if tracking_number:
        result["acknowledgment"] = record_acknowledgment(ledger, row["request_id"], operator,
                                                         tracking_number=tracking_number,
                                                         evidence_sha256=evidence_sha, source="manual_filing")
    return result
