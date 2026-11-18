"""Incremental daily aggregates.

Dashboards read ``MetricAggregate`` rows for historical trend lines instead of
re-scanning raw tables. ``rebuild_daily`` recomputes a bounded window (idempotent:
one row per metric/day/dimension fingerprint) and is meant to run in the
background on a schedule.
"""
from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, DecimalField, Q, Sum
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone

from booking.models import Booking
from conversations.models import Offer
from home.models import ArtisanProfile, CustomerProfile
from manager.models import AnalyticsEvent, MetricAggregate
from payments.models import EscrowPayment
from services.models import ServiceRequest


def _fingerprint(dimensions):
    return hashlib.sha256(json.dumps(dimensions, sort_keys=True, default=str).encode()).hexdigest()


def _upsert(metric_key, bucket_start, value, dimensions=None):
    dimensions = dimensions or {}
    MetricAggregate.objects.update_or_create(
        metric_key=metric_key,
        bucket_kind="DAY",
        bucket_start=bucket_start,
        dimensions_hash=_fingerprint(dimensions),
        defaults={"value": value, "dimensions": dimensions, "calc_version": "v1"},
    )


def rebuild_daily(*, days=30, now=None):
    now = now or timezone.now()
    start = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
    tz = timezone.get_current_timezone()
    written = 0

    def day_rows(queryset, field="created_at", extra=None):
        rows = (
            queryset.filter(**{f"{field}__gte": start})
            .annotate(day=TruncDate(field, tzinfo=tz))
            .values("day")
            .annotate(**extra or {"n": Count("pk")})
        )
        return rows

    for row in day_rows(CustomerProfile.objects.all()):
        _upsert("customers.new", timezone.make_aware(timezone.datetime.combine(row["day"], timezone.datetime.min.time())),
                Decimal(row["n"]))
        written += 1
    for row in day_rows(ArtisanProfile.objects.all()):
        _upsert("artisans.new", timezone.make_aware(timezone.datetime.combine(row["day"], timezone.datetime.min.time())),
                Decimal(row["n"]))
        written += 1
    for row in day_rows(ServiceRequest.objects.all()):
        _upsert("requests.created", timezone.make_aware(timezone.datetime.combine(row["day"], timezone.datetime.min.time())),
                Decimal(row["n"]))
        written += 1
    for row in day_rows(Offer.objects.all()):
        _upsert("offers.created", timezone.make_aware(timezone.datetime.combine(row["day"], timezone.datetime.min.time())),
                Decimal(row["n"]))
        written += 1
    for row in day_rows(
        Booking.objects.all(),
        extra={
            "n": Count("pk"),
            "value": Coalesce(Sum("price_agreed"), Decimal("0"),
                              output_field=DecimalField(max_digits=20, decimal_places=2)),
            "completed": Count("pk", filter=Q(status=Booking.Status.COMPLETED)),
        },
    ):
        bucket = timezone.make_aware(timezone.datetime.combine(row["day"], timezone.datetime.min.time()))
        _upsert("bookings.created", bucket, Decimal(row["n"]))
        _upsert("bookings.value", bucket, Decimal(str(row["value"])))
        _upsert("bookings.completed", bucket, Decimal(row["completed"]))
        written += 3
    for row in day_rows(
        EscrowPayment.objects.filter(status__in=["HELD", "DISPUTED", "RELEASE_PENDING", "RELEASED",
                                                 "REFUND_PENDING", "REFUNDED"]),
        field="created_at",
        extra={
            "n": Count("pk"),
            "captured": Coalesce(Sum("total_amount_kobo"), 0),
            "commission": Coalesce(Sum("platform_commission_kobo"), 0),
            "fees": Coalesce(Sum("customer_fee_kobo"), 0),
        },
    ):
        bucket = timezone.make_aware(timezone.datetime.combine(row["day"], timezone.datetime.min.time()))
        _upsert("payments.captured", bucket, Decimal(row["n"]))
        _upsert("payments.captured_value", bucket, Decimal(int(row["captured"])))
        _upsert("revenue.commission", bucket, Decimal(int(row["commission"])))
        _upsert("revenue.service_fees", bucket, Decimal(int(row["fees"])))
        written += 4
    for row in (
        AnalyticsEvent.objects.filter(occurred_at__gte=start)
        .annotate(day=TruncDate("occurred_at", tzinfo=tz))
        .values("name", "day")
        .annotate(n=Count("pk"))
    ):
        bucket = timezone.make_aware(timezone.datetime.combine(row["day"], timezone.datetime.min.time()))
        _upsert("events", bucket, Decimal(row["n"]), {"name": row["name"]})
        written += 1
    return {"days": days, "written": written, "from": start.isoformat(), "to": now.isoformat()}


def series(metric_key, *, days=30):
    rows = MetricAggregate.objects.filter(metric_key=metric_key).order_by("-bucket_start")[:days]
    return [
        {"date": row.bucket_start.date().isoformat(), "value": float(row.value)}
        for row in reversed(list(rows))
    ]
