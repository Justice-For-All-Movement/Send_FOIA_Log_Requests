"""Minimal dependency-free plain-text PDF writer (Helvetica, US Letter).

Used for printable manual-filing copies. Text outside Latin-1 is replaced with
"?" because the standard Type 1 fonts have no wider encoding; the .txt copy
next to every PDF keeps the exact UTF-8 text.
"""
from __future__ import annotations

import textwrap

PAGE_WIDTH, PAGE_HEIGHT = 612, 792
MARGIN = 72
FONT_SIZE = 10.5
LEADING = 14
WRAP = 92


def _escape(line: str) -> str:
    safe = line.encode("latin-1", "replace").decode("latin-1")
    return safe.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _lines(text: str):
    out = []
    for paragraph in text.replace("\r\n", "\n").split("\n"):
        wrapped = textwrap.wrap(paragraph, WRAP, break_long_words=True, replace_whitespace=False)
        out.extend(wrapped or [""])
    return out


def text_pdf(text: str, title: str = "") -> bytes:
    lines = _lines(text)
    per_page = (PAGE_HEIGHT - 2 * MARGIN) // LEADING
    pages = [lines[i:i + per_page] for i in range(0, len(lines), per_page)] or [[]]
    objects = []  # index 0 -> object 1

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog = add(b"")  # placeholder, filled below
    pages_obj = add(b"")
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    kids = []
    for page_lines in pages:
        stream = [f"BT /F1 {FONT_SIZE} Tf {LEADING} TL {MARGIN} {PAGE_HEIGHT - MARGIN} Td"]
        for line in page_lines:
            stream.append(f"({_escape(line)}) Tj T*")
        stream.append("ET")
        content = "\n".join(stream).encode("latin-1")
        content_obj = add(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
        kids.append(add(("<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %d %d] /Resources << /Font << /F1 %d 0 R >> >>"
                         " /Contents %d 0 R >>" % (pages_obj, PAGE_WIDTH, PAGE_HEIGHT, font, content_obj)).encode()))
    objects[catalog - 1] = b"<< /Type /Catalog /Pages %d 0 R >>" % pages_obj
    objects[pages_obj - 1] = ("<< /Type /Pages /Kids [%s] /Count %d >>"
                              % (" ".join(f"{k} 0 R" for k in kids), len(kids))).encode()
    info = add(("<< /Title (%s) /Producer (foia-requests) >>" % _escape(title)).encode("latin-1"))
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1, catalog, info, xref)
    return bytes(out)
