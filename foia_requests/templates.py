"""SFL-04 versioned request templates and deterministic generation.

Templates are immutable JSON documents identified by ``template_id`` and
``revision``. ``templates/LOCK.json`` pins each revision's SHA-256 so editing a
template without bumping its revision fails loudly. Rendering is pure: the
same template, scope, component, route and requester profile always produce
byte-identical UTF-8 text and the same content hash.

Missing or unverified inputs never block a *preview*; they are rendered as
visible ``[MISSING: ...]`` markers and listed in ``missing_inputs`` so that the
draft can never be marked live-ready. Optional representations (fee waiver,
payment commitment) appear only when the profile explicitly supplies them.
"""
from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from string import Template

from .ledger import RequestScope, canonical_json, sha256_text

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December")
MAX_FIELD = 200
MAX_SUBJECT = 200
MAX_BODY = 20000
# Bidirectional overrides/isolates can disguise rendered text.
FORBIDDEN_CHARACTERS = {chr(c) for c in list(range(0x202A, 0x202F)) + list(range(0x2066, 0x206A))}
PROFILE_FIELDS = {"profile_id", "requester_principal", "name", "organization", "reply_email", "reply_postal",
                  "verified", "synthetic", "fee_policy", "delivery_format"}


class TemplateError(ValueError):
    pass


def _clean_line(value, name, required=True):
    if value is None:
        if required:
            raise TemplateError(f"{name} is required")
        return None
    if not isinstance(value, str):
        raise TemplateError(f"{name} must be text")
    value = unicodedata.normalize("NFC", value).strip()
    if required and not value:
        raise TemplateError(f"{name} is required")
    if len(value) > MAX_FIELD:
        raise TemplateError(f"{name} exceeds {MAX_FIELD} characters")
    if any(unicodedata.category(c) in ("Cc", "Cf") and c != "‍" for c in value) or \
            FORBIDDEN_CHARACTERS & set(value):
        # Includes CR/LF: prevents header/line injection into subject or body.
        raise TemplateError(f"{name} contains control or formatting characters")
    return value


def human_date(value: str) -> str:
    day = date.fromisoformat(value)
    return f"{MONTHS[day.month - 1]} {day.day}, {day.year}"


@dataclass(frozen=True)
class RequestTemplate:
    template_id: str
    revision: str
    jurisdiction: str
    record_class: str
    status: str
    authority_note: str
    requested_fields: tuple
    subject: str
    body: str
    sha256: str

    @property
    def key(self):
        return f"{self.template_id}@{self.revision}"


def template_digest(data) -> str:
    return sha256_text(canonical_json(data))


def load_template(template_id="annual_foia_log_request", revision="1", directory=TEMPLATE_DIR):
    path = Path(directory) / f"{template_id}.v{revision}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    expected = {"template_id", "revision", "jurisdiction", "record_class", "status", "authority_note",
                "requested_fields", "subject", "body"}
    if set(data) != expected:
        raise TemplateError("template fields do not match the template contract")
    if data["template_id"] != template_id or data["revision"] != revision:
        raise TemplateError("template file name does not match its id/revision")
    digest = template_digest(data)
    lock = json.loads((Path(directory) / "LOCK.json").read_text(encoding="utf-8"))
    if lock.get(f"{template_id}@{revision}") != digest:
        raise TemplateError("template content changed without a new revision (LOCK.json mismatch)")
    for text in (data["subject"], data["body"]):
        Template(text).substitute(_PLACEHOLDER_PROBE)  # unknown placeholders fail here
    return RequestTemplate(data["template_id"], data["revision"], data["jurisdiction"], data["record_class"],
                           data["status"], data["authority_note"], tuple(data["requested_fields"]),
                           data["subject"], data["body"], digest)


_PLACEHOLDER_PROBE = {k: "" for k in (
    "component_name", "agency_name", "period_start_text", "period_end_text", "field_list",
    "delivery_format", "fee_paragraph", "requester_name", "requester_block")}


@dataclass(frozen=True)
class RequesterProfile:
    profile_id: str
    requester_principal: str
    name: str | None
    organization: str | None
    reply_email: str | None
    reply_postal: str | None
    verified: bool
    synthetic: bool
    fee_policy: dict | None
    delivery_format: str | None

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict) or set(data) != PROFILE_FIELDS:
            raise TemplateError("requester profile fields do not match the profile contract")
        if type(data["verified"]) is not bool or type(data["synthetic"]) is not bool:
            raise TemplateError("verified/synthetic must be booleans")
        fee = data["fee_policy"]
        if fee is not None:
            if not isinstance(fee, dict) or set(fee) != {"max_authorized_usd", "fee_waiver_basis"}:
                raise TemplateError("fee_policy must contain max_authorized_usd and fee_waiver_basis")
            amount = fee["max_authorized_usd"]
            if type(amount) is not int or not 0 <= amount <= 100000:
                raise TemplateError("max_authorized_usd must be a whole-dollar integer 0..100000")
            _clean_line(fee["fee_waiver_basis"], "fee_waiver_basis", required=False)
        return cls(
            _clean_line(data["profile_id"], "profile_id"),
            _clean_line(data["requester_principal"], "requester_principal"),
            _clean_line(data["name"], "name", required=False) or None,
            _clean_line(data["organization"], "organization", required=False) or None,
            _clean_line(data["reply_email"], "reply_email", required=False) or None,
            _clean_line(data["reply_postal"], "reply_postal", required=False) or None,
            data["verified"], data["synthetic"], fee,
            _clean_line(data["delivery_format"], "delivery_format", required=False) or None)

    @classmethod
    def from_file(cls, path):
        raw = Path(path).read_bytes()
        if len(raw) > 65536:
            raise TemplateError("profile exceeds 64 KiB")
        return cls.from_dict(json.loads(raw.decode("utf-8")))


@dataclass(frozen=True)
class Rendered:
    request_scope: RequestScope
    template_key: str
    template_sha256: str
    subject: str
    body: str
    content_sha256: str
    recipient_route: str | None
    missing_inputs: tuple
    live_ready: bool
    manifest: dict = field(compare=False)


def _fee_paragraph(fee_policy):
    if fee_policy is None:
        return None
    amount = fee_policy["max_authorized_usd"]
    if amount == 0:
        text = ("I am not agreeing to pay fees at this time. If fees are expected, please contact me "
                "with an estimate before incurring them.")
    else:
        text = (f"I agree to pay fees up to ${amount}. If fees will exceed that amount, please contact me "
                "before incurring them.")
    if fee_policy.get("fee_waiver_basis"):
        text += f" I request a fee waiver on the following basis: {fee_policy['fee_waiver_basis']}"
    return text


def render(template: RequestTemplate, scope: RequestScope, component: dict, route: dict | None,
           profile: RequesterProfile) -> Rendered:
    """Render one component-period request. Pure and deterministic."""
    if scope.jurisdiction != template.jurisdiction or scope.record_class != template.record_class:
        raise TemplateError("template does not apply to this jurisdiction/record class")
    if scope.requester != profile.requester_principal:
        raise TemplateError("scope requester does not match the requester profile")
    if component.get("component_id") != scope.component_id:
        raise TemplateError("component does not match request scope")
    missing = []

    def need(value, name):
        if value:
            return value
        missing.append(name)
        return f"[MISSING: {name}]"

    component_name = _clean_line(component.get("name"), "component name")
    agency_name = _clean_line(component.get("agency_name"), "agency name")
    reply = [line for line in (profile.organization, profile.reply_email, profile.reply_postal) if line]
    if not (profile.reply_email or profile.reply_postal):
        missing.append("requester reply route")
    if profile.synthetic:
        missing.append("real requester profile (fixture profile is synthetic)")
    elif not profile.verified:
        missing.append("verified requester identity")
    fee = _fee_paragraph(profile.fee_policy)
    route_id = None
    if route is None:
        missing.append("official submission route")
    else:
        route_id = _clean_line(route.get("route_id"), "route_id")
        if route.get("state") != "verified":
            missing.append(f"verified submission route ({route_id} is {route.get('state')})")
        if route.get("component_id") != scope.component_id:
            raise TemplateError("route belongs to a different component")
    values = {
        "component_name": component_name,
        "agency_name": agency_name,
        "period_start_text": human_date(scope.period_start),
        "period_end_text": human_date(scope.period_end),
        "field_list": "; ".join(template.requested_fields),
        "delivery_format": need(profile.delivery_format, "delivery format"),
        "fee_paragraph": need(fee, "fee policy"),
        "requester_name": need(profile.name, "requester name"),
        "requester_block": "\n".join(reply) if reply else "[MISSING: requester reply route]",
    }
    subject = Template(template.subject).substitute(values)
    body = Template(template.body).substitute(values)
    if len(subject) > MAX_SUBJECT or len(body) > MAX_BODY:
        raise TemplateError("rendered request exceeds length bounds")
    if template.status != "approved":
        missing.append(f"approved template (status: {template.status})")
    content = sha256_text(canonical_json([subject, body, route_id]))
    missing_inputs = tuple(dict.fromkeys(missing))
    manifest = {
        "template": template.key, "template_sha256": template.sha256,
        "template_status": template.status, "authority_note": template.authority_note,
        "requested_fields": list(template.requested_fields),
        "component_id": scope.component_id, "period": [scope.period_start, scope.period_end, scope.period_basis],
        "recipient_route": route_id, "route_kind": None if route is None else route.get("kind"),
        "route_destination": None if route is None else route.get("destination"),
        "content_sha256": content, "body_bytes": len(body.encode("utf-8")),
        "attachments": [], "missing_inputs": list(missing_inputs), "live_ready": not missing_inputs,
    }
    return Rendered(scope, template.key, template.sha256, subject, body, content, route_id,
                    missing_inputs, not missing_inputs, manifest)


def store(ledger, rendered: Rendered, actor="generator"):
    """Persist a rendering as a ledger revision (idempotent for equal content)."""
    result = ledger.create_draft(rendered.request_scope, actor=actor)
    template_id, revision = rendered.template_key.split("@")
    number, created = ledger.add_revision(result.request_id, template_id, revision, rendered.subject,
                                          rendered.body, rendered.recipient_route, actor=actor)
    return result.request_id, number, created
