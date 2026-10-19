import uuid
from django.conf import settings
from django.db import models
from django.utils import timezone

class IdempotencyRecord(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="idempotency_records")
    operation = models.CharField(max_length=100)
    key = models.CharField(max_length=255)
    request_hash = models.CharField(max_length=64)
    resource_type = models.CharField(max_length=100, blank=True)
    resource_id = models.CharField(max_length=100, blank=True)
    response_status = models.PositiveSmallIntegerField(null=True, blank=True)
    response_body = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    class Meta:
        constraints = [models.UniqueConstraint(fields=["actor", "operation", "key"], name="core_idem_actor_operation_key")]
        indexes = [models.Index(fields=["operation", "created_at"], name="core_idem_ope_crt_idx"), models.Index(fields=["expires_at"], name="core_idem_expires_idx")]

class AuditLog(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="marketplace_audit_logs")
    request_id = models.CharField(max_length=64, blank=True, db_index=True)
    action = models.CharField(max_length=120)
    resource_type = models.CharField(max_length=100)
    resource_id = models.CharField(max_length=100, blank=True)
    ip_hash = models.CharField(max_length=64, blank=True)
    user_agent_hash = models.CharField(max_length=64, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["resource_type", "resource_id", "created_at"], name="core_audit_resource_idx"), models.Index(fields=["actor", "created_at"], name="core_audit_actor_created_idx")]

class OutboxEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event_type = models.CharField(max_length=120)
    event_key = models.CharField(max_length=255, unique=True)
    payload = models.JSONField(default=dict)
    attempts = models.PositiveSmallIntegerField(default=0)
    available_at = models.DateTimeField(default=timezone.now)
    processed_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    dead_lettered_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        ordering = ["created_at"]
        indexes = [models.Index(fields=["processed_at", "available_at"], name="core_outbox_ready_idx"), models.Index(fields=["event_type", "created_at"], name="core_outbox_type_created_idx")]
