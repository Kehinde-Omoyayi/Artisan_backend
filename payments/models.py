import uuid

from django.conf import settings
from django.db import models

class EscrowPayment(models.Model):
    class Status(models.TextChoices):
        PAYMENT_PENDING = "PAYMENT_PENDING", "Payment Pending"
        HELD = "HELD", "Held"
        DISPUTED = "DISPUTED", "Disputed"
        RELEASE_PENDING = "RELEASE_PENDING", "Release Pending"
        RELEASED = "RELEASED", "Released"
        REFUND_PENDING = "REFUND_PENDING", "Refund Pending"
        REFUNDED = "REFUNDED", "Refunded"
        FAILED = "FAILED", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    booking = models.ForeignKey("booking.Booking", on_delete=models.PROTECT, related_name="escrow_payments")
    addon_order = models.OneToOneField("booking.BookingAdditionalOrder", on_delete=models.PROTECT, null=True, blank=True, related_name="payment")
    customer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="customer_escrow_payments")
    artisan = models.ForeignKey("home.ArtisanProfile", on_delete=models.PROTECT, related_name="artisan_escrow_payments")
    reference = models.CharField(max_length=150, unique=True)
    provider_transaction_id = models.CharField(max_length=100, blank=True, null=True)
    provider_reference = models.CharField(max_length=150, blank=True)
    provider_status = models.CharField(max_length=50, blank=True)
    service_amount_kobo = models.PositiveBigIntegerField()
    customer_fee_kobo = models.PositiveBigIntegerField(default=15_000)
    total_amount_kobo = models.PositiveBigIntegerField()
    commission_rate = models.DecimalField(max_digits=5, decimal_places=4, default=0.1000)
    platform_commission_kobo = models.PositiveBigIntegerField()
    artisan_payout_kobo = models.PositiveBigIntegerField()
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.PAYMENT_PENDING, db_index=True)
    provider_payload = models.JSONField(default=dict, blank=True)
    verification_payload = models.JSONField(default=dict, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    held_at = models.DateTimeField(null=True, blank=True)
    release_requested_at = models.DateTimeField(null=True, blank=True)
    released_at = models.DateTimeField(null=True, blank=True)
    failure_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["booking"], condition=models.Q(addon_order__isnull=True), name="one_base_payment_per_booking"),
            models.CheckConstraint(condition=models.Q(service_amount_kobo__gt=0), name="payment_service_positive"),
            models.CheckConstraint(condition=models.Q(total_amount_kobo=models.F("service_amount_kobo") + models.F("customer_fee_kobo")), name="payment_total_matches_parts"),
            models.CheckConstraint(condition=models.Q(platform_commission_kobo__lte=models.F("service_amount_kobo")), name="payment_commission_within_service"),
            models.CheckConstraint(condition=models.Q(artisan_payout_kobo__lte=models.F("service_amount_kobo")), name="payment_payout_within_service"),
            models.CheckConstraint(condition=models.Q(platform_commission_kobo=models.F("service_amount_kobo") - models.F("artisan_payout_kobo")), name="payment_service_matches_payout"),
            models.CheckConstraint(condition=models.Q(commission_rate__gte=0) & models.Q(commission_rate__lte=0.1), name="payment_commission_rate_bounds"),
        ]
        indexes = [
            models.Index(fields=["status", "updated_at"], name="payment_status_updated_idx"),
            models.Index(fields=["provider_transaction_id"], name="payment_provider_tx_idx"),
            models.Index(fields=["provider_reference"], name="payment_provider_ref_idx"),
        ]

    def __str__(self):
        return self.reference

class PaymentLedgerEntry(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    payment = models.ForeignKey(EscrowPayment, on_delete=models.PROTECT, related_name="ledger_entries")
    event_key = models.CharField(max_length=255, unique=True)
    event_type = models.CharField(max_length=80)
    status_before = models.CharField(max_length=30, blank=True)
    status_after = models.CharField(max_length=30, blank=True)
    amount_kobo = models.PositiveBigIntegerField(null=True, blank=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="payment_ledger_actions")
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [models.Index(fields=["payment", "created_at"], name="payment_ledger_created_idx"), models.Index(fields=["event_type", "created_at"], name="payment_ledger_type_idx")]

    def save(self, *args, **kwargs):
        if self.pk and PaymentLedgerEntry.objects.filter(pk=self.pk).exists(): raise ValueError("Payment ledger entries are immutable.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Payment ledger entries cannot be deleted.")


class PaymentLedgerLine(models.Model):
    class Direction(models.TextChoices):
        DEBIT = "DEBIT", "Debit"
        CREDIT = "CREDIT", "Credit"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    entry = models.ForeignKey(PaymentLedgerEntry, on_delete=models.PROTECT, related_name="lines")
    account = models.CharField(max_length=60)
    direction = models.CharField(max_length=6, choices=Direction.choices)
    amount_kobo = models.PositiveBigIntegerField()
    currency = models.CharField(max_length=3, default="NGN")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(amount_kobo__gt=0), name="payment_ledger_line_positive"), models.CheckConstraint(condition=models.Q(currency="NGN"), name="payment_ledger_line_ngn_only")]
        indexes = [models.Index(fields=["entry", "account"], name="payment_ledger_line_entry_idx")]

    def save(self, *args, **kwargs):
        if self.pk and PaymentLedgerLine.objects.filter(pk=self.pk).exists(): raise ValueError("Payment ledger lines are immutable.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Payment ledger lines cannot be deleted.")


class EscrowRelease(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        SUBMITTED = "SUBMITTED", "Submitted"
        SUCCESSFUL = "SUCCESSFUL", "Successful"
        FAILED = "FAILED", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    payment = models.OneToOneField(EscrowPayment, on_delete=models.PROTECT, related_name="release")
    reference = models.CharField(max_length=150, unique=True)
    amount_kobo = models.PositiveBigIntegerField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True)
    provider_transfer_id = models.CharField(max_length=100, blank=True, null=True)
    provider_reference = models.CharField(max_length=150, blank=True)
    provider_payload = models.JSONField(default=dict, blank=True)
    requested_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    failure_reason = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "updated_at"], name="release_status_updated_idx"),
            models.Index(fields=["provider_transfer_id"], name="release_provider_tx_idx"),
        ]

    def __str__(self):
        return self.reference

class PaymentWebhookEvent(models.Model):
    provider = models.CharField(max_length=50)
    event_key = models.CharField(max_length=255, unique=True)
    event_type = models.CharField(max_length=100, blank=True)
    payload = models.JSONField(default=dict)
    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["provider", "processed_at"], name="wh_provider_processed_idx"),
        ]

    def __str__(self):
        return f"{self.provider}:{self.event_key}"

class ArtisanPayoutAccount(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        VERIFIED = "VERIFIED", "Verified"
        SUSPENDED = "SUSPENDED", "Suspended"

    artisan = models.OneToOneField("home.ArtisanProfile", on_delete=models.PROTECT, related_name="payout_account")
    bank_code = models.CharField(max_length=20)
    bank_name = models.CharField(max_length=150, blank=True)
    account_number_last4 = models.CharField(max_length=4)
    # The full number is retained only while a finance verification is pending,
    # and is encrypted with the existing KYC Fernet key. It is cleared on success.
    account_number_encrypted = models.TextField(blank=True)
    account_name = models.CharField(max_length=150)
    provider_beneficiary_id = models.PositiveBigIntegerField(null=True, blank=True)
    provider_subaccount_id = models.CharField(max_length=100, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    is_active = models.BooleanField(default=False)
    is_verified = models.BooleanField(default=False)
    # Changing the payout account is locked for a cooldown window after every
    # successful save; the first save starts the window too.
    payout_change_locked_until = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.artisan} ****{self.account_number_last4}"
