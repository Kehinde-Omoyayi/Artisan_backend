import uuid
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion

class Migration(migrations.Migration):
    initial = True
    dependencies = [(settings.AUTH_USER_MODEL.split('.')[0], '0001_initial')]
    operations = [
        migrations.CreateModel(name="AuditLog", fields=[("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)), ("request_id", models.CharField(blank=True, db_index=True, max_length=64)), ("action", models.CharField(max_length=120)), ("resource_type", models.CharField(max_length=100)), ("resource_id", models.CharField(blank=True, max_length=100)), ("metadata", models.JSONField(blank=True, default=dict)), ("created_at", models.DateTimeField(auto_now_add=True)), ("actor", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="marketplace_audit_logs", to=settings.AUTH_USER_MODEL))]),
        migrations.CreateModel(name="IdempotencyRecord", fields=[("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)), ("operation", models.CharField(max_length=100)), ("key", models.CharField(max_length=255)), ("request_hash", models.CharField(max_length=64)), ("resource_type", models.CharField(blank=True, max_length=100)), ("resource_id", models.CharField(blank=True, max_length=100)), ("response_status", models.PositiveSmallIntegerField(blank=True, null=True)), ("response_body", models.JSONField(blank=True, default=dict)), ("created_at", models.DateTimeField(auto_now_add=True)), ("expires_at", models.DateTimeField()), ("actor", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="idempotency_records", to=settings.AUTH_USER_MODEL))]),
        migrations.CreateModel(name="OutboxEvent", fields=[("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)), ("event_type", models.CharField(max_length=120)), ("event_key", models.CharField(max_length=255, unique=True)), ("payload", models.JSONField(default=dict)), ("attempts", models.PositiveSmallIntegerField(default=0)), ("available_at", models.DateTimeField()), ("processed_at", models.DateTimeField(blank=True, null=True)), ("last_error", models.TextField(blank=True)), ("created_at", models.DateTimeField(auto_now_add=True)), ("updated_at", models.DateTimeField(auto_now=True))]),
        migrations.AddIndex(model_name="auditlog", index=models.Index(fields=["resource_type", "resource_id", "created_at"], name="core_audit_resource_idx")),
        migrations.AddIndex(model_name="auditlog", index=models.Index(fields=["actor", "created_at"], name="core_audit_actor_created_idx")),
        migrations.AddConstraint(model_name="idempotencyrecord", constraint=models.UniqueConstraint(fields=["actor", "operation", "key"], name="core_idem_actor_operation_key")),
        migrations.AddIndex(model_name="idempotencyrecord", index=models.Index(fields=["operation", "created_at"], name="core_idem_operation_created_idx")),
        migrations.AddIndex(model_name="idempotencyrecord", index=models.Index(fields=["expires_at"], name="core_idem_expires_idx")),
        migrations.AddIndex(model_name="outboxevent", index=models.Index(fields=["processed_at", "available_at"], name="core_outbox_ready_idx")),
        migrations.AddIndex(model_name="outboxevent", index=models.Index(fields=["event_type", "created_at"], name="core_outbox_type_created_idx")),
    ]
