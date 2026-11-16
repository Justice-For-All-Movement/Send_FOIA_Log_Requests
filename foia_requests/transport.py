"""Pluggable submission transport interface (fail-closed).

Live transports exist (``smtp_transport.SmtpSender`` and the human-in-the-loop
``manual`` filing transport) but are only constructed by
``sending.open_gate`` after every sending precondition holds; see
docs/OWNER_SENDING_CHECKLIST.md.

Environment configuration
  FOIA_TRANSPORT_PROVIDER   "disabled" (default) | "smtp" | "manual" |
                            "fake" (tests only) | anything else ->
                            TransportNotImplemented
  FOIA_TRANSPORT_FAKE_ALLOWED  must be "1" to construct the fake provider, so a
                            stray setting cannot route production work to it.
``sender_from_env`` itself never returns a live sender: "smtp"/"manual" go
through the sending gate.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


class TransportDisabled(RuntimeError):
    pass


class TransportNotImplemented(TransportDisabled):
    pass


@dataclass(frozen=True)
class OutboundMessage:
    request_id: str
    attempt_id: str
    route_id: str
    kind: str
    destination: str
    subject: str
    body: str
    content_sha256: str


@dataclass(frozen=True)
class SendResult:
    outcome: str  # accepted | rejected | rate_limited | auth_failed | bounced | timeout
    provider_message_id: str | None = None
    retry_after: int | None = None
    error_class: str | None = None
    receipt: dict | None = None


class Sender:
    """Interface. ``send`` must transmit exactly the frozen message once or raise."""
    name = "abstract"
    live = False

    def send(self, message: OutboundMessage) -> SendResult:  # pragma: no cover - interface
        raise NotImplementedError


class DisabledSender(Sender):
    """Default transport: refuses every send and counts attempts."""
    name = "disabled"

    def __init__(self):
        self.attempts = 0

    def send(self, message):
        self.attempts += 1
        raise TransportDisabled("live transport is disabled; no message was sent")


def sender_from_env(env=None) -> Sender:
    env = os.environ if env is None else env
    provider = env.get("FOIA_TRANSPORT_PROVIDER", "disabled")
    if provider == "disabled":
        return DisabledSender()
    if provider == "fake":
        if env.get("FOIA_TRANSPORT_FAKE_ALLOWED") != "1":
            raise TransportDisabled("fake transport requires FOIA_TRANSPORT_FAKE_ALLOWED=1 (tests only)")
        from .fake_transport import FakeSender
        return FakeSender()
    if provider in ("smtp", "manual"):
        raise TransportDisabled(f"transport {provider!r} is only available through the sending gate "
                                "(foia-requests send); see docs/OWNER_SENDING_CHECKLIST.md")
    raise TransportNotImplemented(
        f"transport provider {provider!r} is not implemented; live dispatch requires EXT-DISPATCH and a reviewed adapter")
