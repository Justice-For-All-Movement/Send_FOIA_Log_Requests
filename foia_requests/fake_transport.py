"""Deterministic in-memory fake provider for tests. It never performs I/O.

A success from this fake is *not* evidence of a live dispatch.
"""
from __future__ import annotations

import hashlib

from .transport import Sender, SendResult


class FakeTimeout(TimeoutError):
    """Simulates a timeout after the provider may already have accepted the message."""


class FakeSender(Sender):
    name = "fake"
    live = False

    def __init__(self, script=None):
        # script: list of outcomes consumed in order; default "accepted".
        self.script = list(script or [])
        self.sent = []

    def send(self, message):
        outcome = self.script.pop(0) if self.script else "accepted"
        self.sent.append((message, outcome))
        if outcome == "timeout":
            raise FakeTimeout("synthetic timeout after transmission")
        if outcome == "accepted":
            provider_id = "fake-" + hashlib.sha256(message.attempt_id.encode()).hexdigest()[:16]
            return SendResult("accepted", provider_message_id=provider_id,
                              receipt={"provider": "fake", "provider_message_id": provider_id,
                                       "content_sha256": message.content_sha256})
        if outcome == "rate_limited":
            return SendResult("rate_limited", retry_after=60, error_class="429")
        if outcome == "auth_failed":
            return SendResult("auth_failed", error_class="401/403")
        if outcome == "bounced":
            return SendResult("bounced", error_class="bounce")
        return SendResult("rejected", error_class="rejected")
