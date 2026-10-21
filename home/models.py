import uuid

from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from .security_questions import SECURITY_QUESTION_OPTIONS

from .profile_links import (
    SOCIAL_MEDIA_PLATFORM_CHOICES,
    validate_external_profile_url,
    validate_social_media_url,
)

class CustomUserManager(BaseUserManager):
    def create_user(self, email, first_name, last_name, password=None, **kwargs):
        if not email:
            raise ValueError("Email is Required")
        email = self.normalize_email(email)
        user = self.model(email=email, first_name=first_name, last_name=last_name, **kwargs)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, first_name, last_name, password=None, **kwargs):
        user = self.create_user(email=email, first_name=first_name, last_name=last_name, password=password, **kwargs)
        user.is_staff = True
        user.is_superuser = True
        user.is_active = True
        user.save(using=self._db)
        return user

class CustomUser(AbstractBaseUser, PermissionsMixin):
    email = models.EmailField(unique=True)
    first_name = models.CharField(max_length=40)
    last_name = models.CharField(max_length=40)
    phone_number = models.CharField(max_length=20, null=True, blank=True, unique=True)
    is_active = models.BooleanField(default=False)
    is_staff = models.BooleanField(default=False)
    password_changed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    token_version = models.PositiveIntegerField(default=1)

    SECURITY_STATUS = [
        ("NORMAL", "Normal"),
        ("REVIEW", "Review"),
        ("LOCKED", "Locked"),
    ]

    security_status = models.CharField(max_length=10, choices=SECURITY_STATUS, default="NORMAL")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    objects = CustomUserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["first_name", "last_name"]

    def save(self, *args, **kwargs):
        """Timestamp every persisted password change, including Django admin changes."""
        update_fields = kwargs.get("update_fields")
        password_is_being_saved = update_fields is None or "password" in update_fields
        if not self.pk and self.password and self.password_changed_at is None:
            self.password_changed_at = timezone.now()
        elif self.pk and password_is_being_saved:
            previous_password = type(self).objects.filter(pk=self.pk).values_list("password", flat=True).first()
            if previous_password is not None and previous_password != self.password:
                self.password_changed_at = timezone.now()
                if update_fields is not None:
                    kwargs["update_fields"] = set(update_fields) | {"password_changed_at"}
        return super().save(*args, **kwargs)

    def get_full_name(self):
        """Display name used by serializers, payment providers and notifications."""
        return f"{self.first_name} {self.last_name}".strip()

    def get_short_name(self):
        return self.first_name

    def __str__(self):
        return self.email

class StaffSecurityProfile(models.Model):
    """Three registered prompts with one-way password hashes for their answers."""

    user = models.OneToOneField(
        CustomUser,
        on_delete=models.CASCADE,
        related_name="staff_security_questions",
    )
    question_1 = models.CharField(max_length=40, choices=SECURITY_QUESTION_OPTIONS)
    answer_hash_1 = models.CharField(max_length=256)
    question_2 = models.CharField(max_length=40, choices=SECURITY_QUESTION_OPTIONS)
    answer_hash_2 = models.CharField(max_length=256)
    question_3 = models.CharField(max_length=40, choices=SECURITY_QUESTION_OPTIONS)
    answer_hash_3 = models.CharField(max_length=256)
    registered_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    ~Q(question_1=F("question_2"))
                    & ~Q(question_1=F("question_3"))
                    & ~Q(question_2=F("question_3"))
                ),
                name="staff_security_distinct_prompts",
            ),
        ]

    def __str__(self):
        return f"Staff security questions for user {self.user_id}"


class StaffSecurityChallenge(models.Model):
    class Purpose(models.TextChoices):
        PASSWORD_RESET = "PASSWORD_RESET", "Staff password reset"
        SECURITY_SETUP = "SECURITY_SETUP", "Initial security-question setup"
        LOGIN_MFA = "LOGIN_MFA", "Staff sign-in MFA"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name="staff_security_challenges",
    )
    initiated_by = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="initiated_staff_security_challenges",
    )
    purpose = models.CharField(max_length=24, choices=Purpose.choices)
    otp_hash = models.CharField(max_length=256)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    expires_at = models.DateTimeField(db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    consumed_at = models.DateTimeField(null=True, blank=True)
    invalidated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["user", "purpose", "expires_at"], name="staff_challenge_user_exp_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(attempts__lte=10), name="staff_challenge_attempts_lte_10"),
        ]

    def __str__(self):
        return f"Staff security challenge {self.pk} ({self.purpose})"


class StaffPasswordResetToken(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name="staff_password_reset_tokens",
    )
    challenge = models.OneToOneField(
        StaffSecurityChallenge,
        on_delete=models.CASCADE,
        related_name="password_reset_token",
    )
    token_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(db_index=True)
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["user", "expires_at"], name="staff_reset_user_exp_idx"),
        ]

    def __str__(self):
        return f"Staff password reset token {self.pk}"


class ArtisanProfile(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(CustomUser, on_delete=models.CASCADE, related_name="artisan_profile")
    business_name = models.CharField(max_length=150, blank=True)
    bio = models.TextField(blank=True)
    years_experience = models.PositiveIntegerField(default=0)
    country = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=100, blank=True)
    area = models.CharField(max_length=150, blank=True)
    nearest_institution = models.ForeignKey(
        'home.Institution', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='artisan_profiles',
    )
    profile_photo = models.ImageField(upload_to="artisan_profiles/%Y/%m/", null=True, blank=True)
    social_media_1_platform = models.CharField(
        max_length=20, choices=SOCIAL_MEDIA_PLATFORM_CHOICES, blank=True, default=""
    )
    social_media_1_url = models.URLField(
        max_length=2048, blank=True, default="", validators=[validate_external_profile_url]
    )
    social_media_2_platform = models.CharField(
        max_length=20, choices=SOCIAL_MEDIA_PLATFORM_CHOICES, blank=True, default=""
    )
    social_media_2_url = models.URLField(
        max_length=2048, blank=True, default="", validators=[validate_external_profile_url]
    )
    portfolio_url = models.URLField(
        max_length=2048, blank=True, default="", validators=[validate_external_profile_url]
    )
    average_rating = models.DecimalField(max_digits=3, decimal_places=2, default=0.00)
    completion_rate = models.FloatField(default=0.0)
    cancellation_rate = models.FloatField(default=0.0)

    PROFILE_STATUS = [
        ("DRAFT", "Draft"),
        ("ACTIVE", "Active"),
        ("INACTIVE", "Inactive"),
        ("SUSPENDED", "Suspended"),
    ]

    profile_status = models.CharField(max_length=20, choices=PROFILE_STATUS, default="DRAFT")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        super().clean()
        errors = {}
        for index in (1, 2):
            platform_field = f"social_media_{index}_platform"
            url_field = f"social_media_{index}_url"
            platform = getattr(self, platform_field, "")
            url = getattr(self, url_field, "")
            if bool(platform) != bool(url):
                errors[url_field] = "Choose both a platform and its profile URL, or leave both blank."
            elif url:
                try:
                    validate_social_media_url(url, platform)
                except ValidationError as exc:
                    errors[url_field] = exc.messages
        if errors:
            raise ValidationError(errors)

    class Meta:
        indexes = [
            models.Index(fields=["profile_status", "-average_rating"], name="artisan_status_rating_idx"),
        ]

    def __str__(self):
        return f"{self.business_name or self.user.email} - {self.profile_status}"

class Locations(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    country = models.CharField(max_length=100, db_index=True)
    state = models.CharField(max_length=100, db_index=True)
    area = models.CharField(max_length=150, db_index=True)

    def __str__(self):
        return f"{self.area}, {self.state}, {self.country}"


class Institution(models.Model):
    """Admin-maintained institution catalogue linked to an existing marketplace location."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=180)
    campus = models.CharField(max_length=180, blank=True, default="")
    abbreviation = models.CharField(max_length=40, blank=True, default="")
    abbreviations = models.JSONField(default=list, blank=True)
    aliases = models.JSONField(default=list, blank=True)
    source_id = models.CharField(max_length=100, null=True, blank=True, unique=True)
    source_url = models.URLField(blank=True, default="")

    location = models.ForeignKey(
        Locations,
        on_delete=models.PROTECT,
        related_name="institutions",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "campus", "location__country", "location__state", "location__area"]
        constraints = [
            models.UniqueConstraint(
                fields=["name", "campus", "location"],
                name="unique_institution_campus_location",
            ),
        ]
        indexes = [
            models.Index(fields=["is_active", "name"], name="institution_active_name_idx"),
            models.Index(fields=["location", "is_active"], name="institution_loc_active_idx"),
        ]

    def __str__(self):
        label = f"{self.name} — {self.campus}" if self.campus else self.name
        return f"{label} ({self.location})"


class ServiceCategory(models.Model):
    name = models.CharField(primary_key=True, max_length=100, unique=True)

    def __str__(self):
        return self.name

class Service(models.Model):
    category = models.ForeignKey(ServiceCategory, on_delete=models.CASCADE, related_name="services")
    name = models.CharField(max_length=150, unique=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["is_active", "category", "name"],
                name="service_active_cat_name_idx",
            ),
        ]

    def __str__(self):
        return f"{self.category.name} - {self.name}"

class ArtisanService(models.Model):
    artisan = models.ForeignKey(ArtisanProfile, on_delete=models.CASCADE, related_name="artisan_services")
    service = models.ForeignKey(Service, on_delete=models.CASCADE, related_name="artisan_services_category")
    location = models.ForeignKey(Locations, on_delete=models.CASCADE, related_name="artisan_service_location", null=True, blank=True)
    starting_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["service", "location", "artisan"],
                name="artisan_service_match_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["artisan", "service"],
                condition=models.Q(location__isnull=True),
                name="unique_artisan_general_service",
            ),
            models.UniqueConstraint(
                fields=["artisan", "service", "location"],
                condition=models.Q(location__isnull=False),
                name="unique_artisan_service_location",
            ),
        ]

    def __str__(self):
        return f"{self.artisan} - {self.service.name} - {self.location}"

class CustomerProfile(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(CustomUser, on_delete=models.CASCADE, related_name="customer_profile")
    profile_photo = models.ImageField(upload_to="customer_profiles/%Y/%m/", null=True, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    country = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=100, blank=True)
    area = models.CharField(max_length=150, blank=True)

    nearest_institution = models.ForeignKey(
        'home.Institution', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='customer_profiles',
    )

    GENDER_CHOICE = [
        ("M", "Male"),
        ("F", "Female"),
        ("N", "Prefer not to say"),
    ]

    gender = models.CharField(max_length=1, choices=GENDER_CHOICE, default="N")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.first_name} Customer Profile"


class ManagerSnapshotSchedule(models.Model):
    """Database-backed schedule and operational state for manager analytics snapshots."""

    class Frequency(models.TextChoices):
        HOURLY = "HOURLY", "Hourly"
        DAILY = "DAILY", "Daily"
        EVERY_THREE_DAYS = "EVERY_THREE_DAYS", "Every three days"
        WEEKLY = "WEEKLY", "Weekly"
        MONTHLY = "MONTHLY", "Monthly"

    key = models.CharField(max_length=40, primary_key=True, default="manager-overview", editable=False)
    frequency = models.CharField(max_length=20, choices=Frequency.choices, default=Frequency.DAILY)
    timezone_name = models.CharField(max_length=64, default="UTC")
    next_run_at = models.DateTimeField()
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_error_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")
    last_period_key = models.CharField(max_length=180, blank=True, default="")
    monthly_anchor_day = models.PositiveSmallIntegerField(default=1)
    schedule_version = models.PositiveIntegerField(default=1)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="manager_snapshot_schedules_updated",
    )
    updated_by_name_snapshot = models.CharField(max_length=160, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(monthly_anchor_day__gte=1, monthly_anchor_day__lte=31),
                name="manager_snapshot_month_anchor_valid",
            ),
        ]


class ManagerAnalyticsSnapshot(models.Model):
    """One durable result per intended schedule period, including failed attempts."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        RUNNING = "RUNNING", "Running"
        SUCCEEDED = "SUCCEEDED", "Succeeded"
        FAILED = "FAILED", "Failed"
        CANCELLED = "CANCELLED", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    schedule = models.ForeignKey(
        ManagerSnapshotSchedule,
        on_delete=models.PROTECT,
        related_name="runs",
    )
    period_key = models.CharField(max_length=180, unique=True)
    schedule_version = models.PositiveIntegerField()
    frequency = models.CharField(max_length=20, choices=ManagerSnapshotSchedule.Frequency.choices)
    timezone_name = models.CharField(max_length=64)
    scheduled_for = models.DateTimeField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING, db_index=True)
    payload = models.JSONField(default=dict, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    last_dispatched_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-scheduled_for", "-created_at"]
        indexes = [models.Index(fields=["status", "last_dispatched_at"], name="manager_snap_run_queue_idx")]

    def __str__(self):
        return f"{self.frequency} snapshot · {self.period_key} · {self.status}"
