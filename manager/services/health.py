"""Technical health for the console.

Every probe is cheap and bounded: a single-row query for database latency, a
cache round trip, counts on indexed status columns, and the freshness of the last
recorded signal. Nothing here calls a provider synchronously - provider health is
inferred from what the provider already told us (last webhook, last failure).
"""
from __future__ import annotations

import os
import time

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.db.models import Count
from django.utils import timezone

from core.models import OutboxEvent
from manager.models import AnalyticsEvent, BackgroundJob, Campaign, OperationalSnapshot
from manager.services import jobs as job_service
from manager.services import snapshots as snapshot_service


def _timed(fn):
    started = time.perf_counter()
    try:
        value = fn()
        return round((time.perf_counter() - started) * 1000, 2), value, None
    except Exception as exc:  # pragma: no cover - probe failures are reported, not raised
        return round((time.perf_counter() - started) * 1000, 2), None, str(exc)[:200]


def _database():
    ms, _, error = _timed(lambda: connection.cursor().execute("SELECT 1").fetchone())
    return {"status": "healthy" if error is None else "error", "latency_ms": ms, "error": error}


def _cache():
    def probe():
        cache.set("manager:health:probe", "1", 10)
        return cache.get("manager:health:probe")

    ms, value, error = _timed(probe)
    healthy = value == "1" and error is None
    return {"status": "healthy" if healthy else "degraded", "latency_ms": ms, "backend": cache.__class__.__name__}


def _storage():
    """Check the configured media root is writable. Never creates a persistent file."""
    root = str(getattr(settings, "MEDIA_ROOT", "") or "")
    if not root:
        return {"status": "unknown", "detail": "No local media root configured."}
    try:
        writable = os.access(root, os.W_OK) if os.path.isdir(root) else False
    except OSError:
        writable = False
    return {"status": "healthy" if writable else "degraded", "detail": "Local media root writable." if writable else "Media root is not writable."}


def _outbox():
    now = timezone.now()
    pending = OutboxEvent.objects.filter(processed_at__isnull=True, dead_lettered_at__isnull=True).count()
    dead = OutboxEvent.objects.filter(dead_lettered_at__isnull=False).count()
    oldest = OutboxEvent.objects.filter(processed_at__isnull=True).order_by("created_at").values_list(
        "created_at", flat=True).first()
    return {
        "status": "healthy" if dead == 0 else "degraded",
        "pending": pending,
        "dead_lettered": dead,
        "oldest_pending_seconds": round((now - oldest).total_seconds()) if oldest else None,
    }


def _payments():
    from payments.models import EscrowPayment, PaymentWebhookEvent

    now = timezone.now()
    last_webhook = PaymentWebhookEvent.objects.order_by("-received_at").values_list("received_at", flat=True).first()
    failed_24h = EscrowPayment.objects.filter(status=EscrowPayment.Status.FAILED,
                                              created_at__gte=now - timezone.timedelta(hours=24)).count()
    has_keys = bool(getattr(settings, "FLW_SECRET_KEY", ""))
    return {
        "status": "healthy" if has_keys else "misconfigured",
        "configured": has_keys,
        "last_webhook_at": last_webhook.isoformat() if last_webhook else None,
        "failed_payments_24h": failed_24h,
        "detail": "Provider keys present; health is inferred from webhook activity." if has_keys else "Provider keys are missing.",
    }


def _notifications():
    from notifications.models import InAppNotification

    now = timezone.now()
    last = InAppNotification.objects.order_by("-created_at").values_list("created_at", flat=True).first()
    return {
        "status": "healthy",
        "last_delivery_at": last.isoformat() if last else None,
        "delivered_24h": InAppNotification.objects.filter(created_at__gte=now - timezone.timedelta(hours=24)).count(),
    }


def _campaigns():
    now = timezone.now()
    dispatching = Campaign.objects.filter(status=Campaign.Status.DISPATCHING).count()
    stale = Campaign.objects.filter(status=Campaign.Status.DISPATCHING,
                                    updated_at__lt=now - timezone.timedelta(hours=1)).count()
    return {
        "status": "healthy" if stale == 0 else "degraded",
        "dispatching": dispatching,
        "stale_dispatch": stale,
        "scheduled": Campaign.objects.filter(status=Campaign.Status.SCHEDULED).count(),
    }


def _analytics():
    now = timezone.now()
    last = AnalyticsEvent.objects.order_by("-occurred_at").values_list("occurred_at", flat=True).first()
    lag = round((now - last).total_seconds()) if last else None
    return {
        "status": "healthy" if lag is not None and lag < 86400 else "degraded",
        "last_event_at": last.isoformat() if last else None,
        "lag_seconds": lag,
        "events_24h": AnalyticsEvent.objects.filter(occurred_at__gte=now - timezone.timedelta(hours=24)).count(),
    }


def _workers():
    now = timezone.now()
    latest_success = BackgroundJob.objects.filter(status=BackgroundJob.Status.SUCCEEDED).order_by(
        "-finished_at").values_list("finished_at", flat=True).first()
    return {
        "status": "healthy" if latest_success and (now - latest_success).total_seconds() < 3600 else "idle",
        "last_success_at": latest_success.isoformat() if latest_success else None,
        "queues": [
            {"queue": row["queue"], "queued": row["n"]}
            for row in BackgroundJob.objects.filter(status=BackgroundJob.Status.QUEUED)
            .values("queue").annotate(n=Count("pk"))
        ],
    }


def snapshot(*, now=None):
    now = now or timezone.now()
    payload = {
        "generated_at": now.isoformat(),
        "environment": getattr(settings, "ENVIRONMENT", "unknown"),
        "debug": bool(getattr(settings, "DEBUG", False)),
        "database": _database(),
        "cache": _cache(),
        "storage": _storage(),
        "outbox": _outbox(),
        "payments": _payments(),
        "notifications": _notifications(),
        "jobs": job_service.queue_health(),
        "workers": _workers(),
        "snapshots": {
            "interval_seconds": snapshot_service.interval_seconds(),
            "last_captured_for": OperationalSnapshot.objects.order_by("-captured_for").values_list(
                "captured_for", flat=True).first(),
        },
        "campaigns": _campaigns(),
        "analytics": _analytics(),
    }
    last_capture = payload["snapshots"]["last_captured_for"]
    payload["snapshots"]["last_captured_for"] = last_capture.isoformat() if last_capture else None
    payload["overall"] = "healthy" if all(
        section.get("status") in {"healthy", "idle", "unknown"}
        for section in (payload["database"], payload["cache"], payload["outbox"], payload["analytics"])
    ) else "degraded"
    return payload


def latency_percentiles(window_minutes=60):
    """Read the recorded latency samples the middleware keeps (no live traffic probe)."""
    from core.observability import latency_samples

    return latency_samples(window_minutes=window_minutes)
