"""Manager console domain models.

These models exist only for things the marketplace did not already have:

* role/permission assignment for staff (previously ``is_superuser`` was the only gate),
* step-up grants and manager login challenges,
* versioned platform configuration (business rules were hard-coded in ``core.policies``),
* an immutable analytics event store (``core.OutboxEvent`` is a dispatch queue that
  hard-fails on unknown event types, so it cannot carry analytics history),
* precomputed metric buckets and point-in-time snapshots,
* a durable background job registry, campaigns, saved reports and export artifacts.

Everything that already existed (bookings, payments, escrow, support cases, risk
profiles, promotion campaigns, audit log) is read, never duplicated.
"""
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class ManagerRole(models.Model):
    """A named bundle of permission codes. System roles cannot be deleted."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    slug = models.SlugField(max_length=60, unique=True)
    name = models.CharField(max_length=80)
    description = models.CharField(max_length=240, blank=True, default="")
    is_system = models.BooleanField(default=False)
    permissions = models.JSONField(default=list, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="manager_roles_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class ManagerProfile(models.Model):
    """Staff identity, role assignment and second-factor state."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="manager_profile",
    )
    role = models.ForeignKey(
        ManagerRole,
        on_delete=models.PROTECT,
        related_name="members",
    )
    job_title = models.CharField(max_length=120, blank=True, default="")
    department = models.CharField(max_length=120, blank=True, default="")
    is_suspended = models.BooleanField(default=False)
    suspension_reason = models.CharField(max_length=240, blank=True, default="")
    # TOTP secret is stored encrypted with the platform KYC Fernet key.
    totp_secret_encrypted = models.TextField(blank=True, default="")
    totp_confirmed_at = models.DateTimeField(null=True, blank=True)
    totp_failure_count = models.PositiveSmallIntegerField(default=0)
    totp_last_used_step = models.BigIntegerField(default=0)
    email_otp_enabled = models.BooleanField(default=True)
    last_authenticated_at = models.DateTimeField(null=True, blank=True)
    must_change_password = models.BooleanField(default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="manager_profiles_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["role", "is_suspended"], name="mgr_profile_role_idx")]

    def __str__(self):
        return f"{self.user_id} · {self.role.slug}"

    @property
    def totp_enrolled(self) -> bool:
        return bool(self.totp_confirmed_at and self.totp_secret_encrypted)


class ManagerLoginThrottle(models.Model):
    """Per-account and per-address login counters, kept in the database so a
    cache flush cannot reset an attacker's allowance."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    scope = models.CharField(max_length=20, choices=[("ACCOUNT", "Account"), ("IP", "IP")])
    key_hash = models.CharField(max_length=64, db_index=True)
    failures = models.PositiveIntegerField(default=0)
    first_failure_at = models.DateTimeField(default=timezone.now)
    locked_until = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["scope", "key_hash"], name="mgr_throttle_scope_key"),
        ]


class ManagerSession(models.Model):
    """A live console session, so staff can inspect and revoke their devices."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="manager_sessions",
    )
    session_key_hash = models.CharField(max_length=64, unique=True)
    ip_hash = models.CharField(max_length=64, blank=True, default="")
    user_agent_hash = models.CharField(max_length=64, blank=True, default="")
    mfa_verified_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(default=timezone.now)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_reason = models.CharField(max_length=120, blank=True, default="")

    class Meta:
        ordering = ["-last_seen_at"]
        indexes = [models.Index(fields=["user", "revoked_at"], name="mgr_session_user_idx")]


class ManagerOTPChallenge(models.Model):
    """Single-use login / step-up code. Only a keyed digest is stored."""

    class Purpose(models.TextChoices):
        LOGIN = "LOGIN", "Manager sign-in"
        STEP_UP = "STEP_UP", "Step-up verification"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="manager_otp_challenges",
    )
    purpose = models.CharField(max_length=12, choices=Purpose.choices)
    code_hash = models.CharField(max_length=64)
    delivered_via = models.CharField(max_length=12, default="EMAIL")
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    consumed_at = models.DateTimeField(null=True, blank=True)
    invalidated_at = models.DateTimeField(null=True, blank=True)
    ip_hash = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "purpose", "expires_at"], name="mgr_otp_user_purpose_idx")]
        constraints = [
            models.CheckConstraint(condition=models.Q(attempts__lte=10), name="mgr_otp_attempts_lte_10"),
        ]


class StepUpGrant(models.Model):
    """Short-lived, single-use, operation-scoped verification for sensitive writes."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="manager_step_up_grants",
    )
    scope = models.CharField(max_length=60, db_index=True)
    token_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(db_index=True)
    consumed_at = models.DateTimeField(null=True, blank=True)
    ip_hash = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        ordering = ["-created_at"]


class PlatformConfigVersion(models.Model):
    """One immutable version of a platform business rule."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.CharField(max_length=80, db_index=True)
    version = models.PositiveIntegerField()
    value = models.JSONField()
    value_type = models.CharField(max_length=20, default="string")
    effective_from = models.DateTimeField(default=timezone.now, db_index=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=240, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="manager_config_versions",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    change_request = models.ForeignKey(
        "manager.ConfigChangeRequest",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="applied_versions",
    )

    class Meta:
        ordering = ["-effective_from", "-version"]
        constraints = [
            models.UniqueConstraint(fields=["key", "version"], name="mgr_config_key_version"),
        ]
        indexes = [
            models.Index(fields=["key", "effective_from"], name="mgr_config_key_effective_idx"),
        ]

    def __str__(self):
        return f"{self.key} v{self.version}"


class ConfigChangeRequest(models.Model):
    """Proposed configuration change; sensitive keys need a second approver."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "Awaiting approval"
        APPROVED = "APPROVED", "Approved"
        APPLIED = "APPLIED", "Applied"
        REJECTED = "REJECTED", "Rejected"
        CANCELLED = "CANCELLED", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.CharField(max_length=80, db_index=True)
    proposed_value = models.JSONField()
    current_value = models.JSONField(default=dict, blank=True)
    preview = models.JSONField(default=dict, blank=True)
    reason = models.CharField(max_length=500)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING, db_index=True)
    requires_approval = models.BooleanField(default=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="config_changes_requested",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="config_changes_approved",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    applied_version = models.PositiveIntegerField(null=True, blank=True)
    effective_from = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "key"], name="mgr_change_status_key_idx")]

    def __str__(self):
        return f"{self.key} · {self.status}"


class AnalyticsEvent(models.Model):
    """Append-only business event history used by every funnel and cohort view."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event_id = models.UUIDField(default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=80, db_index=True)
    entity_type = models.CharField(max_length=40, blank=True, default="")
    entity_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)
    actor_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    actor_role = models.CharField(max_length=20, blank=True, default="")
    correlation_id = models.CharField(max_length=64, blank=True, default="")
    schema_version = models.PositiveSmallIntegerField(default=1)
    properties = models.JSONField(default=dict, blank=True)
    source = models.CharField(max_length=40, default="backend")
    dedupe_key = models.CharField(max_length=64, unique=True)
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-occurred_at"]
        indexes = [
            models.Index(fields=["name", "occurred_at"], name="mgr_event_name_time_idx"),
            models.Index(fields=["entity_type", "entity_id"], name="mgr_event_entity_idx"),
        ]

    def __str__(self):
        return f"{self.name} · {self.entity_type}:{self.entity_id}"


class MetricAggregate(models.Model):
    """Incremental rollup so dashboards never re-aggregate raw tables."""

    id = models.BigAutoField(primary_key=True)
    metric_key = models.CharField(max_length=80, db_index=True)
    bucket_kind = models.CharField(max_length=10, default="DAY")
    bucket_start = models.DateTimeField(db_index=True)
    dimensions_hash = models.CharField(max_length=64, default="")
    dimensions = models.JSONField(default=dict, blank=True)
    value = models.DecimalField(max_digits=24, decimal_places=4, default=0)
    calc_version = models.CharField(max_length=20, default="v1")
    computed_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["metric_key", "bucket_kind", "bucket_start", "dimensions_hash"],
                name="mgr_metric_bucket_unique",
            ),
        ]
        indexes = [models.Index(fields=["metric_key", "bucket_start"], name="mgr_metric_key_bucket_idx")]


class OperationalSnapshot(models.Model):
    """Point-in-time operational gauge, captured on a configurable interval."""

    id = models.BigAutoField(primary_key=True)
    metric_key = models.CharField(max_length=80, db_index=True)
    value = models.DecimalField(max_digits=24, decimal_places=4, default=0)
    dimensions = models.JSONField(default=dict, blank=True)
    captured_for = models.DateTimeField(db_index=True)
    captured_at = models.DateTimeField(auto_now_add=True)
    interval_seconds = models.PositiveIntegerField(default=300)
    calc_version = models.CharField(max_length=20, default="v1")
    duration_ms = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-captured_for"]
        constraints = [
            models.UniqueConstraint(
                fields=["metric_key", "captured_for", "interval_seconds"],
                name="mgr_snapshot_metric_time_unique",
            ),
        ]
        indexes = [models.Index(fields=["metric_key", "captured_for"], name="mgr_snapshot_key_time_idx")]


class SnapshotIntervalSetting(models.Model):
    """Single row holding the operational snapshot cadence."""

    key = models.CharField(max_length=40, primary_key=True, default="operational")
    interval_seconds = models.PositiveIntegerField(default=300)
    enabled = models.BooleanField(default=True)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="snapshot_interval_changes",
    )
    changed_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(interval_seconds__gte=60) & models.Q(interval_seconds__lte=86400),
                name="mgr_snapshot_interval_range",
            ),
        ]


class BackgroundJob(models.Model):
    """Durable record for every queued unit of work the console can start."""

    class Kind(models.TextChoices):
        SNAPSHOT = "SNAPSHOT", "Snapshot capture"
        CAMPAIGN_DISPATCH = "CAMPAIGN_DISPATCH", "Campaign dispatch"
        EXPORT = "EXPORT", "Data export"
        REPORT = "REPORT", "Saved report run"
        RECONCILIATION = "RECONCILIATION", "Reconciliation"
        AGGREGATION = "AGGREGATION", "Aggregate rebuild"
        DATA_QUALITY = "DATA_QUALITY", "Data quality scan"

    class Status(models.TextChoices):
        QUEUED = "QUEUED", "Queued"
        RUNNING = "RUNNING", "Running"
        SUCCEEDED = "SUCCEEDED", "Succeeded"
        FAILED = "FAILED", "Failed"
        CANCELLED = "CANCELLED", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=24, choices=Kind.choices)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.QUEUED, db_index=True)
    queue = models.CharField(max_length=30, default="default")
    dedupe_key = models.CharField(max_length=80, blank=True, default="", db_index=True)
    payload = models.JSONField(default=dict, blank=True)
    progress = models.PositiveIntegerField(default=0)
    total = models.PositiveIntegerField(default=0)
    result = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True, default="")
    attempts = models.PositiveSmallIntegerField(default=0)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="manager_jobs",
    )
    correlation_id = models.CharField(max_length=64, blank=True, default="")
    scheduled_for = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "created_at"], name="mgr_job_status_created_idx"),
            models.Index(fields=["kind", "status"], name="mgr_job_kind_status_idx"),
        ]

    def __str__(self):
        return f"{self.kind} · {self.status}"


class Campaign(models.Model):
    """No-code message campaign. Creation and dispatch are separate operations."""

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        PENDING_APPROVAL = "PENDING_APPROVAL", "Awaiting approval"
        SCHEDULED = "SCHEDULED", "Scheduled"
        DISPATCHING = "DISPATCHING", "Dispatching"
        SENT = "SENT", "Sent"
        CANCELLED = "CANCELLED", "Cancelled"
        FAILED = "FAILED", "Failed"

    class Channel(models.TextChoices):
        IN_APP = "IN_APP", "In-app notification"
        EMAIL = "EMAIL", "Email"
        PUSH = "PUSH", "Push notification"
        SMS = "SMS", "SMS"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=140)
    subject = models.CharField(max_length=160, blank=True, default="")
    body = models.TextField()
    cta_text = models.CharField(max_length=60, blank=True, default="")
    cta_url = models.CharField(max_length=300, blank=True, default="")
    image_url = models.CharField(max_length=300, blank=True, default="")
    channels = models.JSONField(default=list, blank=True)
    audience_spec = models.JSONField(default=dict, blank=True)
    audience_count = models.PositiveIntegerField(default=0)
    suppressed_count = models.PositiveIntegerField(default=0)
    invalid_contact_count = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT, db_index=True)
    requires_approval = models.BooleanField(default=True)
    notes = models.CharField(max_length=500, blank=True, default="")
    scheduled_for = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    attribution_window_days = models.PositiveSmallIntegerField(default=7)
    # What the last dispatch actually did, so "sent" is never mistaken for
    # "delivered to everyone": a campaign can finish with every recipient
    # suppressed by the frequency cap.
    dispatch_summary = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="campaigns_created",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="campaigns_approved",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "scheduled_for"], name="mgr_campaign_status_sched_idx")]

    def __str__(self):
        return self.title


class CampaignDelivery(models.Model):
    class Status(models.TextChoices):
        QUEUED = "QUEUED", "Queued"
        SENT = "SENT", "Sent"
        DELIVERED = "DELIVERED", "Delivered"
        FAILED = "FAILED", "Failed"
        SKIPPED = "SKIPPED", "Skipped (consent or suppression)"

    id = models.BigAutoField(primary_key=True)
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="deliveries")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="campaign_deliveries",
    )
    channel = models.CharField(max_length=10)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.QUEUED, db_index=True)
    error = models.CharField(max_length=240, blank=True, default="")
    sent_at = models.DateTimeField(null=True, blank=True)
    opened_at = models.DateTimeField(null=True, blank=True)
    clicked_at = models.DateTimeField(null=True, blank=True)
    booking_id = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["campaign", "user", "channel"], name="mgr_delivery_unique"),
        ]
        indexes = [
            models.Index(fields=["campaign", "status"], name="mgr_delivery_camp_status_idx"),
        ]


class SavedReport(models.Model):
    """A validated Data Explorer definition - never free-form SQL."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=120)
    description = models.CharField(max_length=300, blank=True, default="")
    spec = models.JSONField(default=dict, blank=True)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="saved_reports",
    )
    is_shared = models.BooleanField(default=False)
    schedule_cron = models.CharField(max_length=80, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["owner", "name"], name="mgr_report_owner_name")]


class ExportArtifact(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.OneToOneField(BackgroundJob, on_delete=models.CASCADE, related_name="artifact")
    filename = models.CharField(max_length=160)
    content_type = models.CharField(max_length=80, default="text/csv")
    row_count = models.PositiveIntegerField(default=0)
    byte_size = models.PositiveIntegerField(default=0)
    storage_key = models.CharField(max_length=300)
    checksum = models.CharField(max_length=64, blank=True, default="")
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="export_artifacts",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.filename


class RetentionPolicy(models.Model):
    """Configurable retention window per data class, enforced by the purge task."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    data_class = models.CharField(max_length=40, unique=True)
    days = models.PositiveIntegerField(default=365)
    enabled = models.BooleanField(default=True)
    notes = models.CharField(max_length=240, blank=True, default="")
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="retention_policies_updated",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["data_class"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(days__gte=1) & models.Q(days__lte=3650),
                name="mgr_retention_days_range",
            ),
        ]


class DataSubjectRequest(models.Model):
    """Staff workflow for access / correction / deletion requests."""

    class Type(models.TextChoices):
        ACCESS = "ACCESS", "Access"
        CORRECTION = "CORRECTION", "Correction"
        DELETION = "DELETION", "Deletion"
        RESTRICTION = "RESTRICTION", "Restriction"
        OBJECTION = "OBJECTION", "Objection"
        CONSENT_WITHDRAWAL = "CONSENT_WITHDRAWAL", "Consent withdrawal"

    class Status(models.TextChoices):
        RECEIVED = "RECEIVED", "Received"
        VERIFYING = "VERIFYING", "Verifying identity"
        IN_PROGRESS = "IN_PROGRESS", "In progress"
        COMPLETED = "COMPLETED", "Completed"
        REJECTED = "REJECTED", "Rejected (retention obligation)"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference = models.CharField(max_length=24, unique=True)
    subject_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="data_subject_requests",
    )
    request_type = models.CharField(max_length=20, choices=Type.choices)
    status = models.CharField(max_length=14, choices=Status.choices, default=Status.RECEIVED, db_index=True)
    details = models.TextField(blank=True, default="")
    outcome_notes = models.TextField(blank=True, default="")
    handled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="data_subject_requests_handled",
    )
    identity_verified_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]


class RiskReview(models.Model):
    """Human review record for an automated risk signal (never auto-suspends)."""

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        INVESTIGATING = "INVESTIGATING", "Investigating"
        CLEARED = "CLEARED", "Cleared"
        ESCALATED = "ESCALATED", "Escalated"
        ACTIONED = "ACTIONED", "Action taken"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subject_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="risk_reviews",
    )
    signal = models.CharField(max_length=60)
    severity = models.CharField(max_length=10, default="MEDIUM")
    status = models.CharField(max_length=14, choices=Status.choices, default=Status.OPEN, db_index=True)
    summary = models.CharField(max_length=300, blank=True, default="")
    evidence = models.JSONField(default=dict, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="risk_reviews_reviewed",
    )
    decision_notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "severity"], name="mgr_risk_status_sev_idx")]


class ContactPreference(models.Model):
    """Per-user, per-channel marketing consent.

    Nothing in the marketplace recorded marketing consent before the console
    existed, so campaigns need one authoritative place to check it. A row with
    ``opted_out=True`` is a hard block for every campaign on that channel.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="contact_preferences",
    )
    channel = models.CharField(max_length=10, choices=[("IN_APP", "In-app"), ("EMAIL", "Email"),
                                                       ("PUSH", "Push"), ("SMS", "SMS")])
    opted_out = models.BooleanField(default=False)
    reason = models.CharField(max_length=120, blank=True, default="")
    source = models.CharField(max_length=40, default="MANAGER")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "channel"], name="mgr_contact_pref_unique")]
        indexes = [models.Index(fields=["channel", "opted_out"], name="mgr_contact_pref_idx")]


class CampaignAudienceSnapshot(models.Model):
    """Who a campaign was built for, and who was excluded - kept for audit."""

    id = models.BigAutoField(primary_key=True)
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="audience_snapshots")
    estimated = models.PositiveIntegerField(default=0)
    eligible = models.PositiveIntegerField(default=0)
    opted_out = models.PositiveIntegerField(default=0)
    invalid_contact = models.PositiveIntegerField(default=0)
    recently_contacted = models.PositiveIntegerField(default=0)
    spec = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
