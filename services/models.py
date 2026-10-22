import uuid

from django.db import models
from django.conf import settings


class ArtisanVerification(models.Model):
    VERIFICATION_STATUS = [
        ("UNVERIFIED", "Unverified"),
        ("PENDING", "Pending"),
        ("VERIFIED", "Verified"),
        ("REJECTED", "Rejected"),
        ("SUSPENDED", "Suspended"),
    ]

    VERIFICATION_METHOD = [
        ("NIN", "NIN"),
        ("BVN", "BVN"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    artisan = models.OneToOneField(
        "home.ArtisanProfile", on_delete=models.CASCADE, related_name="verification"
    )
    verification_status = models.CharField(
        max_length=20, choices=VERIFICATION_STATUS, default="UNVERIFIED"
    )
    verification_method = models.CharField(
        max_length=10, choices=VERIFICATION_METHOD, blank=True
    )
    verification_tier = models.PositiveSmallIntegerField(default=1)
    # Provider-confirmed legal name encrypted with the existing KYC Fernet key.
    # The plaintext identity is never returned by the API or written to logs.
    verified_identity_name_encrypted = models.TextField(blank=True)
    verification_provider = models.CharField(max_length=30, blank=True)
    verification_reference = models.CharField(max_length=255, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.artisan.user.email} - {self.verification_status} - Tier {self.verification_tier}"


class ServiceRequest(models.Model):
    media_policy_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    URGENCY_CHOICES = [
        ("LOW", "Low"),
        ("NORMAL", "Normal"),
        ("HIGH", "High"),
        ("EMERGENCY", "Emergency"),
    ]

    class Status(models.TextChoices):
        """Lifecycle of a request.

        Only OPEN requests are shown to the customer and matched by artisans.
        ATTENDED is set automatically once a booking exists for the request, and
        ARCHIVED is what "delete" means: the row stays for reference and future
        analysis, but it leaves every customer-facing list.
        """

        DRAFT = "DRAFT", "Draft"
        CLOSED = "CLOSED", "Closed without booking"
        COMPLETED = "COMPLETED", "Completed"
        OPEN = "OPEN", "Open"
        ATTENDED = "ATTENDED", "Attended"
        ARCHIVED = "ARCHIVED", "Archived"

    # Statuses that are kept in the database but never shown in the frontend.
    HIDDEN_STATUSES = (
        Status.ATTENDED,
        Status.ARCHIVED,
        Status.COMPLETED,
        Status.DRAFT,
        Status.CLOSED,
    )

    id = models.BigAutoField(primary_key=True)
    requester = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="service_requests",
    )
    service = models.ForeignKey(
        "home.Service",
        on_delete=models.PROTECT,
        related_name="service_requests",
        null=True,
        blank=True,
    )
    category = models.ForeignKey(
        "home.ServiceCategory",
        on_delete=models.PROTECT,
        related_name="service_requests",
        null=True,
        blank=True,
    )
    location = models.ForeignKey(
        "home.Locations",
        on_delete=models.PROTECT,
        related_name="service_requests",
        null=True,
        blank=True,
    )
    preferred_date = models.DateField(null=True, blank=True)
    preferred_time_start = models.TimeField(null=True, blank=True)
    preferred_time_end = models.TimeField(null=True, blank=True)
    preferred_time_note = models.CharField(max_length=100, blank=True, default="")
    urgency = models.CharField(
        max_length=20,
        choices=URGENCY_CHOICES,
        default="NORMAL",
    )
    budget = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    description = models.TextField()
    additional_details = models.TextField(blank=True, default="")
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.OPEN,
    )
    closed_at = models.DateTimeField(null=True, blank=True, editable=False)
    attended_at = models.DateTimeField(null=True, blank=True)
    archived_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["requester", "-updated_at"],
                name="svc_req_requester_updated_idx",
            ),
            models.Index(
                fields=["category", "location"], name="svc_req_category_location_idx"
            ),
            models.Index(
                fields=["preferred_date", "urgency"], name="svc_req_date_urgency_idx"
            ),
            models.Index(
                fields=["requester", "status"], name="svc_req_requester_status_idx"
            ),
        ]

    def __str__(self):
        return f"Service Request #{self.id} - {self.requester.email}"

    @property
    def is_visible(self):
        """True while the request still belongs in the customer's workspace."""
        return self.status == self.Status.OPEN

    def mark_attended(self, *, when=None):
        """Hide the request from the frontend once an artisan has taken the job.

        The row is never removed: bookings, conversations and analytics keep
        pointing at it. Returns True only when the status actually changed.
        """
        from django.utils import timezone

        if self.status != self.Status.OPEN:
            return False
        self.status = self.Status.ATTENDED
        self.attended_at = when or timezone.now()
        self.save(update_fields=["status", "attended_at", "updated_at"])
        from notifications.events import notify_service_request_attended

        notify_service_request_attended(self)
        return True

    def archive(self, *, when=None):
        """Soft delete: leave every frontend list, keep the record for analysis."""
        from django.utils import timezone

        if self.status == self.Status.ARCHIVED:
            return False
        self.status = self.Status.ARCHIVED
        self.archived_at = when or timezone.now()
        self.save(update_fields=["status", "archived_at", "updated_at"])
        return True


# Stable module-level reference for deterministic OpenAPI enum naming.
SERVICE_REQUEST_URGENCY_CHOICES = ServiceRequest.URGENCY_CHOICES


class ArtisanRequestDismissal(models.Model):
    """A personal 'not interested' choice. Never closes the customer's request."""

    request = models.ForeignKey(
        ServiceRequest, on_delete=models.PROTECT, related_name="artisan_dismissals"
    )
    artisan = models.ForeignKey(
        "home.ArtisanProfile",
        on_delete=models.CASCADE,
        related_name="request_dismissals",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["request", "artisan"], name="unique_artisan_request_dismissal"
            )
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"Request #{self.request_id} hidden by artisan {self.artisan_id}"
