from django.db import transaction

from .models import PaymentLedgerEntry, PaymentLedgerLine


def record_financial_event(*, payment, event_type, event_key, status_before="", status_after="", actor=None, amount_kobo=None, metadata=None):
    metadata = metadata or {}
    with transaction.atomic():
        entry, created = PaymentLedgerEntry.objects.get_or_create(event_key=event_key, defaults={"payment": payment, "event_type": event_type, "status_before": status_before or "", "status_after": status_after or "", "actor": actor, "amount_kobo": amount_kobo, "metadata": metadata})
        if not created: return entry
        amount = int(amount_kobo or 0)
        if amount <= 0: return entry
        if event_type == "ESCROW_HELD":
            payout = int(payment.artisan_payout_kobo); commission = int(payment.platform_commission_kobo); fee = int(payment.customer_fee_kobo)
            PaymentLedgerLine.objects.create(entry=entry, account="ESCROW_FUNDS", direction=PaymentLedgerLine.Direction.DEBIT, amount_kobo=amount)
            PaymentLedgerLine.objects.create(entry=entry, account="ARTISAN_PAYABLE", direction=PaymentLedgerLine.Direction.CREDIT, amount_kobo=payout)
            if commission: PaymentLedgerLine.objects.create(entry=entry, account="PLATFORM_COMMISSION_REVENUE", direction=PaymentLedgerLine.Direction.CREDIT, amount_kobo=commission)
            if fee: PaymentLedgerLine.objects.create(entry=entry, account="PLATFORM_FEE_REVENUE", direction=PaymentLedgerLine.Direction.CREDIT, amount_kobo=fee)
        elif event_type == "PAYOUT_COMPLETED":
            PaymentLedgerLine.objects.create(entry=entry, account="ARTISAN_PAYABLE", direction=PaymentLedgerLine.Direction.DEBIT, amount_kobo=amount)
            PaymentLedgerLine.objects.create(entry=entry, account="ESCROW_FUNDS", direction=PaymentLedgerLine.Direction.CREDIT, amount_kobo=amount)
        elif event_type in {"REFUNDED", "PARTIAL_REFUND_COMPLETED"}:
            PaymentLedgerLine.objects.create(entry=entry, account="CUSTOMER_REFUND_SETTLEMENT", direction=PaymentLedgerLine.Direction.DEBIT, amount_kobo=amount)
            PaymentLedgerLine.objects.create(entry=entry, account="ESCROW_FUNDS", direction=PaymentLedgerLine.Direction.CREDIT, amount_kobo=amount)
        else:
            PaymentLedgerLine.objects.create(entry=entry, account="MARKETPLACE_ACTIVITY", direction=PaymentLedgerLine.Direction.DEBIT, amount_kobo=amount)
            PaymentLedgerLine.objects.create(entry=entry, account="MARKETPLACE_ACTIVITY_OFFSET", direction=PaymentLedgerLine.Direction.CREDIT, amount_kobo=amount)
        return entry


def assert_ledger_entry_balanced(entry):
    lines = list(entry.lines.all())
    if not lines: raise ValueError(f"Financial ledger entry {entry.pk} has no ledger lines.")
    if any(line.currency != "NGN" for line in lines): raise ValueError(f"Financial ledger entry {entry.pk} has a non-NGN ledger line.")
    debit = sum(int(row.amount_kobo) for row in lines if row.direction == PaymentLedgerLine.Direction.DEBIT)
    credit = sum(int(row.amount_kobo) for row in lines if row.direction == PaymentLedgerLine.Direction.CREDIT)
    if debit != credit: raise ValueError(f"Financial ledger entry {entry.pk} is unbalanced: debit={debit}, credit={credit}.")
    return True


def assert_payment_ledger_consistent(payment):
    entries = list(payment.ledger_entries.prefetch_related("lines").all())
    for entry in entries: assert_ledger_entry_balanced(entry)
    held = sum(int(entry.amount_kobo or 0) for entry in entries if entry.event_type == "ESCROW_HELD")
    payouts = sum(int(entry.amount_kobo or 0) for entry in entries if entry.event_type == "PAYOUT_COMPLETED")
    refunds = sum(int(entry.amount_kobo or 0) for entry in entries if entry.event_type in {"REFUNDED", "PARTIAL_REFUND_COMPLETED"})
    if held and held != int(payment.total_amount_kobo): raise ValueError(f"Payment {payment.pk} escrow ledger amount does not match the payment total.")
    if payouts > int(payment.artisan_payout_kobo): raise ValueError(f"Payment {payment.pk} has payout ledger entries above the authorized artisan payout.")
    if refunds > int(payment.total_amount_kobo): raise ValueError(f"Payment {payment.pk} has refund ledger entries above the customer payment.")
    if payouts + refunds > max(held, int(payment.total_amount_kobo)): raise ValueError(f"Payment {payment.pk} has payout/refund activity above held funds.")
    return True
