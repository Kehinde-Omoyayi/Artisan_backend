import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.db.models import F, Q


def duration_timedelta(value, unit):
    """Canonical agreed-service-duration → timedelta mapping (server-side only)."""
    if unit == "HOURS":
        return timedelta(hours=int(value))
    if unit == "WEEKS":
        return timedelta(weeks=int(value))
    return timedelta(days=int(value))


def duration_label(value, unit):
    label = {"HOURS": "hour", "DAYS": "day", "WEEKS": "week"}.get(unit, "day")
    return f"{value} {label}{'' if value == 1 else 's'}"


class Booking(models.Model):
    class Status(models.TextChoices):
        REQUESTED = "REQUESTED", "Requested"
        ACCEPTED = "ACCEPTED", "Accepted"
        IN_PROGRESS = "IN_PROGRESS", "In Progress"
        COMPLETION_REQUESTED = "COMPLETION_REQUESTED", "Completion Requested"
        COMPLETED = "COMPLETED", "Completed"
        CANCELLED = "CANCELLED", "Cancelled"
        REJECTED = "REJECTED", "Rejected"
        DISPUTED = "DISPUTED", "Disputed"

    STATUS_CHOICES = Status.choices

    SOURCE_CHOICES = [
        ("WEB", "Web"),
        ("MOBILE", "Mobile"),
        ("ADMIN", "Admin"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey("services.ServiceRequest", on_delete=models.PROTECT, related_name="booking")
    artisan = models.ForeignKey("home.ArtisanProfile", on_delete=models.PROTECT, related_name="booking")
    customer = models.ForeignKey("home.CustomerProfile", on_delete=models.PROTECT, related_name="booking")
    scheduled_time_start = models.DateTimeField()
    scheduled_time_end = models.DateTimeField()
    price_agreed = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=25, choices=Status.choices, default=Status.REQUESTED, db_index=True)
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default="WEB")

    artisan_completed_at = models.DateTimeField(null=True, blank=True, editable=False)
    customer_completed_at = models.DateTimeField(null=True, blank=True, editable=False)
    payout_eligible_at = models.DateTimeField(null=True, blank=True, editable=False)
    finalized_at = models.DateTimeField(null=True, blank=True, editable=False, db_index=True)
    # Agreed duration copied from the accepted offer when the booking is created.
    # The countdown starts only when the assigned artisan records the actual
    # service start; countdown_ends_at = actual_started_at + duration and never
    # resets while the booking stays in progress.
    service_duration_value = models.PositiveIntegerField(default=1)
    service_duration_unit = models.CharField(
        max_length=8,
        choices=[("HOURS", "Hours"), ("DAYS", "Days"), ("WEEKS", "Weeks")],
        default="DAYS",
    )
    actual_started_at = models.DateTimeField(null=True, blank=True, editable=False)
    countdown_ends_at = models.DateTimeField(null=True, blank=True, editable=False, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(scheduled_time_end__gt=F("scheduled_time_start")), name="booking_valid_schedule"),
            models.CheckConstraint(condition=Q(price_agreed__gt=0), name="booking_positive_price"),
            models.UniqueConstraint(
                fields=["request"],
                condition=Q(status__in=["REQUESTED", "ACCEPTED", "IN_PROGRESS", "COMPLETION_REQUESTED", "DISPUTED"]),
                name="one_active_booking_per_request",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "scheduled_time_start"], name="booking_status_start_idx"),
            models.Index(fields=["customer", "status"], name="booking_customer_status_idx"),
            models.Index(fields=["artisan", "status"], name="booking_artisan_status_idx"),
        ]

    @property
    def escrow_payment(self):
        """Return the original booking payment, excluding any separately billed add-on orders."""
        prefetched = getattr(self, "_prefetched_objects_cache", {}).get("escrow_payments")
        if prefetched is not None:
            return next((payment for payment in prefetched if payment.addon_order_id is None), None)
        return self.escrow_payments.filter(addon_order__isnull=True).order_by("created_at").first()

    def __str__(self):
        return f"Booking {self.id} - {self.status}"


class BookingAdditionalOrder(models.Model):
    class Status(models.TextChoices):
        NEGOTIATING = "NEGOTIATING", "Negotiating"
        APPROVED = "APPROVED", "Approved"
        DECLINED = "DECLINED", "Declined"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    booking = models.ForeignKey(Booking, on_delete=models.PROTECT, related_name="additional_orders")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="additional_orders_created")
    accepted_offer = models.OneToOneField(
        "booking.BookingAdditionalOffer", on_delete=models.PROTECT, null=True, blank=True,
        related_name="accepted_order",
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.NEGOTIATING, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["booking", "-created_at"], name="booking_additional_latest_idx"),
            models.Index(fields=["status", "updated_at"], name="booking_additional_status_idx"),
        ]

    def __str__(self):
        return f"Additional order {self.id} for booking {self.booking_id}"


class BookingAdditionalOffer(models.Model):
    class Status(models.TextChoices):
        PROPOSED = "PROPOSED", "Proposed"
        COUNTERED = "COUNTERED", "Countered"
        ACCEPTED = "ACCEPTED", "Accepted"
        DECLINED = "DECLINED", "Declined"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(BookingAdditionalOrder, on_delete=models.PROTECT, related_name="offers")
    parent = models.ForeignKey("self", on_delete=models.PROTECT, null=True, blank=True, related_name="counters")
    revision = models.PositiveSmallIntegerField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="additional_order_offers_created")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    description = models.TextField(max_length=3000)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PROPOSED, db_index=True)
    accepted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="additional_order_offers_accepted")
    responded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["revision"]
        constraints = [
            models.UniqueConstraint(fields=["order", "revision"], name="booking_additional_offer_revision_unique"),
            models.CheckConstraint(condition=Q(amount__gt=0), name="booking_additional_offer_amount_positive"),
        ]
        indexes = [
            models.Index(fields=["order", "-revision"], name="b_add_offer_latest_idx"),
            models.Index(fields=["status", "updated_at"], name="b_add_offer_status_idx"),
        ]

    def __str__(self):
        return f"Additional order offer {self.order_id} #{self.revision} ({self.status})"


class BookingStatusHistory(models.Model):
    ACTOR_CHOICES = [
        ("CUSTOMER", "Customer"),
        ("ARTISAN", "Artisan"),
        ("ADMIN", "Admin"),
        ("SYSTEM", "System"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name="status_history")
    status = models.CharField(max_length=25, choices=Booking.Status.choices)
    changed_by = models.CharField(max_length=20, choices=ACTOR_CHOICES)
    changed_by_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="booking_status_changes",
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.booking_id} - {self.status}"

# Stable module-level references for deterministic OpenAPI enum names.
BOOKING_STATUS_CHOICES = Booking.Status.choices
