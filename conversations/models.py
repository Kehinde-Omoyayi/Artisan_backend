import os
import uuid
from pathlib import Path

from django.conf import settings
from django.db import models
from django.db.models import Q


def conversation_attachment_upload_to(instance, filename):
    ext = Path(os.path.basename(filename)).suffix.lower()
    return f"conversation_attachments/{instance.message.conversation_id}/{uuid.uuid4().hex}{ext}"


class Conversation(models.Model):
    class ConversationType(models.TextChoices):
        SERVICE = "SERVICE", "Service"
        DIRECT = "DIRECT", "Direct"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation_type = models.CharField(max_length=16, choices=ConversationType.choices, default=ConversationType.SERVICE, db_index=True)
    direct_key = models.CharField(max_length=80, unique=True, null=True, blank=True, editable=False)
    # Legacy marketplace roles remain for service conversations. Generic membership is
    # represented by participants so any authenticated user pair can have a direct chat.
    customer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="marketplace_conversations", null=True, blank=True)
    artisan = models.ForeignKey("home.ArtisanProfile", on_delete=models.PROTECT, related_name="marketplace_conversations", null=True, blank=True)
    participants = models.ManyToManyField(settings.AUTH_USER_MODEL, related_name="conversations", blank=True)
    service_request = models.OneToOneField("services.ServiceRequest", on_delete=models.PROTECT, related_name="conversation", null=True, blank=True)
    draft = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["customer", "artisan"], condition=Q(service_request__isnull=True, conversation_type="SERVICE"), name="conv_one_draft_per_pair"),
        ]
        indexes = [
            models.Index(fields=["customer", "-updated_at"], name="conv_customer_upd_idx"),
            models.Index(fields=["artisan", "-updated_at"], name="conv_artisan_upd_idx"),
            models.Index(fields=["conversation_type", "-updated_at"], name="conv_type_updated_idx"),
        ]
        ordering = ["-updated_at"]

    def __str__(self):
        return f"Conversation {self.id}"


class ConversationReadState(models.Model):
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="read_states")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="conversation_read_states")
    last_read_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["conversation", "user"], name="conversation_read_state_unique")]
        indexes = [models.Index(fields=["user", "conversation"], name="conversation_read_user_idx")]


class Message(models.Model):
    class MessageType(models.TextChoices):
        USER = "USER", "User"
        SYSTEM = "SYSTEM", "System"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="messages")
    sender = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="conversation_messages")
    message_type = models.CharField(max_length=10, choices=MessageType.choices, default=MessageType.USER)
    body = models.TextField(blank=True)
    event_code = models.CharField(max_length=60, blank=True)
    event_key = models.CharField(max_length=255, unique=True, null=True, blank=True)
    idempotency_key = models.CharField(max_length=255, null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["conversation", "-created_at"], name="conversation_msg_created_idx"), models.Index(fields=["conversation", "event_code"], name="conversation_msg_event_idx")]
        ordering = ["created_at"]
        constraints = [models.UniqueConstraint(fields=["conversation", "sender", "event_key"], condition=Q(event_key__isnull=False), name="conversation_msg_event_scoped_unique"), models.UniqueConstraint(fields=["conversation", "sender", "idempotency_key"], condition=Q(idempotency_key__isnull=False), name="conversation_msg_idempotency_unique")]

    def __str__(self):
        return f"Message {self.id}"


class ConversationAttachment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(upload_to=conversation_attachment_upload_to)
    content_type = models.CharField(max_length=80)
    size = models.PositiveBigIntegerField()
    scan_status = models.CharField(max_length=16, choices=[("PENDING", "Pending"), ("CLEAN", "Clean"), ("INFECTED", "Infected"), ("FAILED", "Failed")], default="PENDING", db_index=True)
    scan_reason = models.CharField(max_length=500, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["message", "uploaded_at"], name="conversation_att_created_idx")]


class Offer(models.Model):
    class Status(models.TextChoices):
        PROPOSED = "PROPOSED", "Proposed"
        COUNTERED = "COUNTERED", "Countered"
        ACCEPTED = "ACCEPTED", "Accepted"
        DECLINED = "DECLINED", "Declined"
        EXPIRED = "EXPIRED", "Expired"
        CANCELLED = "CANCELLED", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="offers")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="offers_created")
    parent_offer = models.ForeignKey("self", on_delete=models.PROTECT, null=True, blank=True, related_name="counter_offers")
    labour_amount = models.DecimalField(max_digits=12, decimal_places=2)
    materials_amount = models.DecimalField(max_digits=12, decimal_places=2)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2)
    materials_included = models.BooleanField(default=False)
    scope = models.TextField(max_length=5000)
    notes = models.TextField(max_length=3000, blank=True)
    scheduled_start = models.DateTimeField()
    scheduled_end = models.DateTimeField()
    expires_at = models.DateTimeField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PROPOSED, db_index=True)
    idempotency_key = models.CharField(max_length=255, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(condition=Q(labour_amount__gte=0), name="offer_labour_nonnegative"),
            models.CheckConstraint(condition=Q(materials_amount__gte=0), name="offer_materials_nonnegative"),
            models.CheckConstraint(condition=Q(total_amount__gt=0), name="offer_total_positive"),
            models.CheckConstraint(condition=Q(total_amount=models.F("labour_amount") + models.F("materials_amount")), name="offer_total_matches_parts"),
            models.UniqueConstraint(fields=["created_by", "idempotency_key"], condition=Q(idempotency_key__isnull=False), name="offer_creator_idempotency_unique"),
        ]
        indexes = [
            models.Index(fields=["conversation", "status", "-created_at"], name="offer_conversation_status_idx"),
            models.Index(fields=["expires_at", "status"], name="offer_expiry_status_idx"),
            models.Index(fields=["conversation", "parent_offer"], name="offer_parent_idx"),
        ]

    IMMUTABLE_STATUSES = {Status.ACCEPTED, Status.DECLINED, Status.EXPIRED, Status.CANCELLED}
    _COMMERCIAL_FIELDS = ("labour_amount", "materials_amount", "total_amount", "materials_included", "scope", "notes", "scheduled_start", "scheduled_end", "expires_at", "created_by_id", "parent_offer_id")

    def save(self, *args, **kwargs):
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).first()
            if previous and previous.status in self.IMMUTABLE_STATUSES:
                changed = [field for field in self._COMMERCIAL_FIELDS if getattr(previous, field) != getattr(self, field)]
                if changed: raise ValueError("Accepted, declined, expired, and cancelled offers are immutable.")
        return super().save(*args, **kwargs)

    def __str__(self): return f"Offer {self.id}"
