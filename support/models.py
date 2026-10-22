import uuid

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import models


class SupportCase(models.Model):

    class CaseType(models.TextChoices):
        DISPUTE = "DISPUTE", "Dispute"
        REPORT = "REPORT", "Report"
        PAYMENT = "PAYMENT", "Payment"
        ACCOUNT = "ACCOUNT", "Account"
        TECHNICAL = "TECHNICAL", "Technical"
        OTHER = "OTHER", "Other"

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        UNDER_REVIEW = "UNDER_REVIEW", "Under review"
        RESOLVED = "RESOLVED", "Resolved"
        CLOSED = "CLOSED", "Closed"

    class Priority(models.TextChoices):
        LOW = "LOW", "Low"
        MEDIUM = "MEDIUM", "Medium"
        HIGH = "HIGH", "High"
        URGENT = "URGENT", "Urgent"

    class ResolutionCode(models.TextChoices):
        REFUND = "REFUND", "Refund"
        PAYOUT = "PAYOUT", "Payout"
        PARTIAL_REFUND = "PARTIAL_REFUND", "Partial refund"
        PARTIAL_PAYOUT = "PARTIAL_PAYOUT", "Partial payout"
        REBOOK = "REBOOK", "Rebook"
        REPERFORM = "REPERFORM", "Re-perform service"
        NO_ADJUSTMENT = "NO_ADJUSTMENT", "No financial adjustment"
        POLICY_ACTION = "POLICY_ACTION", "Policy action"
        OTHER = "OTHER", "Other"

    id = models.BigAutoField(primary_key=True)
    reference = models.CharField(
        max_length=32,
        unique=True,
        editable=False,
    )

    booking = models.ForeignKey(
        "booking.Booking",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="support_cases",
    )

    conversation = models.ForeignKey(
        "conversations.Conversation",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="support_cases",
    )

    promotion_campaign = models.ForeignKey(
        "promotions.PromotionCampaign",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="support_cases",
    )

    opened_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="support_cases_opened",
    )

    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="support_cases_assigned",
    )

    case_type = models.CharField(
        max_length=30,
        choices=CaseType.choices,
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.OPEN,
    )

    priority = models.CharField(
        max_length=20,
        choices=Priority.choices,
        default=Priority.MEDIUM,
    )

    subject = models.CharField(max_length=200)
    description = models.TextField()

    resolution_code = models.CharField(
        max_length=30,
        choices=ResolutionCode.choices,
        null=True,
        blank=True,
    )

    resolution_note = models.TextField(blank=True)

    resolved_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "priority"]),
            models.Index(fields=["opened_by", "status"]),
            models.Index(fields=["booking", "status"]),
            models.Index(fields=["assigned_to", "status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["booking", "case_type"],
                condition=models.Q(
                    case_type="DISPUTE", status__in=["OPEN", "UNDER_REVIEW"]
                ),
                name="support_one_active_dispute_per_booking",
            ),
            models.CheckConstraint(
                condition=~(
                    models.Q(booking__isnull=False)
                    & models.Q(promotion_campaign__isnull=False)
                ),
                name="support_case_one_business_resource",
            ),
            models.CheckConstraint(
                condition=~(
                    models.Q(conversation__isnull=False)
                    & models.Q(promotion_campaign__isnull=False)
                ),
                name="support_case_conversation_not_promotion",
            ),
        ]

    def save(self, *args, **kwargs):
        from django.db import transaction
        from services.models import ServiceRequest

        request_id = (
            self.booking.request_id
            if self.booking_id
            else (
                self.conversation.service_request_id if self.conversation_id else None
            )
        )
        with transaction.atomic():
            if request_id:
                ServiceRequest.objects.select_for_update().get(pk=request_id)
            return self._save_validated(*args, **kwargs)

    def _save_validated(self, *args, **kwargs):
        if (
            self._state.adding
            and self.case_type == self.CaseType.DISPUTE
            and self.booking_id is None
        ):
            raise DjangoValidationError(
                {"booking": "A dispute case must reference a booking."}
            )
        if self.conversation_id:
            if self.promotion_campaign_id:
                raise DjangoValidationError(
                    {
                        "conversation": "A support case can reference a conversation or a promotion campaign, not both."
                    }
                )
            conversation = self.conversation
            is_customer = conversation.customer_id == self.opened_by_id
            is_artisan = bool(
                conversation.artisan_id
                and conversation.artisan.user_id == self.opened_by_id
            )
            is_generic_participant = conversation.participants.filter(
                pk=self.opened_by_id
            ).exists()
            if not (is_customer or is_artisan or is_generic_participant):
                raise DjangoValidationError(
                    {
                        "conversation": "The case opener must participate in the linked conversation."
                    }
                )
            if self.booking_id:
                booking = self.booking
                matches_booking = (
                    conversation.conversation_type == "SERVICE"
                    and conversation.service_request_id == booking.request_id
                    and conversation.customer_id == booking.customer.user_id
                    and conversation.artisan_id == booking.artisan_id
                    and booking.request.requester_id == booking.customer.user_id
                )
                if not matches_booking:
                    raise DjangoValidationError(
                        {
                            "conversation": "The conversation does not match the linked booking's customer, artisan, and service request."
                        }
                    )
        if not self.reference:
            self.reference = f"CASE-{uuid.uuid4().hex[:12].upper()}"
        super().save(*args, **kwargs)

    def __str__(self):
        return self.reference


class SupportFinancialAction(models.Model):

    class ActionType(models.TextChoices):
        REFUND = "REFUND", "Refund"
        PARTIAL_REFUND = "PARTIAL_REFUND", "Partial refund"

    class Status(models.TextChoices):
        REQUESTED = "REQUESTED", "Requested"
        APPROVED = "APPROVED", "Approved"
        PROCESSING = "PROCESSING", "Processing"
        PROCESSING_UNKNOWN = (
            "PROCESSING_UNKNOWN",
            "Processing outcome unknown",
        )
        SUCCEEDED = "SUCCEEDED", "Succeeded"
        FAILED = "FAILED", "Failed"
        REJECTED = "REJECTED", "Rejected"

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    reference = models.CharField(
        max_length=60,
        unique=True,
        editable=False,
    )

    case = models.ForeignKey(
        SupportCase,
        on_delete=models.PROTECT,
        related_name="financial_actions",
    )

    payment = models.ForeignKey(
        "payments.EscrowPayment",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="support_financial_actions",
    )

    promotion_payment = models.ForeignKey(
        "promotions.PromotionPayment",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="support_financial_actions",
    )

    action_type = models.CharField(
        max_length=30,
        choices=ActionType.choices,
    )

    amount_kobo = models.PositiveBigIntegerField()
    reason = models.TextField()
    payment_status_before = models.CharField(max_length=30, blank=True, default="")

    idempotency_key = models.CharField(
        max_length=255,
        unique=True,
    )

    status = models.CharField(
        max_length=25,
        choices=Status.choices,
        default=Status.REQUESTED,
        db_index=True,
    )

    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="support_financial_actions_requested",
    )

    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="support_financial_actions_approved",
    )

    executed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="support_financial_actions_executed",
    )

    provider_refund_id = models.CharField(
        max_length=100,
        blank=True,
        null=True,
    )

    provider_ref = models.CharField(max_length=150, blank=True)
    provider_status = models.CharField(max_length=60, blank=True)
    provider_payload = models.JSONField(default=dict, blank=True)
    failure_reason = models.TextField(blank=True)

    requested_at = models.DateTimeField(auto_now_add=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-requested_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount_kobo__gt=0),
                name="support_financial_action_positive_amount",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(payment__isnull=False, promotion_payment__isnull=True)
                    | models.Q(payment__isnull=True, promotion_payment__isnull=False)
                ),
                name="support_financial_action_one_payment",
            ),
        ]
        indexes = [
            models.Index(
                fields=["case", "status"],
                name="support_fin_case_status_idx",
            ),
            models.Index(
                fields=["payment", "status"],
                name="support_fin_payment_status_idx",
            ),
            models.Index(
                fields=["promotion_payment", "status"],
                name="sup_fin_promo_status_idx",
            ),
            models.Index(
                fields=["provider_refund_id"],
                name="sup_fin_provider_ref_idx",
            ),
            models.Index(
                fields=["status", "-requested_at"],
                name="sup_fin_status_requested_idx",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self.reference:
            self.reference = f"RFND-{uuid.uuid4().hex[:16].upper()}"
        super().save(*args, **kwargs)

    def __str__(self):
        return self.reference


class SupportCaseEvent(models.Model):

    case = models.ForeignKey(
        SupportCase,
        on_delete=models.PROTECT,
        related_name="events",
    )

    action = models.CharField(max_length=50)
    note = models.TextField(blank=True)

    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="support_case_events",
    )

    metadata = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(
                fields=["case", "created_at"], name="support_event_case_created_idx"
            )
        ]

    def __str__(self):
        return f"{self.case.reference} • {self.action}"


# Stable module-level reference for deterministic OpenAPI enum naming.
SUPPORT_FINANCIAL_ACTION_STATUS_CHOICES = SupportFinancialAction.Status.choices
SUPPORT_CASE_STATUS_CHOICES = SupportCase.Status.choices
