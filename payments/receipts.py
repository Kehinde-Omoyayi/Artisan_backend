"""Small dependency-free PDF receipts for verified artisan payouts."""
from decimal import Decimal
import textwrap

from django.utils import timezone

from .models import EscrowPayment, EscrowRelease


def _money(amount_kobo):
    return f"NGN {Decimal(amount_kobo) / Decimal('100'):,.2f}"


def _ascii_pdf_text(value):
    text = str(value or "").encode("latin-1", "replace").decode("latin-1")
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").replace("\r", " ").replace("\n", " ")


def _make_pdf(lines):
    content_parts = []
    y = 748
    for index, line in enumerate(lines):
        font_size = 17 if index == 0 else 12 if index == 1 else 10
        content_parts.append(f"BT /F1 {font_size} Tf 50 {y} Td ({_ascii_pdf_text(line)}) Tj ET")
        y -= 24 if index < 2 else 17
        if y < 45:
            break
    stream = ("\n".join(content_parts) + "\n").encode("latin-1", "replace")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"endstream",
    ]
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode("ascii"))
        output.extend(obj)
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    return bytes(output)


def payout_receipt_pdf(payment_id):
    payment = (
        EscrowPayment.objects.select_related(
            "booking__request__category", "artisan__user", "addon_order__accepted_offer"
        )
        .get(pk=payment_id)
    )
    release = EscrowRelease.objects.get(payment=payment)
    if release.status != EscrowRelease.Status.SUCCESSFUL:
        raise ValueError("A payout receipt can only be issued after the provider confirms the transfer.")
    artisan_name = payment.artisan.business_name or payment.artisan.user.get_full_name() or payment.artisan.user.email
    booking = payment.booking
    if payment.addon_order_id:
        label = "Separate add-on order"
        scope = payment.addon_order.accepted_offer.description
        order_ref = f"Add-on order: {payment.addon_order_id}"
    else:
        label = "Original booking"
        scope = getattr(getattr(booking.request, "category", None), "name", "Service booking")
        order_ref = "Add-on order: none"
    completed_at = release.completed_at or timezone.now()
    scope_lines = textwrap.wrap(str(scope).replace("\n", " "), width=82) or ["Service work"]
    lines = [
        "ARTISAN MARKETPLACE PAYOUT RECEIPT",
        "Verified payout confirmation",
        f"Artisan: {artisan_name}",
        f"Booking: {booking.id}",
        order_ref,
        f"Payout type: {label}",
        "Work scope:",
        *[f"  {line}" for line in scope_lines[:4]],
        f"Payment reference: {payment.reference}",
        f"Payout reference: {release.reference}",
        f"Service amount: {_money(payment.service_amount_kobo)}",
        f"Marketplace commission: {_money(payment.platform_commission_kobo)}",
        f"Artisan payout: {_money(payment.artisan_payout_kobo)}",
        f"Payout status: {release.status}",
        f"Transfer completed: {timezone.localtime(completed_at).strftime('%Y-%m-%d %H:%M %Z')}",
        "This receipt reflects a provider-confirmed payout. Keep it for your records.",
    ]
    return _make_pdf(lines)
