import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class CustomerRiskProfile(models.Model):
    LEVEL_CHOICES = [
        ("NORMAL", "Normal"),
        ("WARNING", "Warning"),
        ("REVIEW", "Review"),
        ("SUSPENDED", "Suspended"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    customer = models.OneToOneField(
        "home.CustomerProfile",
        on_delete=models.CASCADE,
        related_name="risk_profile",
    )
    cancellation_points = models.PositiveSmallIntegerField(default=0)
    cancellation_count = models.PositiveIntegerField(default=0)
    eligible_booking_count = models.PositiveIntegerField(default=0)
    weighted_cancellation_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0,
    )
    risk_level = models.CharField(
        max_length=10,
        choices=LEVEL_CHOICES,
        default="NORMAL",
    )
    last_evaluated_at = models.DateTimeField(null=True, blank=True)
    suspended_at = models.DateTimeField(null=True, blank=True)
    suspension_reason = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["risk_level", "updated_at"], name="risk_level_updated_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(cancellation_points__gte=0) & Q(cancellation_points__lte=5),
                name="risk_score_0_5",
            ),
            models.CheckConstraint(
                condition=Q(weighted_cancellation_rate__gte=0) & Q(weighted_cancellation_rate__lte=100),
                name="risk_rate_0_100",
            ),
        ]

    def __str__(self):
        return f"{self.customer.user.email} cancellation risk {self.cancellation_points}/5"


class CustomerCancellationEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    customer = models.ForeignKey(
        "home.CustomerProfile",
        on_delete=models.PROTECT,
        related_name="cancellation_events",
    )
    booking = models.OneToOneField(
        "booking.Booking",
        on_delete=models.PROTECT,
        related_name="customer_cancellation_event",
    )
    reason_code = models.CharField(max_length=50)
    reason_text = models.TextField(blank=True)
    severity = models.PositiveSmallIntegerField(default=0)
    points = models.PositiveSmallIntegerField(default=0)
    customer_attributable = models.BooleanField(default=False)
    requires_review = models.BooleanField(default=False)
    ip_hash = models.CharField(max_length=64, blank=True)
    occurred_at = models.DateTimeField(auto_now_add=True)
    scheduled_time_start = models.DateTimeField()

    class Meta:
        indexes = [
            models.Index(fields=["customer", "occurred_at"], name="cancel_customer_time_idx"),
            models.Index(fields=["ip_hash", "occurred_at"], name="cancel_ip_time_idx"),
            models.Index(fields=["reason_code", "occurred_at"], name="cancel_reason_time_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(severity__gte=0) & Q(severity__lte=5),
                name="cancel_severity_0_5",
            ),
            models.CheckConstraint(
                condition=Q(points__gte=0) & Q(points__lte=5),
                name="cancel_points_0_5",
            ),
        ]

    def __str__(self):
        return f"Cancellation {self.booking_id} {self.points}/5"


class CustomerNetworkObservation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    customer = models.ForeignKey(
        "home.CustomerProfile",
        on_delete=models.PROTECT,
        related_name="network_observations",
    )
    ip_hash = models.CharField(max_length=64)
    observation_type = models.CharField(max_length=30, default="ACTIVITY")
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)
    observation_count = models.PositiveIntegerField(default=1)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["customer", "ip_hash"],
                name="unique_customer_ip_observation",
            )
        ]
        indexes = [
            models.Index(fields=["ip_hash", "last_seen_at"], name="network_ip_last_seen_idx"),
            models.Index(fields=["customer", "last_seen_at"], name="network_customer_last_seen_idx"),
        ]

    def __str__(self):
        return f"{self.customer.user.email} {self.ip_hash[:10]}"


class CustomerNetworkBlock(models.Model):
    SCOPE_CHOICES = [
        ("AUTH_BOOKING", "Authentication and booking"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ip_hash = models.CharField(max_length=64, unique=True)
    scope = models.CharField(max_length=20, choices=SCOPE_CHOICES, default="AUTH_BOOKING")
    reason = models.CharField(max_length=255)
    confidence = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    block_count = models.PositiveIntegerField(default=1)
    blocked_until = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["ip_hash", "blocked_until"], name="network_block_active_idx"),
            models.Index(fields=["blocked_until"], name="network_block_expiry_idx"),
        ]

    def __str__(self):
        return f"Network block {self.ip_hash[:10]} until {self.blocked_until.isoformat()}"
