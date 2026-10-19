import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class PromotionApplication(models.Model):
    class PromotionType(models.TextChoices):
        INSTITUTION = "INSTITUTION", "Institution Promotion"
        AREA = "AREA", "Area Promotion"
        STATE = "STATE", "State Promotion"
        HOMEPAGE = "HOMEPAGE", "Homepage Feature"

    class Status(models.TextChoices):
        PENDING_REVIEW = "PENDING_REVIEW", "Pending Review"
        CHANGES_REQUIRED = "CHANGES_REQUIRED", "Changes Required"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"
        WITHDRAWN = "WITHDRAWN", "Withdrawn"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    artisan = models.ForeignKey(
        "home.ArtisanProfile",
        on_delete=models.CASCADE,
        related_name="promotion_applications",
    )
    promotion_type = models.CharField(max_length=20, choices=PromotionType.choices)
    institution = models.ForeignKey(
        "home.Institution",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="promotion_applications",
    )
    area_location = models.ForeignKey(
        "home.Locations",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="area_promotion_applications",
    )
    state_country = models.CharField(max_length=100, blank=True, default="")
    state_name = models.CharField(max_length=100, blank=True, default="")
    services = models.ManyToManyField(
        "home.ArtisanService",
        related_name="promotion_applications",
    )
    promotional_description = models.TextField(max_length=1200)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING_REVIEW,
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    last_reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="promotion_applications_reviewed",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-submitted_at", "-created_at"]
        indexes = [
            models.Index(fields=["status", "-submitted_at"], name="promo_status_submit_idx"),
            models.Index(fields=["artisan", "-created_at"], name="promo_artisan_created_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(
                        promotion_type="INSTITUTION",
                        institution__isnull=False,
                        area_location__isnull=True,
                        state_country="",
                        state_name="",
                    )
                    | Q(
                        promotion_type="AREA",
                        institution__isnull=True,
                        area_location__isnull=False,
                        state_country="",
                        state_name="",
                    )
                    | Q(
                        promotion_type="STATE",
                        institution__isnull=True,
                        area_location__isnull=True,
                    )
                    & ~Q(state_country="")
                    & ~Q(state_name="")
                    | Q(
                        promotion_type="HOMEPAGE",
                        institution__isnull=True,
                        area_location__isnull=True,
                        state_country="",
                        state_name="",
                    )
                ),
                name="promotion_target_matches_type",
            ),
            models.UniqueConstraint(
                fields=["artisan", "institution"],
                condition=Q(
                    promotion_type="INSTITUTION",
                    status__in=["PENDING_REVIEW", "CHANGES_REQUIRED", "APPROVED"],
                ),
                name="promo_institution_open_uniq",
            ),
            models.UniqueConstraint(
                fields=["artisan", "area_location"],
                condition=Q(
                    promotion_type="AREA",
                    status__in=["PENDING_REVIEW", "CHANGES_REQUIRED", "APPROVED"],
                ),
                name="promo_area_open_uniq",
            ),
            models.UniqueConstraint(
                fields=["artisan", "state_country", "state_name"],
                condition=Q(
                    promotion_type="STATE",
                    status__in=["PENDING_REVIEW", "CHANGES_REQUIRED", "APPROVED"],
                ),
                name="promo_state_open_uniq",
            ),
            models.UniqueConstraint(
                fields=["artisan"],
                condition=Q(
                    promotion_type="HOMEPAGE",
                    status__in=["PENDING_REVIEW", "CHANGES_REQUIRED", "APPROVED"],
                ),
                name="promo_homepage_open_uniq",
            ),
        ]

    @property
    def latest_revision(self):
        cached = getattr(self, "_prefetched_latest_revision", None)
        if cached is not None:
            return cached
        return self.revisions.order_by("-revision_number").first()

    @property
    def target_label(self):
        if self.promotion_type == self.PromotionType.INSTITUTION and self.institution_id:
            institution = self.institution
            label = institution.name
            if institution.campus:
                label = f"{label} — {institution.campus}"
            return f"{label} · {institution.location}"
        if self.promotion_type == self.PromotionType.AREA and self.area_location_id:
            return str(self.area_location)
        if self.promotion_type == self.PromotionType.STATE:
            return f"{self.state_name}, {self.state_country}"
        return "Homepage Feature"

    def __str__(self):
        return f"{self.get_promotion_type_display()} for {self.target_label} ({self.get_status_display()})"


class PromotionApplicationRevision(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey(
        PromotionApplication,
        on_delete=models.CASCADE,
        related_name="revisions",
    )
    revision_number = models.PositiveSmallIntegerField()
    snapshot = models.JSONField(default=dict)
    submitted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["revision_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["application", "revision_number"],
                name="promo_revision_number_uniq",
            ),
        ]
        indexes = [models.Index(fields=["application", "-submitted_at"], name="promo_revision_submit_idx")]

    def __str__(self):
        return f"{self.application_id} · revision {self.revision_number}"


class PromotionApplicationEvidence(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    revision = models.ForeignKey(
        PromotionApplicationRevision,
        on_delete=models.CASCADE,
        related_name="evidence",
    )
    image = models.ImageField(upload_to="promotion-evidence/%Y/%m/")
    caption = models.CharField(max_length=240, blank=True, default="")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["uploaded_at", "id"]
        indexes = [models.Index(fields=["revision", "uploaded_at"], name="promo_evidence_revision_idx")]

    def __str__(self):
        return f"Work sample for application revision {self.revision_id}"


class PromotionApplicationDecision(models.Model):
    class DecisionType(models.TextChoices):
        APPROVED = "APPROVED", "Approved"
        CHANGES_REQUIRED = "CHANGES_REQUIRED", "Changes Required"
        REJECTED = "REJECTED", "Rejected"
        WITHDRAWN = "WITHDRAWN", "Withdrawn"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey(
        PromotionApplication,
        on_delete=models.CASCADE,
        related_name="decisions",
    )
    revision = models.ForeignKey(
        PromotionApplicationRevision,
        on_delete=models.PROTECT,
        related_name="decisions",
    )
    decision_type = models.CharField(max_length=20, choices=DecisionType.choices)
    reason = models.TextField(max_length=2000, blank=True, default="")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="promotion_application_actions",
    )
    actor_name_snapshot = models.CharField(max_length=160, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["application", "-created_at"], name="promo_decision_created_idx")]

    def __str__(self):
        return f"{self.get_decision_type_display()} · {self.application_id}"


class PromotionPlan(models.Model):
    """Manager-controlled price, duration and terms for one promotion product."""

    class RefundPolicy(models.TextChoices):
        PRO_RATA = "PRO_RATA", "Pro-rata for platform-initiated termination"
        NONE = "NONE", "No refund except where required by law"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    promotion_type = models.CharField(max_length=20, choices=PromotionApplication.PromotionType.choices, unique=True)
    name = models.CharField(max_length=120)
    is_enabled = models.BooleanField(default=False)
    price_kobo = models.PositiveBigIntegerField(default=0)
    currency = models.CharField(max_length=3, default="NGN")
    duration_days = models.PositiveSmallIntegerField(default=7)
    refund_policy = models.CharField(max_length=20, choices=RefundPolicy.choices, default=RefundPolicy.PRO_RATA)
    terms_text = models.TextField(blank=True, default="")
    version = models.PositiveIntegerField(default=1)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="promotion_plans_updated")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["promotion_type"]
        constraints = [
            models.CheckConstraint(condition=Q(duration_days__gt=0), name="promo_plan_duration_positive"),
            models.CheckConstraint(condition=Q(currency="NGN"), name="promo_plan_ngn_only"),
        ]

    def __str__(self):
        return self.name


class PromotionInventory(models.Model):
    """A row-level mutex and independently configurable capacity for one canonical target."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    promotion_type = models.CharField(max_length=20, choices=PromotionApplication.PromotionType.choices)
    target_key = models.CharField(max_length=320, unique=True)
    institution = models.ForeignKey("home.Institution", on_delete=models.PROTECT, null=True, blank=True, related_name="promotion_inventories")
    area_location = models.ForeignKey("home.Locations", on_delete=models.PROTECT, null=True, blank=True, related_name="area_promotion_inventories")
    state_country = models.CharField(max_length=100, blank=True, default="")
    state_name = models.CharField(max_length=100, blank=True, default="")
    target_label = models.CharField(max_length=300)
    capacity = models.PositiveIntegerField(default=20)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["promotion_type", "target_label", "target_key"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(promotion_type="INSTITUTION", institution__isnull=False, area_location__isnull=True, state_country="", state_name="")
                    | Q(promotion_type="AREA", institution__isnull=True, area_location__isnull=False, state_country="", state_name="")
                    | (Q(promotion_type="STATE", institution__isnull=True, area_location__isnull=True) & ~Q(state_country="") & ~Q(state_name=""))
                    | Q(promotion_type="HOMEPAGE", institution__isnull=True, area_location__isnull=True, state_country="", state_name="")
                ),
                name="promo_inventory_target_matches_type",
            ),
        ]
        indexes = [models.Index(fields=["promotion_type", "target_key"], name="promo_inventory_type_key_idx")]

    def __str__(self):
        return f"{self.get_promotion_type_display()} · {self.target_label}"


class PromotionCampaign(models.Model):
    class Status(models.TextChoices):
        PENDING_PAYMENT = "PENDING_PAYMENT", "Awaiting payment"
        PAYMENT_REVIEW = "PAYMENT_REVIEW", "Payment review required"
        QUEUED = "QUEUED", "Paid — queued"
        SCHEDULED = "SCHEDULED", "Scheduled"
        ACTIVE = "ACTIVE", "Active"
        PAUSED = "PAUSED", "Paused"
        SUSPENDED = "SUSPENDED", "Suspended pending review"
        EXPIRED = "EXPIRED", "Expired"
        REVOKED = "REVOKED", "Revoked"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.OneToOneField(PromotionApplication, on_delete=models.PROTECT, related_name="campaign")
    approved_revision = models.ForeignKey(PromotionApplicationRevision, on_delete=models.PROTECT, related_name="promotion_campaigns")
    artisan = models.ForeignKey("home.ArtisanProfile", on_delete=models.PROTECT, related_name="promotion_campaigns")
    inventory = models.ForeignKey(PromotionInventory, on_delete=models.PROTECT, related_name="campaigns")
    plan = models.ForeignKey(PromotionPlan, on_delete=models.SET_NULL, null=True, blank=True, related_name="campaigns")
    promotion_type = models.CharField(max_length=20, choices=PromotionApplication.PromotionType.choices)
    target_snapshot = models.JSONField(default=dict)
    description_snapshot = models.TextField(blank=True, default="")
    services_snapshot = models.JSONField(default=list)
    profile_snapshot = models.JSONField(default=dict)
    plan_name_snapshot = models.CharField(max_length=120)
    price_kobo = models.PositiveBigIntegerField()
    currency = models.CharField(max_length=3, default="NGN")
    duration_days = models.PositiveSmallIntegerField()
    terms_snapshot = models.TextField()
    refund_policy_snapshot = models.CharField(max_length=20, choices=PromotionPlan.RefundPolicy.choices)
    accepted_terms_version = models.PositiveIntegerField()
    terms_accepted_at = models.DateTimeField()
    terms_accepted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="promotion_terms_accepted")
    status = models.CharField(max_length=25, choices=Status.choices, default=Status.PENDING_PAYMENT, db_index=True)
    verified_payment = models.OneToOneField("PromotionPayment", on_delete=models.PROTECT, null=True, blank=True, related_name="verified_campaign")
    payment_verified_at = models.DateTimeField(null=True, blank=True)
    queue_reentry_at = models.DateTimeField(null=True, blank=True)
    scheduled_start_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    paused_at = models.DateTimeField(null=True, blank=True)
    remaining_duration_seconds = models.PositiveIntegerField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    ended_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="promotion_campaigns_ended")
    last_action_reason = models.TextField(blank=True, default="")
    # Legacy counters remain for compatibility; deduplicated event aggregates are stored separately.
    impressions = models.PositiveBigIntegerField(default=0)
    profile_clicks = models.PositiveBigIntegerField(default=0)
    performance_impressions = models.PositiveBigIntegerField(default=0)
    performance_profile_visits = models.PositiveBigIntegerField(default=0)
    enquiries = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "id"]
        constraints = [
            models.CheckConstraint(condition=Q(price_kobo__gt=0), name="promo_campaign_price_positive"),
            models.CheckConstraint(condition=Q(duration_days__gt=0), name="promo_campaign_duration_positive"),
            models.CheckConstraint(condition=Q(currency="NGN"), name="promo_campaign_ngn_only"),
        ]
        indexes = [
            models.Index(fields=["inventory", "status", "expires_at"], name="pr_campaign_inv_status_idx"),
            models.Index(fields=["artisan", "-created_at"], name="pr_campaign_artisan_idx"),
            models.Index(fields=["status", "scheduled_start_at"], name="pr_campaign_status_start_idx"),
        ]

    @property
    def is_publicly_active(self):
        from django.utils import timezone
        now = timezone.now()
        return self.status == self.Status.ACTIVE and self.started_at is not None and self.started_at <= now and self.expires_at is not None and self.expires_at > now

    def __str__(self):
        return f"{self.plan_name_snapshot} · {self.target_snapshot.get('label', self.inventory.target_label)}"


class ArtisanMarketPick(models.Model):
    """Editorial homepage selection, deliberately independent from paid campaigns."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    artisan = models.OneToOneField(
        "home.ArtisanProfile",
        on_delete=models.PROTECT,
        related_name="market_pick",
    )
    position = models.PositiveSmallIntegerField(default=1)
    selection_note = models.CharField(max_length=500, blank=True, default="")
    is_active = models.BooleanField(default=True, db_index=True)
    selected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="artisan_market_picks_selected",
    )
    selected_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["position", "created_at", "id"]
        indexes = [models.Index(fields=["is_active", "position"], name="promo_market_pick_order_idx")]
        constraints = [
            models.CheckConstraint(condition=Q(position__gt=0), name="promo_market_pick_position_positive"),
        ]

    def __str__(self):
        return f"Artisan Market Pick · {self.artisan_id}"


class PromotionCampaignPerformanceDaily(models.Model):
    """Privacy-minimized daily aggregates retained after visitor dedupe keys are purged."""

    id = models.BigAutoField(primary_key=True)
    campaign = models.ForeignKey(
        "PromotionCampaign",
        on_delete=models.CASCADE,
        related_name="performance_daily",
    )
    event_day = models.DateField()
    impressions = models.PositiveBigIntegerField(default=0)
    profile_visits = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-event_day", "campaign_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["campaign", "event_day"],
                name="promo_perf_campaign_day_uniq",
            ),
        ]
        indexes = [models.Index(fields=["event_day"], name="promo_perf_daily_day_idx")]


class PromotionCampaignPerformanceEvent(models.Model):
    """One first-party, deduplicated exposure/visit per browser session per day."""

    class EventType(models.TextChoices):
        IMPRESSION = "IMPRESSION", "Impression"
        PROFILE_VISIT = "PROFILE_VISIT", "Profile visit"

    id = models.BigAutoField(primary_key=True)
    campaign = models.ForeignKey(
        "PromotionCampaign",
        on_delete=models.CASCADE,
        related_name="performance_events",
    )
    event_type = models.CharField(max_length=20, choices=EventType.choices)
    visitor_digest = models.CharField(max_length=64)
    event_day = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["campaign", "event_type", "visitor_digest", "event_day"],
                name="promo_perf_daily_visitor_event_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["event_day", "event_type"], name="promo_perf_event_day_idx"),
            models.Index(fields=["campaign", "event_day"], name="promo_perf_campaign_day_idx"),
        ]


class PromotionPayment(models.Model):
    class Status(models.TextChoices):
        INITIALIZATION_PENDING = "INITIALIZATION_PENDING", "Preparing payment"
        INITIALIZED = "INITIALIZED", "Payment ready"
        VERIFYING = "VERIFYING", "Verifying payment"
        VERIFIED = "VERIFIED", "Verified"
        REVIEW_REQUIRED = "REVIEW_REQUIRED", "Review required"
        FAILED = "FAILED", "Failed"
        EXPIRED = "EXPIRED", "Expired"
        REFUND_PENDING = "REFUND_PENDING", "Refund pending"
        PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED", "Partially refunded"
        REFUNDED = "REFUNDED", "Refunded"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    campaign = models.ForeignKey(PromotionCampaign, on_delete=models.PROTECT, related_name="payments")
    reference = models.CharField(max_length=150, unique=True)
    amount_kobo = models.PositiveBigIntegerField()
    currency = models.CharField(max_length=3, default="NGN")
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.INITIALIZATION_PENDING, db_index=True)
    authorization_url = models.URLField(max_length=1000, blank=True, default="")
    provider_transaction_id = models.CharField(max_length=100, blank=True, null=True)
    provider_reference = models.CharField(max_length=150, blank=True, default="")
    provider_status = models.CharField(max_length=60, blank=True, default="")
    provider_payload = models.JSONField(default=dict, blank=True)
    verification_payload = models.JSONField(default=dict, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    failure_reason = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            models.CheckConstraint(condition=Q(amount_kobo__gt=0), name="promo_payment_amount_positive"),
            models.CheckConstraint(condition=Q(currency="NGN"), name="promo_payment_ngn_only"),
        ]
        indexes = [
            models.Index(fields=["campaign", "status"], name="pr_payment_campaign_status_idx"),
            models.Index(fields=["status", "created_at"], name="pr_payment_status_created_idx"),
            models.Index(fields=["provider_transaction_id"], name="promo_payment_provider_tx_idx"),
        ]

    def __str__(self):
        return self.reference


class PromotionCampaignAction(models.Model):
    class ActionType(models.TextChoices):
        PAYMENT_VERIFIED = "PAYMENT_VERIFIED", "Payment verified"
        SCHEDULED = "SCHEDULED", "Scheduled"
        STARTED = "STARTED", "Started"
        PAUSED = "PAUSED", "Paused for review"
        SUSPENDED = "SUSPENDED", "Suspended"
        RESTORED = "RESTORED", "Restored"
        REVOKED = "REVOKED", "Revoked"
        EXPIRED = "EXPIRED", "Expired"
        PAYMENT_REVIEW = "PAYMENT_REVIEW", "Payment review"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    campaign = models.ForeignKey(PromotionCampaign, on_delete=models.PROTECT, related_name="actions")
    action = models.CharField(max_length=30, choices=ActionType.choices)
    status_before = models.CharField(max_length=25, blank=True, default="")
    status_after = models.CharField(max_length=25, blank=True, default="")
    reason = models.TextField(blank=True, default="")
    evidence_summary = models.TextField(blank=True, default="")
    support_case_id = models.PositiveBigIntegerField(null=True, blank=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="promotion_campaign_actions")
    actor_name_snapshot = models.CharField(max_length=160, blank=True, default="")
    refund_action_id = models.UUIDField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["campaign", "-created_at"], name="pr_campaign_action_created_idx")]

    def __str__(self):
        return f"{self.get_action_display()} · {self.campaign_id}"

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValueError("Promotion campaign audit actions are immutable.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Promotion campaign audit actions cannot be deleted.")

# Stable module-level references for deterministic OpenAPI enum names.
PROMOTION_APPLICATION_STATUS_CHOICES = PromotionApplication.Status.choices
PROMOTION_CAMPAIGN_STATUS_CHOICES = PromotionCampaign.Status.choices
