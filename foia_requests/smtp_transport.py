"""SMTP e-mail transport (STARTTLS required).

Configuration comes only from the environment; nothing is read from the
repository and nothing is logged except the server's reply codes:

  FOIA_SMTP_HOST      submission server host name (no IP literals)
  FOIA_SMTP_PORT      submission port (default 587)
  FOIA_SMTP_USERNAME  login user
  FOIA_SMTP_PASSWORD  login password / app password
  FOIA_SMTP_FROM      envelope and header sender address
  FOIA_SMTP_CA_FILE   optional extra CA bundle (tests use a throwaway CA)
  FOIA_SMTP_TIMEOUT   optional socket timeout in seconds (default 60)

The connection must upgrade with STARTTLS and verify the server certificate
before any credential or message byte is sent; a server that does not offer
STARTTLS is refused. Failures before the DATA command are definite (nothing
was handed over). A connection loss after DATA starts raises, which the
dispatch worker records as ``submission_unknown`` (never retried
automatically).
"""
from __future__ import annotations

import hashlib
import ipaddress
import re
import smtplib
import socket
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import format_datetime
from datetime import datetime, timezone

from .transport import Sender, SendResult

ADDRESS = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+")
REQUIRED = ("FOIA_SMTP_HOST", "FOIA_SMTP_USERNAME", "FOIA_SMTP_PASSWORD", "FOIA_SMTP_FROM")


class SmtpConfigError(ValueError):
    pass


@dataclass(frozen=True)
class SmtpSettings:
    host: str
    port: int
    username: str
    password: str
    sender: str
    ca_file: str | None = None
    timeout: float = 60.0

    def __repr__(self):  # never expose the password
        return f"SmtpSettings(host={self.host!r}, port={self.port}, username=<set>, sender={self.sender!r})"

    @classmethod
    def problems(cls, env):
        problems = [f"{name} is not set" for name in REQUIRED if not env.get(name)]
        host = env.get("FOIA_SMTP_HOST") or ""
        try:
            ipaddress.ip_address(host)
            problems.append("FOIA_SMTP_HOST must be a host name so the certificate can be verified")
        except ValueError:
            pass
        port = env.get("FOIA_SMTP_PORT", "587")
        if not port.isdigit() or not 1 <= int(port) <= 65535:
            problems.append("FOIA_SMTP_PORT must be a port number")
        elif int(port) == 465:
            problems.append("FOIA_SMTP_PORT 465 is implicit TLS; use the STARTTLS submission port (587)")
        sender = env.get("FOIA_SMTP_FROM")
        if sender and not ADDRESS.fullmatch(sender):
            problems.append("FOIA_SMTP_FROM must be a plain e-mail address")
        timeout = env.get("FOIA_SMTP_TIMEOUT", "60")
        try:
            if not 1 <= float(timeout) <= 600:
                raise ValueError
        except ValueError:
            problems.append("FOIA_SMTP_TIMEOUT must be 1..600 seconds")
        return problems

    @classmethod
    def from_env(cls, env):
        problems = cls.problems(env)
        if problems:
            raise SmtpConfigError("; ".join(problems))
        return cls(env["FOIA_SMTP_HOST"], int(env.get("FOIA_SMTP_PORT", "587")), env["FOIA_SMTP_USERNAME"],
                   env["FOIA_SMTP_PASSWORD"], env["FOIA_SMTP_FROM"], env.get("FOIA_SMTP_CA_FILE") or None,
                   float(env.get("FOIA_SMTP_TIMEOUT", "60")))


def build_message(message, settings: SmtpSettings, reply_to=None, now=None):
    """Return the exact RFC 5322 message for an outbound request."""
    if message.kind != "email" or not ADDRESS.fullmatch(message.destination):
        raise SmtpConfigError("SMTP can only send to a verified e-mail route")
    mail = EmailMessage()
    mail["From"] = settings.sender
    mail["To"] = message.destination
    if reply_to and reply_to != settings.sender:
        mail["Reply-To"] = reply_to
    mail["Subject"] = message.subject
    mail["Date"] = format_datetime(now or datetime.now(timezone.utc))
    # Deterministic per attempt so a provider or agency can spot a duplicate.
    domain = settings.sender.rsplit("@", 1)[1]
    mail["Message-ID"] = f"<{message.attempt_id}@{domain}>"
    mail.set_content(message.body, charset="utf-8")
    return mail


class SmtpSender(Sender):
    name = "smtp"
    live = True

    def __init__(self, settings: SmtpSettings, *, reply_to=None, smtp_class=smtplib.SMTP, clock=None):
        self.settings = settings
        self.reply_to = reply_to
        self.smtp_class = smtp_class
        self.clock = clock
        self.attempts = 0

    def _context(self):
        context = ssl.create_default_context(cafile=self.settings.ca_file)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        return context

    def send(self, message) -> SendResult:
        self.attempts += 1
        try:
            mail = build_message(message, self.settings, self.reply_to, self.clock() if self.clock else None)
        except SmtpConfigError as exc:
            return SendResult("rejected", error_class=f"config: {exc}")
        # Hash and send the exact wire form (CRLF line endings).
        raw = mail.as_bytes(policy=mail.policy.clone(linesep="\r\n"))
        stage = "connect"
        try:
            client = self.smtp_class(self.settings.host, self.settings.port, timeout=self.settings.timeout)
        except (OSError, smtplib.SMTPException) as exc:
            return SendResult("rejected", error_class=f"connect: {type(exc).__name__}")
        try:
            stage = "ehlo"
            client.ehlo()
            if not client.has_extn("starttls"):
                return SendResult("rejected", error_class="starttls_unavailable")
            stage = "starttls"
            client.starttls(context=self._context())
            client.ehlo()
            stage = "auth"
            try:
                client.login(self.settings.username, self.settings.password)
            except smtplib.SMTPAuthenticationError as exc:
                return SendResult("auth_failed", error_class=f"auth {exc.smtp_code}")
            except smtplib.SMTPNotSupportedError:
                return SendResult("auth_failed", error_class="auth not offered after STARTTLS")
            stage = "envelope"
            code, reply = client.mail(self.settings.sender)
            if code != 250:
                return _refusal("MAIL", code, reply)
            code, reply = client.rcpt(message.destination)
            if code not in (250, 251):
                return _refusal("RCPT", code, reply)
            stage = "data"
            code, reply = client.data(raw)
        except (ssl.SSLError, ssl.CertificateError) as exc:
            if stage == "data":
                raise
            return SendResult("rejected", error_class=f"tls: {type(exc).__name__}")
        except (smtplib.SMTPServerDisconnected, socket.timeout, OSError) as exc:
            if stage == "data":
                raise  # the server may already hold the message: unknown outcome
            return SendResult("rejected", error_class=f"{stage}: {type(exc).__name__}")
        except smtplib.SMTPResponseException as exc:
            if stage == "data":
                if 400 <= exc.smtp_code < 500:
                    return SendResult("rate_limited", retry_after=900, error_class=f"DATA {exc.smtp_code}")
                return SendResult("rejected", error_class=f"DATA {exc.smtp_code}")
            return _refusal(stage, exc.smtp_code, exc.smtp_error)
        finally:
            try:
                client.quit()
            except Exception:  # noqa: BLE001 - closing never changes the outcome
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass
        if code != 250:
            return _refusal("DATA", code, reply)
        reply_text = reply.decode("utf-8", "replace") if isinstance(reply, bytes) else str(reply)
        receipt = {
            "provider": "smtp", "smtp_host": self.settings.host, "smtp_port": self.settings.port,
            "from": self.settings.sender, "to": message.destination, "message_id": mail["Message-ID"],
            "date": mail["Date"], "smtp_reply_code": code, "smtp_reply": reply_text[:500],
            "content_sha256": message.content_sha256,
            "rfc822_sha256": hashlib.sha256(raw).hexdigest(), "tls": "starttls",
        }
        return SendResult("accepted", provider_message_id=mail["Message-ID"], receipt=receipt)


def _refusal(stage, code, reply):
    if 400 <= int(code) < 500:
        return SendResult("rate_limited", retry_after=900, error_class=f"{stage} {code}")
    return SendResult("rejected", error_class=f"{stage} {code}")

