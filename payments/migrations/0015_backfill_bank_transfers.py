"""Move legacy provider_payload bank-transfer records into the new table.

The account the payer transfers into used to live inside
``EscrowPayment.provider_payload["bank_transfer"]`` with its expiry computed
from a local 900-second TTL instead of the provider's ``account_expiration``.
This copies every minted account into ``PaymentBankTransfer`` so nothing the
payer is holding disappears, keeps the provider's own expiry as the clock, and
marks records whose expiry already passed as EXPIRED. Records without an
account number are skipped; they were never usable.
"""

from datetime import datetime

from django.db import migrations
from django.utils import timezone
from datetime import timezone as dt_timezone

from django.utils.dateparse import parse_datetime


def _parse_provider_expiry(raw):
    text = str(raw or "").strip().replace("Z", "+00:00")
    if not text:
        return None
    value = parse_datetime(text)
    if value is None:
        for pattern in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                value = datetime.strptime(text, pattern)
                break
            except ValueError:
                value = None
    if value is None:
        return None
    if timezone.is_naive(value):
        value = timezone.make_aware(value, dt_timezone.utc)
    return value


def backfill(apps, schema_editor):
    EscrowPayment = apps.get_model("payments", "EscrowPayment")
    PaymentBankTransfer = apps.get_model("payments", "PaymentBankTransfer")
    now = timezone.now()
    for payment in EscrowPayment.objects.all().iterator():
        payload = payment.provider_payload if isinstance(payment.provider_payload, dict) else {}
        record = payload.get("bank_transfer")
        if not isinstance(record, dict):
            continue
        account_number = str(record.get("account_number") or "").strip()
        if not account_number:
            continue
        if PaymentBankTransfer.objects.filter(payment=payment).exists():
            continue
        expires = _parse_provider_expiry(record.get("expires_at")) or _parse_provider_expiry(record.get("account_expiration"))
        minted = _parse_provider_expiry(record.get("minted_at"))
        from datetime import timedelta

        if expires is None:
            # No provider expiry survived: keep the old local semantics rather
            # than inventing one. Such an account is treated as expired and a
            # fresh one is minted against a rotated reference on next request.
            expires = (minted + timedelta(seconds=900)) if minted else now
        try:
            amount_kobo = int(round(float(str(record.get("amount") or "0").replace(",", "")) * 100)) or None
        except (TypeError, ValueError):
            amount_kobo = None
        status = "READY" if expires > now else "EXPIRED"
        if payment.status != "PAYMENT_PENDING":
            status = "PAID" if payment.status in {"HELD", "DISPUTED", "RELEASE_PENDING", "RELEASED"} else "EXPIRED"
        PaymentBankTransfer.objects.create(
            payment=payment,
            status=status,
            reference=payment.reference,
            transfer_reference=str(record.get("transfer_reference") or "")[:150],
            transfer_account=account_number[:34],
            transfer_bank=str(record.get("bank") or "")[:150],
            beneficiary_name=str(record.get("beneficiary_name") or "")[:150],
            payer_name=str(record.get("payer_name") or "")[:150],
            narration=str(record.get("narration") or "")[:200],
            transfer_note=str(record.get("note") or "")[:200],
            transfer_amount_kobo=amount_kobo,
            mode=str(record.get("mode") or "")[:40],
            account_expires_at=expires,
            minted_at=minted,
            creation_started_at=minted,
            attempts=1,
        )


def reverse_backfill(apps, schema_editor):
    # The rows are a copy; the canonical record is being rebuilt. Deleting on
    # reverse keeps the migration honest without touching EscrowPayment.
    apps.get_model("payments", "PaymentBankTransfer").objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("payments", "0014_paymentbanktransfer"),
    ]

    operations = [
        migrations.RunPython(backfill, reverse_backfill),
    ]
