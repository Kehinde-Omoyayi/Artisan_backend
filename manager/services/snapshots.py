"""Configurable operational snapshot engine.

The default cadence is one minute. Each capture writes one immutable row per
metric (``OperationalSnapshot``), so history is never overwritten. Cheap gauges
come from indexed counts; anything expensive is folded through the aggregate
buckets first, so a minute-by-minute run is a handful of indexed queries rather
than a full-table aggregation.

Nothing here runs inside a user request except the status read.
"""
from __future__ import annotations

import time

from django.core.cache import cache
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from booking.models import Booking
from conversations.models import Conversation, Offer
from home.models import ArtisanProfile, CustomerProfile
from manager.models import OperationalSnapshot, SnapshotIntervalSetting
from payments.models import EscrowPayment, EscrowRelease
from services.models import ServiceRequest
from support.models import SupportCase, SupportFinancialAction

from manager.config_spec import SNAPSHOT_INTERVALS
from manager.services.config_engine import current

CALC_VERSION = "v1"
STATUS_CACHE_KEY = "manager:snapshot:status:v1"
STATUS_TTL = 20


def interval_seconds():
    configured = int(current("analytics.snapshot_interval_seconds") or 300)
    setting = SnapshotIntervalSetting.objects.filter(pk="operational").first()
    if setting:
        return setting.interval_seconds
    return configured


def set_interval(seconds, *, user):
    seconds = int(seconds)
    if seconds not in SNAPSHOT_INTERVALS and not (60 <= seconds <= 86400):
        raise ValueError("Choose a supported interval between one minute and 24 hours.")
    row, _ = SnapshotIntervalSetting.objects.update_or_create(
        pk="operational", defaults={"interval_seconds": seconds, "changed_by": user}
    )
    cache.delete(STATUS_CACHE_KEY)
    return row


def _floor(now, seconds):
    epoch = int(now.timestamp())
    return timezone.datetime.fromtimestamp(epoch - (epoch % seconds), tz=timezone.get_current_timezone())


def compute_gauges():
    """The measured gauges. Every number is a real count or sum."""
    holding = (
        EscrowPayment.Status.HELD,
        EscrowPayment.Status.DISPUTED,
        EscrowPayment.Status.RELEASE_PENDING,
        EscrowPayment.Status.REFUND_PENDING,
    )
    return {
        "users.customers": CustomerProfile.objects.count(),
        "users.artisans": ArtisanProfile.objects.count(),
        "artisans.active": ArtisanProfile.objects.filter(profile_status="ACTIVE").count(),
        "requests.open": ServiceRequest.objects.filter(status=ServiceRequest.Status.OPEN).count(),
        "conversations.total": Conversation.objects.count(),
        "offers.proposed": Offer.objects.filter(status=Offer.Status.PROPOSED).count(),
        "bookings.unpaid": Booking.objects.filter(status=Booking.Status.REQUESTED).count(),
        "bookings.in_progress": Booking.objects.filter(status=Booking.Status.IN_PROGRESS).count(),
        "bookings.completed": Booking.objects.filter(status=Booking.Status.COMPLETED).count(),
        "bookings.disputed": Booking.objects.filter(status=Booking.Status.DISPUTED).count(),
        "payments.failed": EscrowPayment.objects.filter(status=EscrowPayment.Status.FAILED).count(),
        "payments.pending": EscrowPayment.objects.filter(status=EscrowPayment.Status.PAYMENT_PENDING).count(),
        "escrow.held_kobo": int(
            EscrowPayment.objects.filter(status__in=holding).aggregate(t=Sum("total_amount_kobo"))["t"] or 0
        ),
        "escrow.artisan_payable_kobo": int(
            EscrowPayment.objects.filter(status__in=holding).aggregate(t=Sum("artisan_payout_kobo"))["t"] or 0
        ),
        "payouts.pending_kobo": int(
            EscrowRelease.objects.filter(status=EscrowRelease.Status.PENDING).aggregate(t=Sum("amount_kobo"))["t"] or 0
        ),
        "refunds.pending_kobo": int(
            SupportFinancialAction.objects.filter(status__in=["REQUESTED", "APPROVED", "PROCESSING"])
            .aggregate(t=Sum("amount_kobo"))["t"] or 0
        ),
        "support.open_cases": SupportCase.objects.filter(status__in=["OPEN", "UNDER_REVIEW"]).count(),
    }


def capture(*, user=None, now=None):
    now = now or timezone.now()
    seconds = interval_seconds()
    captured_for = _floor(now, seconds)
    started = time.perf_counter()
    gauges = compute_gauges()
    duration_ms = int((time.perf_counter() - started) * 1000)
    created = 0
    with transaction.atomic():
        for key, value in gauges.items():
            _, was_created = OperationalSnapshot.objects.get_or_create(
                metric_key=key,
                captured_for=captured_for,
                interval_seconds=seconds,
                defaults={"value": value, "calc_version": CALC_VERSION, "duration_ms": duration_ms},
            )
            created += int(was_created)
    cache.delete(STATUS_CACHE_KEY)
    return {
        "captured_for": captured_for.isoformat(),
        "interval_seconds": seconds,
        "metrics": len(gauges),
        "created": created,
        "duration_ms": duration_ms,
        "captured_at": timezone.now().isoformat(),
    }


def status():
    """Fast read used by the console: last capture, gaps, next run."""
    cached = cache.get(STATUS_CACHE_KEY)
    if cached is not None:
        return cached
    now = timezone.now()
    seconds = interval_seconds()
    last = OperationalSnapshot.objects.order_by("-captured_for").values_list("captured_for", flat=True).first()
    last_success_at = last
    expected_since_last = int((now - last).total_seconds() // seconds) if last else None
    recent = OperationalSnapshot.objects.filter(captured_for__gte=now - timezone.timedelta(hours=1))
    distinct_slots = recent.values("captured_for").distinct().count()
    expected_slots = max(1, min(60, int(3600 / seconds)))
    payload = {
        "interval_seconds": seconds,
        "interval_label": _label(seconds),
        "supported_intervals": [
            {"seconds": value, "label": _label(value)} for value in SNAPSHOT_INTERVALS
        ],
        "last_captured_for": last.isoformat() if last else None,
        "last_success_at": last_success_at.isoformat() if last_success_at else None,
        "seconds_since_last": round((now - last).total_seconds()) if last else None,
        "next_expected_at": (last + timezone.timedelta(seconds=seconds)).isoformat() if last else None,
        "behind_by_intervals": max(0, expected_since_last or 0),
        "slots_last_hour": distinct_slots,
        "expected_slots_last_hour": expected_slots,
        "missing_slots_last_hour": max(0, expected_slots - distinct_slots),
        "latest_duration_ms": OperationalSnapshot.objects.order_by("-captured_at").values_list(
            "duration_ms", flat=True).first(),
        "metrics_tracked": OperationalSnapshot.objects.values("metric_key").distinct().count(),
        "recent": [
            {"metric": row["metric_key"], "value": str(row["value"]), "captured_for": row["captured_for"].isoformat()}
            for row in OperationalSnapshot.objects.order_by("-captured_for")[:12].values(
                "metric_key", "value", "captured_for")
        ],
        "computed_at": now.isoformat(),
    }
    cache.set(STATUS_CACHE_KEY, payload, STATUS_TTL)
    return payload


def _label(seconds):
    if seconds < 3600:
        return f"{seconds // 60} minute{'s' if seconds != 60 else ''}"
    hours = seconds // 3600
    return f"{hours} hour{'s' if hours != 1 else ''}"


def history(metric_key, *, limit=120):
    rows = OperationalSnapshot.objects.filter(metric_key=metric_key).order_by("-captured_for")[:limit]
    return [
        {"captured_for": row.captured_for.isoformat(), "value": str(row.value), "interval_seconds": row.interval_seconds}
        for row in reversed(list(rows))
    ]


def capture_if_due():
    now = timezone.now()
    seconds = interval_seconds()
    last = OperationalSnapshot.objects.order_by("-captured_for").values_list("captured_for", flat=True).first()
    if last and (now - last).total_seconds() < seconds:
        return {"skipped": True, "reason": "not due", "next_at": (last + timezone.timedelta(seconds=seconds)).isoformat()}
    return capture(now=now)
