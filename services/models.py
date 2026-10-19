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
    artisan = models.OneToOneField("home.ArtisanProfile", on_delete=models.CASCADE, related_name="verification")
    verification_status = models.CharField(max_length=20, choices=VERIFICATION_STATUS, default="UNVERIFIED")
    verification_method = models.CharField(max_length=10, choices=VERIFICATION_METHOD, blank=True)
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
    URGENCY_CHOICES = [
        ("LOW", "Low"),
        ("NORMAL", "Normal"),
        ("HIGH", "High"),
        ("EMERGENCY", "Emergency"),
    ]

    id = models.BigAutoField(primary_key=True)
    requester = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="service_requests")
    service = models.ForeignKey("home.Service", on_delete=models.PROTECT,related_name="service_requests",null=True, blank=True)
    category = models.ForeignKey("home.ServiceCategory", on_delete=models.PROTECT, related_name="service_requests")
    location = models.ForeignKey("home.Locations", on_delete=models.PROTECT, related_name="service_requests")
    preferred_date = models.DateField()
    preferred_time_start = models.TimeField(null=True, blank=True)
    preferred_time_end = models.TimeField(null=True, blank=True)
    preferred_time_note = models.CharField(max_length=100, blank=True, default="")
    urgency = models.CharField(max_length=20, choices=URGENCY_CHOICES, default="NORMAL",)
    budget = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True )
    description = models.TextField()
    additional_details = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["requester", "-updated_at"], name="svc_req_requester_updated_idx"),
            models.Index(fields=["category", "location"], name="svc_req_category_location_idx"),
            models.Index(fields=["preferred_date", "urgency"], name="svc_req_date_urgency_idx"),
        ]

    def __str__(self):
        return f"Service Request #{self.id} - {self.requester.email}"

# Stable module-level reference for deterministic OpenAPI enum naming.
SERVICE_REQUEST_URGENCY_CHOICES = ServiceRequest.URGENCY_CHOICES
