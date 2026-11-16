"""Local fake SMTP submission server for tests (127.0.0.1 only, throwaway TLS cert).

It accepts any recipient and stores messages in memory; nothing is relayed.
Modes: ``no_starttls`` (does not offer STARTTLS), ``drop_after_data`` (closes
the connection after receiving the message without replying), ``reject_rcpt``
(550 on RCPT) and ``tempfail_rcpt`` (451 on RCPT).
"""
import base64
import shutil
import socketserver
import ssl
import subprocess
import tempfile
import threading
from pathlib import Path

USERNAME, PASSWORD = "test-user", "test-password-not-a-secret"


def make_cert(folder):
    folder = Path(folder)
    cert, key = folder / "cert.pem", folder / "key.pem"
    if shutil.which("openssl") is None:
        raise RuntimeError("openssl is required to create the throwaway test certificate")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out",
                    str(cert), "-days", "2", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
                    "-addext", "basicConstraints=critical,CA:TRUE",
                    "-addext", "keyUsage=critical,digitalSignature,keyCertSign"],
                   check=True, capture_output=True)
    return cert, key


class _Handler(socketserver.StreamRequestHandler):
    def setup(self):
        super().setup()
        self.tls = False

    def finish(self):
        try:
            super().finish()
        finally:
            if self.tls:
                self.request.close()

    def reply(self, text):
        self.wfile.write((text + "\r\n").encode())
        self.wfile.flush()

    def readline(self):
        line = self.rfile.readline(65536)
        if not line:
            raise ConnectionError("client closed")
        return line.decode("utf-8", "replace").rstrip("\r\n")

    def handle(self):
        server = self.server
        server.connections += 1
        self.reply("220 localhost fake ESMTP")
        authed = False
        mail_from, rcpts = None, []
        try:
            while True:
                line = self.readline()
                verb = line.split(" ", 1)[0].upper()
                server.commands.append(verb)
                if verb in ("EHLO", "HELO"):
                    lines = ["250-localhost"]
                    if not self.tls and server.mode != "no_starttls":
                        lines.append("250-STARTTLS")
                    if self.tls or server.mode == "no_starttls":
                        lines.append("250-AUTH PLAIN")
                    lines.append("250 8BITMIME")
                    for item in lines:
                        self.reply(item)
                elif verb == "STARTTLS":
                    self.reply("220 2.0.0 Ready to start TLS")
                    self.request = server.tls_context.wrap_socket(self.request, server_side=True)
                    self.rfile = self.request.makefile("rb")
                    self.wfile = self.request.makefile("wb")
                    self.tls = True
                elif verb == "AUTH":
                    parts = line.split()
                    decoded = base64.b64decode(parts[2]).split(b"\0") if len(parts) > 2 else []
                    if decoded[1:] == [USERNAME.encode(), server.password.encode()] and self.tls:
                        authed = True
                        server.logins += 1
                        self.reply("235 2.7.0 Authentication successful")
                    else:
                        self.reply("535 5.7.8 Authentication credentials invalid")
                elif verb == "MAIL":
                    if not authed:
                        self.reply("530 5.7.0 Authentication required")
                        continue
                    mail_from = line[10:].strip("<> ")
                    self.reply("250 2.1.0 Ok")
                elif verb == "RCPT":
                    if server.mode == "reject_rcpt":
                        self.reply("550 5.1.1 Mailbox unavailable")
                        continue
                    if server.mode == "tempfail_rcpt":
                        self.reply("451 4.7.1 Try again later")
                        continue
                    rcpts.append(line[8:].strip("<> "))
                    self.reply("250 2.1.5 Ok")
                elif verb == "DATA":
                    self.reply("354 End data with <CR><LF>.<CR><LF>")
                    data = []
                    while True:
                        chunk = self.rfile.readline(1 << 20)
                        if chunk in (b".\r\n", b""):
                            break
                        data.append(chunk[1:] if chunk.startswith(b"..") else chunk)
                    raw = b"".join(data)
                    server.messages.append({"from": mail_from, "to": list(rcpts), "raw": raw})
                    if server.mode == "drop_after_data":
                        return
                    server.counter += 1
                    self.reply(f"250 2.0.0 Ok: queued as FAKE{server.counter:04d}")
                    rcpts = []
                elif verb == "RSET":
                    rcpts = []
                    self.reply("250 Ok")
                elif verb == "QUIT":
                    self.reply("221 Bye")
                    return
                else:
                    self.reply("502 Command not implemented")
        except (ConnectionError, ssl.SSLError, OSError):
            return


class FakeSmtpServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, cert, key, mode="normal", password=PASSWORD):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.mode = mode
        self.password = password
        self.messages, self.commands = [], []
        self.connections = self.logins = self.counter = 0
        self.tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.tls_context.load_cert_chain(cert, key)
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)

    @property
    def port(self):
        return self.server_address[1]

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.shutdown()
        self.server_close()


class CertFolder:
    """One throwaway certificate per test class."""

    def __init__(self):
        self.folder = tempfile.TemporaryDirectory()
        self.cert, self.key = make_cert(self.folder.name)

    def cleanup(self):
        self.folder.cleanup()


def smtp_env(server, cert, **extra):
    env = {"FOIA_SMTP_HOST": "localhost", "FOIA_SMTP_PORT": str(server.port), "FOIA_SMTP_USERNAME": USERNAME,
           "FOIA_SMTP_PASSWORD": PASSWORD, "FOIA_SMTP_FROM": "requests@sender.example",
           "FOIA_SMTP_CA_FILE": str(cert), "FOIA_SMTP_TIMEOUT": "10", "FOIA_TRANSPORT_PROVIDER": "smtp",
           "FOIA_SENDING_ENABLED": "true"}
    env.update(extra)
    return env
