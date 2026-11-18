"""The receipt a payer keeps after a successful payment.

Everything here comes from the payment record itself. The amount on a receipt is
the amount the provider confirmed, not the price on the booking and not a figure
worked out again at display time; if the record does not say money was taken,
there is no receipt to show.
"""
from zoneinfo import ZoneInfo

from django.utils import timezone

from .models import EscrowPayment

# Money has moved and has not been given back. FAILED, CANCELLED and
# PAYMENT_PENDING are deliberately absent: a receipt for money that never
# arrived would be a lie.
PAID_STATUSES = frozenset(
    {
        EscrowPayment.Status.HELD,
        EscrowPayment.Status.DISPUTED,
        EscrowPayment.Status.RELEASE_PENDING,
        EscrowPayment.Status.RELEASED,
        EscrowPayment.Status.REFUND_PENDING,
        EscrowPayment.Status.REFUNDED,
    }
)

# A receipt states whether the money arrived, not what the platform will do with
# it next. The escrow story belongs in the chat, not on the document.
STATUS_LABELS = {
    EscrowPayment.Status.HELD: "Paid",
    EscrowPayment.Status.DISPUTED: "Paid · under dispute",
    EscrowPayment.Status.RELEASE_PENDING: "Paid · release in progress",
    EscrowPayment.Status.RELEASED: "Paid",
    EscrowPayment.Status.REFUND_PENDING: "Paid · refund in progress",
    EscrowPayment.Status.REFUNDED: "Refunded",
}

_METHOD_LABELS = {
    "bank_transfer": "Bank transfer",
    "banktransfer": "Bank transfer",
    "card": "Card",
    "mobilemoney": "Mobile money",
}


def _human_moment(value) -> str:
    """'31 October 2026, 19:06' in Lagos time — what the reader sees on screen."""
    if not value:
        return ""
    try:
        moment = value if hasattr(value, "astimezone") else None
        if moment is None:
            return ""
        return moment.astimezone(ZoneInfo("Africa/Lagos")).strftime("%d %B %Y, %H:%M")
    except Exception:
        return ""


def _naira(kobo) -> float:
    return round(int(kobo or 0) / 100, 2)


def _plain_name(user) -> str:
    if user is None:
        return ""
    return (f"{user.first_name} {user.last_name}".strip()) or user.email or ""


def receipt_number(payment) -> str:
    """Short, human, and stable for the life of the payment."""
    stamp = payment.created_at.strftime("%Y%m%d") if payment.created_at else "----------"
    return f"AM-{stamp}-{str(payment.pk)[:6].upper()}"


def payment_method(payment) -> str:
    """How the payer paid, as the provider reported it.

    The provider's verification payload is what actually settled the money, so it
    wins. The bank-transfer marker is the fallback for a payment that was paid by
    transfer before the verification payload was stored.
    """
    verification = payment.verification_payload
    if isinstance(verification, dict):
        data = verification.get("data")
        if isinstance(data, dict):
            label = _METHOD_LABELS.get(str(data.get("payment_type") or "").lower())
            if label:
                return label
    payload = payment.provider_payload
    if isinstance(payload, dict) and isinstance(payload.get("bank_transfer"), dict):
        return "Bank transfer"
    return ""


def issued_at(payment):
    return (
        payment.verified_at
        or payment.held_at
        or payment.released_at
        or payment.release_requested_at
        or payment.created_at
    )


def _service_label(payment) -> str:
    booking = getattr(payment, "booking", None)
    request_obj = getattr(booking, "request", None)
    if payment.addon_order_id:
        base = getattr(getattr(request_obj, "category", None), "name", "") or ""
        return f"{base} — separate add-on" if base else "Separate add-on"
    category = getattr(request_obj, "category", None)
    return getattr(category, "name", "") or "Service booking"


def build_receipt(payment, viewer: str) -> dict:
    """One receipt, shaped for the person asking.

    The payer sees what they paid. The artisan sees what they will receive; that
    line is omitted for the payer, who has no business seeing someone else's
    payout.
    """
    available = payment.status in PAID_STATUSES
    booking = getattr(payment, "booking", None)
    artisan = getattr(payment, "artisan", None)
    payee = getattr(artisan, "business_name", "") or _plain_name(
        getattr(artisan, "user", None)
    )
    payer = _plain_name(getattr(getattr(booking, "customer", None), "user", None))
    stamp = issued_at(payment)

    amounts = {
        "service": _naira(payment.service_amount_kobo),
        "fee": _naira(payment.customer_fee_kobo),
        "total": _naira(payment.total_amount_kobo),
    }
    if viewer == "artisan":
        amounts["net_to_payee"] = _naira(payment.artisan_payout_kobo)

    return {
        "available": available,
        "number": receipt_number(payment),
        "reference": payment.reference,
        "status": payment.status,
        "status_label": STATUS_LABELS.get(payment.status, payment.get_status_display())
        if available
        else "",
        "issued_at": stamp.isoformat() if stamp else "",
        "issued_label": _human_moment(stamp),
        "currency": "NGN",
        "amounts": amounts,
        "payer_name": payer,
        "payee_name": payee,
        "service": _service_label(payment),
        "method": payment_method(payment),
        "provider_reference": str(payment.provider_transaction_id or ""),
        "booking_id": str(payment.booking_id or ""),
        "viewer": viewer,
        "generated_at": timezone.now().isoformat(),
    }


def payout_receipt_pdf(payment_id) -> bytes:
    """The receipt emailed to an artisan the moment their payout lands.

    It is built from the payment and its release record every time it is needed,
    so the email always carries the same numbers as the database.
    """
    from . import pdf_doc
    from .models import EscrowPayment, EscrowRelease

    payment = (
        EscrowPayment.objects.select_related(
            "booking__customer__user", "artisan__user", "artisan__payout_account", "addon_order"
        )
        .filter(pk=payment_id)
        .first()
    )
    if payment is None:
        raise ValueError("That payment no longer exists.")
    release = (
        EscrowRelease.objects.filter(payment=payment)
        .order_by("-completed_at", "-requested_at")
        .first()
    )
    artisan_user = getattr(getattr(payment, "artisan", None), "user", None)
    artisan_name = _plain_name(artisan_user)
    business = getattr(getattr(payment, "artisan", None), "business_name", "") or ""
    payee = business or artisan_name
    order_label = _service_label(payment)

    ops: list = []
    band = 118
    ops.append(pdf_doc.rect(0, pdf_doc.PAGE_HEIGHT - band, pdf_doc.PAGE_WIDTH, band, pdf_doc.BRAND))
    ops.append(
        pdf_doc.text(
            pdf_doc.LEFT,
            pdf_doc.PAGE_HEIGHT - 46,
            "ARTISAN MARKETPLACE PAYOUT RECEIPT",
            font="F2",
            size=10,
            rgb=pdf_doc.WHITE,
        )
    )
    ops.append(
        pdf_doc.text(
            pdf_doc.LEFT,
            pdf_doc.PAGE_HEIGHT - 78,
            "Payout receipt",
            font="F2",
            size=24,
            rgb=pdf_doc.WHITE,
        )
    )
    ops.append(
        pdf_doc.text(
            pdf_doc.LEFT,
            pdf_doc.PAGE_HEIGHT - 100,
            f"Receipt {receipt_number(payment)}",
            size=10,
            rgb=(0.93, 0.88, 0.84),
        )
    )

    y = pdf_doc.PAGE_HEIGHT - band - 54
    ops.append(
        pdf_doc.text(
            pdf_doc.LEFT,
            y,
            "PAID OUT",
            font="F2",
            size=9.5,
            rgb=pdf_doc.GREEN,
        )
    )
    y -= 34
    ops.append(
        pdf_doc.text(
            pdf_doc.LEFT,
            y,
            f"Artisan payout: {pdf_doc.money(_naira(payment.artisan_payout_kobo))}",
            font="F2",
            size=20,
            rgb=pdf_doc.INK,
        )
    )
    y -= 26
    when = getattr(release, "completed_at", None) or issued_at(payment)
    ops.append(
        pdf_doc.text(
            pdf_doc.LEFT,
            y,
            " · ".join(
                part
                for part in (
                    f"Paid to {payee}" if payee else "",
                    pdf_doc.long_date(when),
                )
                if part
            ),
            size=10,
            rgb=pdf_doc.MUTED,
        )
    )

    y -= 44
    ops.append(pdf_doc.rule(y + 12))
    ops.append(pdf_doc.text(pdf_doc.LEFT, y - 6, "PAYOUT DETAILS", font="F2", size=8.5, rgb=pdf_doc.MUTED))
    y -= 34
    rows = [
        ("Service", order_label),
        ("Customer paid", pdf_doc.money(_naira(payment.total_amount_kobo))),
        ("Platform commission", pdf_doc.money(_naira(payment.platform_commission_kobo))),
        ("Release reference", getattr(release, "reference", "") or "—"),
        ("Release status", getattr(getattr(release, "status", ""), "title", lambda: "")() or "—"),
        ("Payment reference", payment.reference),
        ("Paid to account", _masked_account(payment)),
    ]
    for index, (label, value) in enumerate(rows):
        y = pdf_doc.row(ops, y, label, value, strong=index == 1, wrap_width=44)
        if index == len(rows) - 1:
            ops.append(pdf_doc.rule(y + 12))

    # Nothing after the last reference: the document ends where the facts end.
    return pdf_doc.assemble("".join(ops).encode("latin-1", "replace"))


def _masked_account(payment) -> str:
    """Never print a full account number in an email attachment.

    The payout account stores only the last four digits next to the encrypted
    number, so this is all that can be printed — and all that should be.
    """
    last4 = ""
    try:
        account = getattr(getattr(payment, "artisan", None), "payout_account", None)
        last4 = str(getattr(account, "account_number_last4", "") or "")[-4:]
    except Exception:
        last4 = ""
    return f"\u2022\u2022\u2022\u2022 {last4}" if last4 else "registered payout account"
