"""A very small PDF writer: one page, base-14 fonts, no dependencies.

Django ships no PDF library and the project does not install one for a handful of
receipts, so documents are assembled here. One page, Helvetica, filled
rectangles and rules — enough for a receipt and nothing more.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

PAGE_WIDTH = 595
PAGE_HEIGHT = 842

BRAND = (0.455, 0.263, 0.184)
INK = (0.176, 0.129, 0.106)
MUTED = (0.459, 0.400, 0.353)
RULE = (0.898, 0.855, 0.812)
GREEN = (0.114, 0.478, 0.282)
WHITE = (1.0, 1.0, 1.0)

LEFT = 56
VALUE_COLUMN = 300
RIGHT = PAGE_WIDTH - LEFT


_PUNCTUATION = {
    "\u2014": "-", "\u2013": "-", "\u2018": "'", "\u2019": "'",
    "\u201c": '"', "\u201d": '"', "\u2022": "*", "\u00a0": " ",
    "\u20a6": "NGN ", "\u2026": "...", "\u2192": "->",
}


def escape(value) -> str:
    plain = str(value or "")
    for source, replacement in _PUNCTUATION.items():
        plain = plain.replace(source, replacement)
    cleaned = plain.encode("latin-1", "replace").decode("latin-1")
    cleaned = "".join(char for char in cleaned if char >= " " or char == "\t")
    return cleaned.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def text(x, y, value, *, font="F1", size=10, rgb=INK) -> str:
    return (
        f"BT {rgb[0]:.4f} {rgb[1]:.4f} {rgb[2]:.4f} rg /{font} {size} Tf "
        f"1 0 0 1 {x:.2f} {y:.2f} Tm ({escape(value)}) Tj ET\n"
    )


def rect(x, y, width, height, rgb) -> str:
    return (
        f"{rgb[0]:.4f} {rgb[1]:.4f} {rgb[2]:.4f} rg "
        f"{x:.2f} {y:.2f} {width:.2f} {height:.2f} re f\n"
    )


def rule(y, rgb=RULE, width=0.8, x_start=LEFT, x_end=RIGHT) -> str:
    return (
        f"{rgb[0]:.4f} {rgb[1]:.4f} {rgb[2]:.4f} RG {width} w "
        f"{x_start:.2f} {y:.2f} m {x_end:.2f} {y:.2f} l S\n"
    )


def wrap(value, width=42) -> list:
    """Break a long value on the page instead of letting it run off the edge."""
    words = str(value or "").split(" ")
    lines: list = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        while len(candidate) > width:
            lines.append(candidate[:width])
            candidate = candidate[width:]
        current = candidate
    if current:
        lines.append(current)
    return lines or [""]


def fit_size(value, *, base=10.0, width=236.0) -> float:
    """The largest size at which the value still fits its column on one line.

    Base-14 Helvetica averages about 0.55 em per character; a reference such as
    RELEASE-ADDON-<uuid> is too wide at 10 pt, so it is printed smaller instead of
    being broken in two.
    """
    length = max(1, len(str(value or "")))
    return max(7.0, min(base, width / (0.55 * length)))


def row(ops, y, label, value, *, strong=False, wrap_width=None) -> float:
    ops.append(text(LEFT, y, label, size=9.5, rgb=MUTED))
    shown = value or "-"
    size = fit_size(shown, base=10.5 if strong else 10.0)
    ops.append(
        text(
            VALUE_COLUMN,
            y,
            shown,
            font="F2" if strong else "F1",
            size=size,
            rgb=INK,
        )
    )
    return y - 22


def money(amount) -> str:
    try:
        return f"NGN {float(amount):,.2f}"
    except (TypeError, ValueError):
        return "NGN 0.00"


def long_date(value) -> str:
    """'31 October 2026, 19:06' in Lagos time — what the reader saw on screen."""
    if not value:
        return ""
    moment = None
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return str(value)
    try:
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=ZoneInfo("UTC"))
        moment = moment.astimezone(ZoneInfo("Africa/Lagos"))
    except Exception:
        pass
    return f"{moment.day} {moment.strftime('%B %Y')}, {moment.strftime('%H:%M')}"


def assemble(stream: bytes) -> bytes:
    """Wrap a content stream in a one-page PDF with a correct cross-reference."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] "
            "/Resources << /Font << /F1 4 0 R /F2 5 0 R >> >> /Contents 6 0 R >>"
        ).encode("latin-1"),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
        b"<< /Length " + str(len(stream)).encode("latin-1") + b" >>\nstream\n" + stream + b"\nendstream",
    ]

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode("latin-1") + body + b"\nendobj\n"

    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("latin-1")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("latin-1")
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n"
    ).encode("latin-1")
    return bytes(out)
