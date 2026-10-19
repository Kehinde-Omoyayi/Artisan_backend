import logging
from datetime import timedelta
from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from .models import AuditLog, IdempotencyRecord, OutboxEvent

logger = logging.getLogger(__name__)
MAX_OUTBOX_ATTEMPTS = 12


def _dispatch(event):
    if event.event_type == "notification.email":
        from notifications.tasks import send_email_notification
        send_email_notification.delay(**event.payload)
    elif event.event_type == "maintenance.reconcile":
        from payments.tasks import reconcile_stale_payments
        reconcile_stale_payments.delay()
    elif event.event_type == "automation.fanout_service_request":
        from notifications.tasks import fanout_service_request
        fanout_service_request.delay(event.payload["service_request_id"])
    elif event.event_type == "media.scan":
        from conversations.signals_tasks import scan_conversation_attachment
        scan_conversation_attachment.delay(event.payload["attachment_id"])
    else: raise RuntimeError(f"Unsupported outbox event type: {event.event_type}")

@shared_task(bind=True, max_retries=8, retry_backoff=True, retry_backoff_max=600, retry_jitter=True)
def dispatch_outbox_events(self, batch_size=100):
    now = timezone.now(); event_ids = list(OutboxEvent.objects.filter(processed_at__isnull=True, available_at__lte=now, dead_lettered_at__isnull=True).order_by("created_at").values_list("id", flat=True)[:batch_size]); dispatched = 0
    for event_id in event_ids:
        with transaction.atomic():
            event = OutboxEvent.objects.select_for_update(of=("self",)).filter(pk=event_id, processed_at__isnull=True, dead_lettered_at__isnull=True).first()
            if not event: continue
            event.attempts += 1; event.available_at = now + timedelta(seconds=60); event.save(update_fields=["attempts", "available_at", "updated_at"])
            event_type, payload, attempts = event.event_type, dict(event.payload), event.attempts
        try:
            _dispatch(type("OutboxProxy", (), {"event_type": event_type, "payload": payload})())
        except Exception as exc:
            with transaction.atomic():
                current = OutboxEvent.objects.select_for_update(of=("self",)).get(pk=event_id); current.last_error = str(exc)[:2000]; current.available_at = timezone.now() + timedelta(seconds=min(900, 2 ** min(current.attempts, 10))); current.dead_lettered_at = timezone.now() if current.attempts >= MAX_OUTBOX_ATTEMPTS else None; current.save(update_fields=["last_error", "available_at", "dead_lettered_at", "updated_at"])
                if current.dead_lettered_at: logger.error("outbox_dead_letter event=%s attempts=%s", event_id, attempts)
            continue
        with transaction.atomic():
            current = OutboxEvent.objects.select_for_update(of=("self",)).filter(pk=event_id, processed_at__isnull=True).first()
            if current: current.processed_at = timezone.now(); current.last_error = ""; current.save(update_fields=["processed_at", "last_error", "updated_at"]); dispatched += 1
    return {"status": "ok", "dispatched": dispatched}

@shared_task
def purge_core_retention():
    now = timezone.now(); audit_days = getattr(settings, "AUDIT_LOG_RETENTION_DAYS", 365); outbox_days = getattr(settings, "OUTBOX_RETENTION_DAYS", 30); idem_days = getattr(settings, "IDEMPOTENCY_RETENTION_DAYS", 2)
    AuditLog.objects.filter(created_at__lt=now - timedelta(days=audit_days)).delete(); OutboxEvent.objects.filter(processed_at__isnull=False, processed_at__lt=now - timedelta(days=outbox_days)).delete(); IdempotencyRecord.objects.filter(expires_at__lt=now - timedelta(days=idem_days)).delete()

@shared_task
def purge_conversation_data(batch_size=500):
    """Deprecated: whole conversations are no longer removed by retention tasks."""
    return {"status": "retired", "deleted": 0}


@shared_task
def replay_dead_letter_outbox(batch_size=100):
    now = timezone.now(); reset = 0
    ids = list(OutboxEvent.objects.filter(dead_lettered_at__isnull=False, processed_at__isnull=True).order_by("created_at").values_list("id", flat=True)[:batch_size])
    for event_id in ids:
        with transaction.atomic():
            event = OutboxEvent.objects.select_for_update(of=("self",)).filter(pk=event_id, dead_lettered_at__isnull=False, processed_at__isnull=True).first()
            if not event: continue
            event.dead_lettered_at = None; event.available_at = now; event.last_error = ""; event.attempts = 0
            event.save(update_fields=["dead_lettered_at", "available_at", "last_error", "attempts", "updated_at"]); reset += 1
    if reset:
        try: dispatch_outbox_events.delay(batch_size=max(reset, 100))
        except Exception: logger.exception("dead_letter_dispatch_enqueue_failed reset=%s", reset)
    return {"status": "ok", "replayed": reset}
