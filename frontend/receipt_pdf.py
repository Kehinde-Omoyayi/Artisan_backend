"""A receipt as a real PDF, written with nothing but the standard library.

Django ships no PDF writer and the project does not install one for a single
document, so this module emits a minimal, well-formed PDF 1.4: one A4 page, the
two base Helvetica faces every reader has, filled rectangles for the header and
rules for the table.

Two deliberate limits:

* The naira glyph is not in the WinAnsi encoding of the base fonts, so amounts
  are printed as ``NGN 164.22`` — the same form the payment provider uses.
* Nothing is measured. Every value is left-aligned in its own column, so the
  document cannot drift because of a guessed font metric.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

PAGE_WIDTH = 595
PAGE_HEIGHT = 842

BRAND = (0.455, 0.263, 0.184)      # #74432f
INK = (0.176, 0.129, 0.106)        # #2d211b
MUTED = (0.459, 0.400, 0.353)      # #75665a
RULE = (0.898, 0.855, 0.812)       # #e5dacf
SOFT = (0.957, 0.929, 0.890)       # #f4ede3
PAPER = (1.0, 1.0, 1.0)
GREEN = (0.114, 0.478, 0.282)      # #1d7a48
WHITE = (1.0, 1.0, 1.0)

LEFT = 56
VALUE_COLUMN = 300
RIGHT = PAGE_WIDTH - LEFT


_PUNCTUATION = {
    "\u2014": "-", "\u2013": "-", "\u2018": "'", "\u2019": "'",
    "\u201c": '"', "\u201d": '"', "\u2022": "*", "\u00a0": " ",
    "\u20a6": "NGN ", "\u2026": "...", "\u2192": "->",
}


def _escape(text: str) -> str:
    """WinAnsi covers the money and the dot; everything else gets a plain twin.

    Without this an em dash or a curly quote becomes a stray '?' in the middle of
    an otherwise clean receipt.
    """
    plain = str(text or "")
    for source, replacement in _PUNCTUATION.items():
        plain = plain.replace(source, replacement)
    cleaned = plain.encode("latin-1", "replace").decode("latin-1")
    cleaned = "".join(char for char in cleaned if char >= " " or char == "\t")
    return cleaned.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _text(x, y, text, *, font="F1", size=10, rgb=INK) -> str:
    return (
        f"BT {rgb[0]:.4f} {rgb[1]:.4f} {rgb[2]:.4f} rg /{font} {size} Tf "
        f"1 0 0 1 {x:.2f} {y:.2f} Tm ({_escape(text)}) Tj ET\n"
    )


def _rect(x, y, width, height, rgb) -> str:
    return (
        f"{rgb[0]:.4f} {rgb[1]:.4f} {rgb[2]:.4f} rg "
        f"{x:.2f} {y:.2f} {width:.2f} {height:.2f} re f\n"
    )


def _rule(y, rgb=RULE, width=0.8, x_start=LEFT, x_end=RIGHT) -> str:
    return (
        f"{rgb[0]:.4f} {rgb[1]:.4f} {rgb[2]:.4f} RG {width} w "
        f"{x_start:.2f} {y:.2f} m {x_end:.2f} {y:.2f} l S\n"
    )


def _money(amount) -> str:
    try:
        return f"NGN {float(amount):,.2f}"
    except (TypeError, ValueError):
        return "NGN 0.00"


def _long_date(value: str, *, local: bool = True) -> str:
    """'31 October 2026, 19:06' — the date a person would write on a receipt.

    The record is stored in UTC; the reader is in Lagos, so the printed time is
    Lagos time, matching what the chat showed when the money landed.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if local:
        try:
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=ZoneInfo("UTC"))
            moment = moment.astimezone(ZoneInfo("Africa/Lagos"))
        except Exception:
            pass
    return f"{moment.day} {moment.strftime('%B %Y')}, {moment.strftime('%H:%M')}"


def _fit_size(value, *, base=10.0, width=236.0) -> float:
    """The largest size at which a value still fits its column on one line.

    A reference such as PAY-<uuid> is too wide at 10 pt in base-14 Helvetica, so
    it is printed smaller rather than broken across two lines.
    """
    length = max(1, len(str(value or "")))
    return max(7.0, min(base, width / (0.55 * length)))


def _row(ops: list, y: float, label: str, value: str, *, strong=False) -> float:
    ops.append(_text(LEFT, y, label, size=9.5, rgb=MUTED))
    shown = value or "-"
    ops.append(
        _text(
            VALUE_COLUMN,
            y,
            shown,
            font="F2" if strong else "F1",
            size=_fit_size(shown, base=10.5 if strong else 10.0),
            rgb=INK,
        )
    )
    return y - 22


def build_receipt_pdf(receipt: dict, *, platform_name: str = "Artisan Market") -> bytes:
    ops: list[str] = []

    # Header band
    band_height = 118
    ops.append(_rect(0, PAGE_HEIGHT - band_height, PAGE_WIDTH, band_height, BRAND))
    ops.append(
        _text(LEFT, PAGE_HEIGHT - 46, platform_name.upper(), font="F2", size=10, rgb=WHITE)
    )
    ops.append(_text(LEFT, PAGE_HEIGHT - 78, "Payment receipt", font="F2", size=24, rgb=WHITE))
    ops.append(
        _text(
            LEFT,
            PAGE_HEIGHT - 100,
            f"Receipt {receipt.get('number') or ''}",
            size=10,
            rgb=(0.93, 0.88, 0.84),
        )
    )

    # Amount block
    y = PAGE_HEIGHT - band_height - 54
    status_label = str(receipt.get("status_label") or "")
    if status_label:
        ops.append(_text(LEFT, y, status_label.upper(), font="F2", size=9.5, rgb=GREEN))
        y -= 34
    ops.append(_text(LEFT, y, _money((receipt.get("amounts") or {}).get("total")), font="F2", size=32, rgb=INK))
    y -= 26
    payer = str(receipt.get("payer_name") or "")
    when = _long_date(receipt.get("issued_at") or "")
    sub = " · ".join(part for part in (f"Received from {payer}" if payer else "", when) if part)
    ops.append(_text(LEFT, y, sub, size=10, rgb=MUTED))

    # Payment details table
    y -= 44
    ops.append(_rule(y + 12))
    ops.append(_text(LEFT, y - 6, "PAYMENT DETAILS", font="F2", size=8.5, rgb=MUTED))
    y -= 34

    amounts = receipt.get("amounts") or {}
    rows = [
        ("Service", str(receipt.get("service") or "Service booking")),
        ("Service amount", _money(amounts.get("service"))),
        ("Service fee", _money(amounts.get("fee"))),
        ("Total paid", _money(amounts.get("total"))),
    ]
    if "net_to_payee" in amounts:
        rows.append(("Net to you", _money(amounts.get("net_to_payee"))))
    for index, (label, value) in enumerate(rows):
        y = _row(ops, y, label, value, strong=label in {"Total paid", "Net to you"})
        if index == len(rows) - 1:
            ops.append(_rule(y + 12))

    # Who and how
    y -= 18
    ops.append(_text(LEFT, y, "PAID TO", font="F2", size=8.5, rgb=MUTED))
    y -= 22
    for label, value in (
        ("Payee", str(receipt.get("payee_name") or "")),
        ("Method", str(receipt.get("method") or "")),
        ("Provider reference", str(receipt.get("provider_reference") or "")),
        ("Our reference", str(receipt.get("reference") or "")),
    ):
        y = _row(ops, y, label, value)

    # Nothing after the reference: a receipt ends where the facts end.

    stream = "".join(ops).encode("latin-1", "replace")
    return _assemble(stream)


def _assemble(stream: bytes) -> bytes:
    header = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
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

    out = bytearray(header)
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
