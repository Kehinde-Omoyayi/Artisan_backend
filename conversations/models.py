import os
import uuid
from pathlib import Path

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone
from .private_media import conversation_storage


def conversation_attachment_upload_to(instance, filename):
    ext = Path(os.path.basename(filename)).suffix.lower()
    prefix = (
        "conversation_recordings"
        if instance.content_type.lower().startswith(("audio/", "video/"))
        else "conversation_attachments"
    )
    return f"{prefix}/{instance.message.conversation_id}/{uuid.uuid4().hex}{ext}"


class Conversation(models.Model):
    class ConversationType(models.TextChoices):
        SERVICE = "SERVICE", "Service"
        DIRECT = "DIRECT", "Direct"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation_type = models.CharField(
        max_length=16,
        choices=ConversationType.choices,
        default=ConversationType.SERVICE,
        db_index=True,
    )
    direct_key = models.CharField(
        max_length=80, unique=True, null=True, blank=True, editable=False
    )
    # Legacy marketplace roles remain for service conversations. Generic membership is
    # represented by participants so any authenticated user pair can have a direct chat.
    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="marketplace_conversations",
        null=True,
        blank=True,
    )
    artisan = models.ForeignKey(
        "home.ArtisanProfile",
        on_delete=models.PROTECT,
        related_name="marketplace_conversations",
        null=True,
        blank=True,
    )
    participants = models.ManyToManyField(
        settings.AUTH_USER_MODEL, related_name="conversations", blank=True
    )
    service_request = models.OneToOneField(
        "services.ServiceRequest",
        on_delete=models.PROTECT,
        related_name="conversation",
        null=True,
        blank=True,
    )
    draft = models.JSONField(default=dict, blank=True)
    # Persisted "Description Done" state: the assigned artisan has explicitly
    # confirmed the clarified scope. description_confirmed_hash is a digest of the
    # request description (+ additional details) at confirmation time; a later
    # material change invalidates the confirmation for offer purposes.
    description_confirmed_at = models.DateTimeField(null=True, blank=True)
    description_confirmed_hash = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["customer", "artisan"],
                condition=Q(service_request__isnull=True, conversation_type="SERVICE"),
                name="conv_one_draft_per_pair",
            ),
            models.CheckConstraint(
                condition=~Q(conversation_type="SERVICE")
                | Q(service_request__isnull=False, customer__isnull=False),
                name="service_room_has_request_owner",
            ),
        ]
        indexes = [
            models.Index(
                fields=["customer", "-updated_at"], name="conv_customer_upd_idx"
            ),
            models.Index(
                fields=["artisan", "-updated_at"], name="conv_artisan_upd_idx"
            ),
            models.Index(
                fields=["conversation_type", "-updated_at"],
                name="conv_type_updated_idx",
            ),
        ]
        ordering = ["-updated_at"]

    @property
    def request_submitted(self):
        return bool(
            self.service_request_id
            and self.service_request.status not in ("DRAFT", "CLOSED")
        )

    def save(self, *args, **kwargs):
        from django.db import transaction
        from services.models import ServiceRequest

        with transaction.atomic():
            if self.conversation_type == "SERVICE" and not self.service_request_id:
                if not self.customer_id:
                    raise ValueError(
                        "A service room needs its legitimate request owner."
                    )
                self.service_request = ServiceRequest.objects.create(
                    requester_id=self.customer_id, status="DRAFT", description=""
                )
                if kwargs.get("update_fields") is not None:
                    kwargs["update_fields"] = set(kwargs["update_fields"]) | {
                        "service_request"
                    }
            if (
                self.conversation_type == "SERVICE"
                and self.service_request.requester_id != self.customer_id
            ):
                raise ValueError("The service room customer must be the request owner.")
            return super().save(*args, **kwargs)

    def __str__(self):
        return f"Conversation {self.id}"


class ConversationReadState(models.Model):
    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="read_states"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="conversation_read_states",
    )
    last_read_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["conversation", "user"], name="conversation_read_state_unique"
            )
        ]
        indexes = [
            models.Index(
                fields=["user", "conversation"], name="conversation_read_user_idx"
            )
        ]


class Message(models.Model):
    class MessageType(models.TextChoices):
        USER = "USER", "User"
        SYSTEM = "SYSTEM", "System"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="messages"
    )
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="conversation_messages",
    )
    message_type = models.CharField(
        max_length=10, choices=MessageType.choices, default=MessageType.USER
    )
    body = models.TextField(blank=True)
    event_code = models.CharField(max_length=60, blank=True)
    event_key = models.CharField(max_length=255, unique=True, null=True, blank=True)
    idempotency_key = models.CharField(max_length=255, null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    # "Delete" is a soft delete on purpose. The row, its sender and its
    # timestamps stay for audit and for dispute handling; what goes is the
    # content, and both sides are told it is gone rather than left looking at
    # a message the other person can no longer see.
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="deleted_conversation_messages",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def is_deleted(self):
        return self.deleted_at is not None

    @property
    def deleted_label(self):
        """What the other side reads in place of the message."""
        if not self.is_deleted:
            return ""
        return "This message was deleted."

    class Meta:
        indexes = [
            models.Index(
                fields=["conversation", "-created_at"],
                name="conversation_msg_created_idx",
            ),
            models.Index(
                fields=["conversation", "event_code"], name="conversation_msg_event_idx"
            ),
        ]
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["conversation", "sender", "event_key"],
                condition=Q(event_key__isnull=False),
                name="conversation_msg_event_scoped_unique",
            ),
            models.UniqueConstraint(
                fields=["conversation", "sender", "idempotency_key"],
                condition=Q(idempotency_key__isnull=False),
                name="conversation_msg_idempotency_unique",
            ),
        ]

    def __str__(self):
        return f"Message {self.id}"


class ConversationAttachment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    message = models.ForeignKey(
        Message, on_delete=models.CASCADE, related_name="attachments"
    )
    file = models.FileField(
        upload_to=conversation_attachment_upload_to, storage=conversation_storage
    )
    private_storage = models.BooleanField(default=True)
    content_type = models.CharField(max_length=80)
    size = models.PositiveBigIntegerField()
    scan_status = models.CharField(
        max_length=16,
        choices=[
            ("PENDING", "Pending"),
            ("CLEAN", "Clean"),
            ("INFECTED", "Infected"),
            ("FAILED", "Failed"),
        ],
        default="PENDING",
        db_index=True,
    )
    scan_reason = models.CharField(max_length=500, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["message", "uploaded_at"], name="conversation_att_created_idx"
            )
        ]


class Offer(models.Model):
    class Status(models.TextChoices):
        PROPOSED = "PROPOSED", "Proposed"
        REVISION_REQUESTED = "REVISION_REQUESTED", "Revision requested"
        COUNTERED = "COUNTERED", "Countered"
        ACCEPTED = "ACCEPTED", "Accepted"
        DECLINED = "DECLINED", "Declined"
        EXPIRED = "EXPIRED", "Expired"
        CANCELLED = "CANCELLED", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="offers"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="offers_created",
    )
    parent_offer = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="counter_offers",
    )
    labour_amount = models.DecimalField(max_digits=12, decimal_places=2)
    materials_amount = models.DecimalField(max_digits=12, decimal_places=2)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2)
    version = models.PositiveIntegerField(default=1)
    transport_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    additional_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    additional_charge_details = models.TextField(max_length=1500, blank=True)
    payload_hash = models.CharField(max_length=64, blank=True)
    # "Materials included in this price": the itemised materials are supplied
    # from within the labour price, so the materials subtotal is recorded for
    # disclosure but never added again to the payable total.
    materials_included = models.BooleanField(default=False)
    scheduled_start = models.DateTimeField()
    scheduled_end = models.DateTimeField()
    # Canonical agreed duration that drives the service countdown once the
    # booking starts. scheduled_end is computed from scheduled_start + duration.
    duration_value = models.PositiveIntegerField(default=1)
    duration_unit = models.CharField(
        max_length=8,
        choices=[("HOURS", "Hours"), ("DAYS", "Days"), ("WEEKS", "Weeks")],
        default="DAYS",
    )
    # Derived server-side from the schedule; the artisan is never asked for an
    # expiry date. Kept so an old quote still stops being acceptable.
    expires_at = models.DateTimeField()
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PROPOSED, db_index=True
    )
    idempotency_key = models.CharField(max_length=255, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Phase 6 — edit/withdraw window.
    # An offer may be edited or withdrawn only while it is still pending, still
    # unexpired, and still unread by the customer. The moment the customer has
    # seen it, the price is on the table and can only be answered, not changed.
    customer_seen_at = models.DateTimeField(null=True, blank=True, db_index=True)
    # A withdrawal is a soft delete: the row and every revision stay for audit,
    # but the offer can never be accepted or superseded again.
    withdrawn_at = models.DateTimeField(null=True, blank=True)
    withdrawn_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="offers_withdrawn",
    )
    withdrawal_reason = models.TextField(max_length=500, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(labour_amount__gte=0), name="offer_labour_nonnegative"
            ),
            models.CheckConstraint(
                condition=Q(materials_amount__gte=0), name="offer_materials_nonnegative"
            ),
            models.CheckConstraint(
                condition=Q(transport_amount__gte=0, additional_amount__gte=0),
                name="offer_extra_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(total_amount__gt=0), name="offer_total_positive"
            ),
            # Materials are charged on top unless they are declared included in
            # the labour price, in which case the itemised subtotal is recorded
            # for disclosure but never added to the payable total.
            models.CheckConstraint(
                condition=(
                    Q(
                        materials_included=True,
                        total_amount=models.F("labour_amount")
                        + models.F("transport_amount")
                        + models.F("additional_amount"),
                    )
                    | Q(
                        materials_included=False,
                        total_amount=models.F("labour_amount")
                        + models.F("materials_amount")
                        + models.F("transport_amount")
                        + models.F("additional_amount"),
                    )
                ),
                name="offer_total_matches_parts",
            ),
            models.UniqueConstraint(
                fields=["created_by", "idempotency_key"],
                condition=Q(idempotency_key__isnull=False),
                name="offer_creator_idempotency_unique",
            ),
        ]
        indexes = [
            models.Index(
                fields=["conversation", "status", "-created_at"],
                name="offer_conversation_status_idx",
            ),
            models.Index(
                fields=["expires_at", "status"], name="offer_expiry_status_idx"
            ),
            models.Index(
                fields=["conversation", "parent_offer"], name="offer_parent_idx"
            ),
        ]

    IMMUTABLE_STATUSES = {
        Status.ACCEPTED,
        Status.DECLINED,
        Status.EXPIRED,
        Status.CANCELLED,
        Status.COUNTERED,
    }
    _COMMERCIAL_FIELDS = (
        "version",
        "transport_amount",
        "additional_amount",
        "additional_charge_details",
        "payload_hash",
        "labour_amount",
        "materials_amount",
        "total_amount",
        "materials_included",
        "scheduled_start",
        "scheduled_end",
        "duration_value",
        "duration_unit",
        "expires_at",
        "created_by_id",
        "parent_offer_id",
    )

    EDITABLE_STATUSES = {Status.PROPOSED}

    @property
    def is_withdrawn(self):
        return self.withdrawn_at is not None

    @property
    def is_pending(self):
        return self.status == self.Status.PROPOSED and self.expires_at > timezone.now()

    def edit_window_state(self, *, now=None):
        """Why this offer can (or cannot) still be edited or withdrawn.

        Returned as ``(allowed, reason)`` so every caller — service, API and
        template — reports the same rule instead of inventing its own.
        """
        now = now or timezone.now()
        if self.withdrawn_at is not None:
            return False, "This offer has already been withdrawn."
        if self.status != self.Status.PROPOSED:
            if self.status == self.Status.ACCEPTED:
                return False, "The customer has already accepted this offer."
            if self.status == self.Status.COUNTERED:
                return False, "A newer version of this offer has already been sent."
            if self.status == self.Status.DECLINED:
                return False, "The customer has already declined this offer."
            if self.status == self.Status.EXPIRED:
                return False, "This offer has expired."
            return False, "This offer is no longer pending."
        if self.expires_at <= now:
            return False, "This offer has expired and can no longer be changed."
        if self.customer_seen_at is not None:
            return False, (
                "The customer has already seen this offer. Send a new version "
                "instead of changing one they are looking at."
            )
        return True, ""

    def mark_seen_by_customer(self, *, when=None):
        """Close the edit window: the customer has been shown this price."""
        if self.customer_seen_at is None:
            self.customer_seen_at = when or timezone.now()
            self.save(update_fields=["customer_seen_at", "updated_at"])
            return True
        return False

    def snapshot(self):
        """A full, self-contained copy of the commercial terms for the audit trail."""
        return {
            "offer_id": str(self.id),
            "version": self.version,
            "status": self.status,
            "labour_amount": str(self.labour_amount),
            "materials_amount": str(self.materials_amount),
            "transport_amount": str(self.transport_amount),
            "additional_amount": str(self.additional_amount),
            "additional_charge_details": self.additional_charge_details,
            "total_amount": str(self.total_amount),
            "materials_included": self.materials_included,
            "scheduled_start": self.scheduled_start.isoformat()
            if self.scheduled_start
            else None,
            "scheduled_end": self.scheduled_end.isoformat() if self.scheduled_end else None,
            "duration_value": self.duration_value,
            "duration_unit": self.duration_unit,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "materials": [
                {
                    "name": item.name,
                    "unit": item.unit,
                    "quantity": str(item.quantity),
                    "unit_price": str(item.unit_price),
                    "line_total": str(item.line_total),
                }
                for item in self.materials.all()
            ],
        }

    def save(self, *args, **kwargs):
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).first()
            if previous and previous.status in self.IMMUTABLE_STATUSES:
                changed = [
                    field
                    for field in self._COMMERCIAL_FIELDS
                    if getattr(previous, field) != getattr(self, field)
                ]
                if previous.status != self.status:
                    raise ValueError(
                        "Terminal quotes cannot be reopened or superseded in place."
                    )
                if changed:
                    raise ValueError(
                        "Accepted, declined, expired, and cancelled offers are immutable."
                    )
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"Offer {self.id}"


class OfferMaterial(models.Model):
    """One itemized material line on an offer.

    The financial truth for these rows is stored here and recomputed by the
    backend from name, quantity and unit price: line total = quantity × unit
    price, and the offer's materials_amount is the sum of its line totals.
    Client-supplied totals are never trusted.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    offer = models.ForeignKey(
        Offer, on_delete=models.CASCADE, related_name="materials"
    )
    position = models.PositiveSmallIntegerField(default=0)
    name = models.CharField(max_length=120)
    unit = models.CharField(max_length=40, blank=True)
    quantity = models.DecimalField(max_digits=10, decimal_places=2)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    line_total = models.DecimalField(max_digits=12, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["position", "created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(quantity__gt=0), name="offer_material_quantity_positive"
            ),
            models.CheckConstraint(
                condition=Q(unit_price__gte=0), name="offer_material_price_nonnegative"
            ),
            models.CheckConstraint(
                condition=Q(line_total__gte=0), name="offer_material_line_nonnegative"
            ),
        ]
        indexes = [
            models.Index(fields=["offer", "position"], name="offer_material_order_idx"),
        ]

    def __str__(self):
        return f"OfferMaterial {self.name} x{self.quantity}"


class MediaPreservationHold(models.Model):
    release_reason = models.CharField(max_length=256, blank=True)
    released_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        editable=False,
        related_name="released_media_holds",
    )
    provider_reference = models.CharField(
        max_length=128, unique=True, null=True, blank=True, editable=False
    )
    """Staff-managed legal/chargeback holds; support disputes are also checked live."""
    service_request = models.ForeignKey(
        "services.ServiceRequest", on_delete=models.PROTECT, related_name="media_holds"
    )
    kind = models.CharField(
        max_length=16,
        choices=[
            ("LEGAL", "Legal preservation"),
            ("CHARGEBACK", "Chargeback investigation"),
            ("DISPUTE", "Dispute"),
        ],
    )
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    released_at = models.DateTimeField(null=True, blank=True)

    def clean(self):
        if not self.active and not self.release_reason.strip():
            from django.core.exceptions import ValidationError

            raise ValidationError(
                {
                    "release_reason": "A documented review reason is required before a hold is released."
                }
            )

    def save(self, *args, **kwargs):
        self.clean()
        from django.db import transaction
        from services.models import ServiceRequest

        with transaction.atomic():
            ServiceRequest.objects.select_for_update().get(pk=self.service_request_id)
            return super().save(*args, **kwargs)


class MediaPurge(models.Model):
    class Status(models.TextChoices):
        NOT_ELIGIBLE = "NOT_ELIGIBLE", "Not eligible"
        PENDING = "PENDING", "Pending"
        PROCESSING = "PROCESSING", "Processing"
        COMPLETE = "COMPLETE", "Complete"
        RETRY_REQUIRED = "RETRY_REQUIRED", "Retry required"

    service_request = models.OneToOneField(
        "services.ServiceRequest", on_delete=models.PROTECT, related_name="media_purge"
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.NOT_ELIGIBLE,
        db_index=True,
    )
    blocked_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True, db_index=True)
    attempts = models.PositiveIntegerField(default=0)
    audio_count = models.PositiveIntegerField(default=0)
    video_count = models.PositiveIntegerField(default=0)
    policy_version = models.CharField(max_length=12, default="1.0")
    # Deliberately no filename, object key, URL, exception text or media content.


class MediaDerivative(models.Model):
    attachment = models.ForeignKey(
        ConversationAttachment, on_delete=models.CASCADE, related_name="derivatives"
    )
    storage_key = models.CharField(max_length=512)
    kind = models.CharField(
        max_length=20,
        choices=[
            (k, k)
            for k in ("thumbnail", "preview", "waveform", "transcode", "chunk", "cache")
        ],
    )


class MediaRelocation(models.Model):
    """Retry-only journal; removed after verified copy + legacy origin deletion."""

    attachment = models.OneToOneField(
        ConversationAttachment, on_delete=models.PROTECT, related_name="relocation"
    )
    source_key = models.CharField(max_length=1024)
    target_key = models.CharField(max_length=1024)
    sha256 = models.CharField(max_length=64)
    byte_size = models.PositiveBigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)


class MediaPolicyIdentity(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    identity = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)


class OfferRevision(models.Model):
    """Immutable audit row for every edit and every withdrawal of an offer.

    A withdrawal is a soft delete: the offer row stays, marked withdrawn, and the
    terms the customer was originally shown are kept here so a dispute can be
    answered with what was actually on the table at each point in time.
    """

    class Action(models.TextChoices):
        CREATED = "CREATED", "Created"
        EDITED = "EDITED", "Edited"
        WITHDRAWN = "WITHDRAWN", "Withdrawn"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    offer = models.ForeignKey(
        Offer, on_delete=models.CASCADE, related_name="revisions"
    )
    action = models.CharField(max_length=20, choices=Action.choices)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="offer_revisions",
    )
    version = models.PositiveIntegerField(default=1)
    # The terms as they were *before* this action, so the sequence of revisions
    # reconstructs the whole negotiation.
    snapshot = models.JSONField(default=dict, blank=True)
    reason = models.TextField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["offer", "created_at"], name="offer_revision_offer_idx")
        ]

    def __str__(self):
        return f"Offer {self.offer_id} · {self.action} · v{self.version}"


