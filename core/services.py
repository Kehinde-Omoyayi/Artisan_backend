import hashlib
import json
import logging
from datetime import timedelta
from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from .models import AuditLog, IdempotencyRecord, OutboxEvent
from .policies import IDEMPOTENCY_TTL_SECONDS

logger = logging.getLogger(__name__)

def request_fingerprint(payload):
    canonical = json.dumps(payload or {}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

IDEMPOTENCY_LEASE_SECONDS = 120


def json_safe(value):
    """Return a JSON-column-safe copy of ``value`` (datetimes, Decimals and UUIDs become strings)."""
    return json.loads(json.dumps(value, cls=DjangoJSONEncoder))


def get_or_create_idempotency(*, actor, operation, key, payload, resource_type="", resource_id=""):
    """Claim an idempotency key for ``actor``/``operation``.

    Returns ``(record, created)``. An existing, unexpired record is replayed only when the request
    fingerprint matches; a different request reusing the key is rejected. A record that never received a
    response and is older than the lease is treated as abandoned (worker crash) and is re-claimed.
    """
    key = str(key or "").strip()
    if not key or len(key) > 255: raise ValidationError({"Idempotency-Key": "Idempotency-Key is required and must be 1 to 255 characters for this operation."})
    if actor is None or not getattr(actor, "is_authenticated", False): raise ValidationError({"Idempotency-Key": "Idempotent operations require an authenticated user."})
    fingerprint = request_fingerprint({"payload": payload, "resource": [str(resource_type or ""), str(resource_id or "")]})
    now = timezone.now()
    existing = IdempotencyRecord.objects.filter(actor=actor, operation=operation, key=key, expires_at__gt=now).first()
    if existing:
        if existing.request_hash != fingerprint: raise ValidationError({"Idempotency-Key": "This key was already used with a different request."})
        abandoned = existing.response_status is None and existing.created_at < now - timedelta(seconds=IDEMPOTENCY_LEASE_SECONDS)
        if not abandoned: return existing, False
        IdempotencyRecord.objects.filter(pk=existing.pk, response_status__isnull=True).delete()
    # An expired record would otherwise violate the unique constraint and replay a stale response.
    IdempotencyRecord.objects.filter(actor=actor, operation=operation, key=key, expires_at__lte=now).delete()
    try:
        with transaction.atomic(): record = IdempotencyRecord.objects.create(actor=actor, operation=operation, key=key, request_hash=fingerprint, resource_type=resource_type, resource_id=str(resource_id or ""), expires_at=now + timedelta(seconds=getattr(settings, "IDEMPOTENCY_TTL_SECONDS", IDEMPOTENCY_TTL_SECONDS)))
    except IntegrityError:
        record = IdempotencyRecord.objects.get(actor=actor, operation=operation, key=key)
        if record.request_hash != fingerprint: raise ValidationError({"Idempotency-Key": "This key was already used with a different request."}) from None
        return record, False
    return record, True

def store_idempotency_response(record, *, status_code, body, resource_type="", resource_id=""):
    IdempotencyRecord.objects.filter(pk=record.pk).update(response_status=status_code, response_body=json_safe(body), resource_type=resource_type or record.resource_type, resource_id=str(resource_id or record.resource_id or ""))

def create_audit_log(*, action, resource_type, resource_id="", actor=None, metadata=None, request_id="", ip_hash="", user_agent_hash=""):
    try:
        with transaction.atomic(): return AuditLog.objects.create(actor=actor if getattr(actor, "pk", None) else None, request_id=str(request_id or "")[:64], action=action, resource_type=resource_type, resource_id=str(resource_id or ""), metadata=json_safe(metadata or {}), ip_hash=ip_hash, user_agent_hash=user_agent_hash)
    except Exception: logger.exception("audit_log_write_failed action=%s resource=%s id=%s", action, resource_type, resource_id); return None

def enqueue_outbox(*, event_type, event_key, payload):
    """Idempotently record an outbox event. A repeated key is a no-op; differing data is logged, not raised."""
    payload = json_safe(payload)
    try:
        with transaction.atomic(): event, created = OutboxEvent.objects.get_or_create(event_key=event_key, defaults={"event_type": event_type, "payload": payload})
    except IntegrityError:
        event = OutboxEvent.objects.get(event_key=event_key); created = False
    if not created and (event.event_type != event_type or event.payload != payload): logger.warning("outbox_event_key_reused_with_different_data key=%s", event_key)
    return event
