"""Analytical query layer for the manager console.

Design rules enforced here:

* **Bounded.** Every range is capped (``analytics.max_dashboard_range_days``) and
  every list is paginated or limited to a top-N.
* **Aggregated in SQL.** Counts and sums come from ``.values().annotate()``, never
  from iterating rows in Python.
* **Cached per permission scope.** The cache key contains the viewer's permission
  fingerprint, the filters and the range, so one manager can never be served a
  figure computed for a role with broader access.
* **Traceable.** Each figure carries its definition, and ratios carry the
  numerator and denominator they were computed from.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.core.cache import cache
from django.db.models import Avg, Count, DecimalField, F, Max, Min, Q, Sum
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone

from booking.models import Booking
from conversations.models import Conversation, Offer
from home.models import ArtisanProfile, CustomerProfile, CustomUser, Service, ServiceCategory
from payments.models import EscrowPayment, EscrowRelease, PaymentLedgerLine
from reviews.models import Review
from risk.models import CustomerRiskProfile
from services.models import ArtisanVerification, ServiceRequest
from support.models import SupportCase, SupportFinancialAction

from manager.models import AnalyticsEvent, Campaign, CampaignDelivery, OperationalSnapshot
from manager.services import events as event_service

CACHE_PREFIX = "manager:metrics:v1"
CACHE_TTL = 45

PAID_STATUSES = (
    EscrowPayment.Status.HELD,
    EscrowPayment.Status.DISPUTED,
    EscrowPayment.Status.RELEASE_PENDING,
    EscrowPayment.Status.RELEASED,
    EscrowPayment.Status.REFUND_PENDING,
    EscrowPayment.Status.REFUNDED,
)
HOLDING_STATUSES = (
    EscrowPayment.Status.HELD,
    EscrowPayment.Status.DISPUTED,
    EscrowPayment.Status.RELEASE_PENDING,
    EscrowPayment.Status.REFUND_PENDING,
)
OPEN_BOOKING_STATUSES = (
    Booking.Status.REQUESTED,
    Booking.Status.ACCEPTED,
    Booking.Status.IN_PROGRESS,
    Booking.Status.COMPLETION_REQUESTED,
    Booking.Status.DISPUTED,
)
OPEN_CASE_STATUSES = (SupportCase.Status.OPEN, SupportCase.Status.UNDER_REVIEW)
PENDING_REFUND_STATUSES = (
    SupportFinancialAction.Status.REQUESTED,
    SupportFinancialAction.Status.APPROVED,
    SupportFinancialAction.Status.PROCESSING,
    SupportFinancialAction.Status.PROCESSING_UNKNOWN,
)

PERIODS = {
    "today": ("Today", 0),
    "yesterday": ("Yesterday", 1),
    "last_7_days": ("Last 7 days", 7),
    "last_30_days": ("Last 30 days", 30),
    "last_90_days": ("Last 90 days", 90),
    "last_12_months": ("Last 12 months", 365),
    "this_month": ("This month", None),
    "last_month": ("Last month", None),
    "this_quarter": ("This quarter", None),
    "last_quarter": ("Last quarter", None),
    "this_year": ("This year", None),
    "last_year": ("Last year", None),
    "custom": ("Custom range", None),
}


class FilterError(ValueError):
    pass


@dataclass(frozen=True)
class Range:
    start: datetime
    end: datetime
    label: str
    key: str

    @property
    def previous(self):
        span = self.end - self.start
        return Range(self.start - span, self.start, f"{self.label} (previous)", "previous")

    @property
    def days(self):
        return max(1, round((self.end - self.start).total_seconds() / 86400))


def _aware(day: date):
    return timezone.make_aware(datetime.combine(day, time.min), timezone.get_current_timezone())


def _parse_date(value, field):
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise FilterError(f"'{field}' must be a date in YYYY-MM-DD form.")


def resolve_range(params, *, now=None):
    """Turn any supported period keyword into a concrete half-open range."""
    from manager.services.config_engine import current

    now = now or timezone.now()
    local = timezone.localtime(now)
    today = local.date()
    key = str(params.get("period") or params.get("range") or "last_30_days").strip().lower().replace("-", "_")
    if key in {"7d", "week"}:
        key = "last_7_days"
    if key in {"30d", "month"}:
        key = "last_30_days"
    if key in {"90d"}:
        key = "last_90_days"

    if key == "today":
        start, end, label = _aware(today), _aware(today + timedelta(days=1)), "Today"
    elif key == "yesterday":
        start, end, label = _aware(today - timedelta(days=1)), _aware(today), "Yesterday"
    elif key in PERIODS and isinstance(PERIODS[key][1], int) and PERIODS[key][1]:
        days = PERIODS[key][1]
        start, end, label = now - timedelta(days=days), now, PERIODS[key][0]
    elif key == "this_week":
        start = _aware(today - timedelta(days=today.weekday()))
        start, end, label = start, now, "This week"
    elif key == "last_week":
        monday = today - timedelta(days=today.weekday())
        start, end, label = _aware(monday - timedelta(days=7)), _aware(monday), "Last week"
    elif key == "this_month":
        start = _aware(today.replace(day=1))
        start, end, label = start, now, "This month"
    elif key == "last_month":
        first = today.replace(day=1)
        previous_end = first
        start = _aware((previous_end - timedelta(days=1)).replace(day=1))
        start, end, label = start, _aware(previous_end), "Last month"
    elif key == "this_quarter":
        quarter_month = ((today.month - 1) // 3) * 3 + 1
        start = _aware(today.replace(month=quarter_month, day=1))
        start, end, label = start, now, "This quarter"
    elif key == "last_quarter":
        quarter_month = ((today.month - 1) // 3) * 3 + 1
        current_start = today.replace(month=quarter_month, day=1)
        previous_end = current_start
        previous_month = quarter_month - 3
        year = today.year
        if previous_month <= 0:
            previous_month += 12
            year -= 1
        start = _aware(date(year, previous_month, 1))
        start, end, label = start, _aware(previous_end), "Last quarter"
    elif key == "this_year":
        start, end, label = _aware(date(today.year, 1, 1)), now, "This year"
    elif key == "last_year":
        start, end, label = _aware(date(today.year - 1, 1, 1)), _aware(date(today.year, 1, 1)), "Last year"
    elif key == "custom":
        if not params.get("start") or not params.get("end"):
            raise FilterError("A custom range needs both 'start' and 'end'.")
        start_day = _parse_date(params.get("start"), "start")
        end_day = _parse_date(params.get("end"), "end")
        if end_day < start_day:
            raise FilterError("'end' cannot be before 'start'.")
        start, end, label = _aware(start_day), _aware(end_day + timedelta(days=1)), "Custom range"
        key = "custom"
    else:
        start, end, label = now - timedelta(days=30), now, "Last 30 days"
        key = "last_30_days"

    limit_days = int(current("analytics.max_dashboard_range_days") or 366)
    if (end - start).days > limit_days:
        raise FilterError(f"Ask for at most {limit_days} days at a time.")
    if end <= start:
        end = start + timedelta(days=1)
    return Range(start=start, end=end, label=label, key=key)


def viewer_fingerprint(user):
    from manager.services.auth import effective_permissions

    codes = sorted(effective_permissions(user))
    return hashlib.sha256("|".join(codes).encode()).hexdigest()[:16]


def _cache_safe(parts):
    """Floor timestamps to the minute so a rolling range keeps a stable cache key."""
    out = []
    for part in parts:
        if isinstance(part, datetime):
            out.append(part.replace(second=0, microsecond=0).isoformat())
        else:
            out.append(part)
    return out


def cached(key_parts, builder, ttl=CACHE_TTL):
    raw = f"{CACHE_PREFIX}:{json.dumps(_cache_safe(key_parts), sort_keys=True, default=str)[:400]}"
    key = cache.make_key("mgr:" + hashlib.sha256(raw.encode()).hexdigest())
    hit = cache.get(key)
    if hit is not None:
        return hit, True
    value = builder()
    cache.set(key, value, ttl)
    return value, False


# --------------------------------------------------------------------------- #
# Small numeric helpers
# --------------------------------------------------------------------------- #
def ratio(numerator, denominator, *, dp=1):
    if not denominator:
        return 0.0
    return round(float(numerator) * 100.0 / float(denominator), dp)


def growth(current_value, previous_value, *, dp=1):
    """Percentage change, or ``None`` when there is no baseline to compare against.

    A change that rounds to minus zero is reported as zero: "-0.0%" reads like a
    defect to whoever is looking at the page.
    """
    current_value = float(current_value or 0)
    previous_value = float(previous_value or 0)
    if previous_value == 0:
        # Nothing to compare against. "0.0% vs previous period" for a metric that
        # did not exist last period reads like a real, flat result.
        return None
    raw = (current_value - previous_value) * 100.0 / previous_value
    if raw == 0:
        return 0.0
    rounded = round(raw, dp)
    # A change smaller than the display precision is still a change. Keep the
    # precision here and let the presentation layer say "<0.1%"; collapsing it to
    # 0.0 would tell the reader nothing moved when thousands of naira did.
    return rounded if rounded else round(raw, 4)


def money(kobo):
    return f"₦{Decimal(int(kobo or 0)) / 100:,.2f}"


def metric(label, value, *, previous=None, definition="", unit="number", numerator=None, denominator=None):
    payload = {
        "label": label,
        "value": value,
        "unit": unit,
        "definition": definition,
    }
    if previous is not None:
        payload["previous"] = previous
        payload["change_percent"] = growth(value, previous)
    if numerator is not None:
        payload["numerator"] = numerator
    if denominator is not None:
        payload["denominator"] = denominator
    return payload


def _sum(qs, field):
    return int(qs.aggregate(total=Coalesce(Sum(field), 0))["total"] or 0)


def _count(qs):
    return int(qs.count())


def _distinct(qs, field):
    return int(qs.values(field).distinct().count())


def _median(qs, field):
    values = list(qs.order_by(field).values_list(field, flat=True))
    if not values:
        return 0
    values.sort()
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2


def _percentile(qs, field, fraction):
    values = sorted(v for v in qs.values_list(field, flat=True) if v is not None)
    if not values:
        return None
    index = min(len(values) - 1, max(0, int(round(fraction * (len(values) - 1)))))
    return values[index]


# --------------------------------------------------------------------------- #
# Overview
# --------------------------------------------------------------------------- #
def overview(user, params):
    rng = resolve_range(params)
    scope = viewer_fingerprint(user)

    def build():
        prev = rng.previous
        customers = CustomerProfile.objects.all()
        artisans = ArtisanProfile.objects.all()
        verified_artisan_ids = ArtisanVerification.objects.filter(
            verification_status="VERIFIED"
        ).values("artisan_id")
        artisans_verified = artisans.filter(id__in=verified_artisan_ids)
        requests = ServiceRequest.objects.all()
        bookings = Booking.objects.all()
        payments = EscrowPayment.objects.all()
        paid = payments.filter(status__in=PAID_STATUSES)

        in_range = {
            "customers": customers.filter(created_at__gte=rng.start, created_at__lt=rng.end).count(),
            "customers_prev": customers.filter(created_at__gte=prev.start, created_at__lt=prev.end).count(),
            "artisans": artisans.filter(created_at__gte=rng.start, created_at__lt=rng.end).count(),
            "artisans_prev": artisans.filter(created_at__gte=prev.start, created_at__lt=prev.end).count(),
            "requests": requests.filter(created_at__gte=rng.start, created_at__lt=rng.end).count(),
            "requests_prev": requests.filter(created_at__gte=prev.start, created_at__lt=prev.end).count(),
            "bookings": bookings.filter(created_at__gte=rng.start, created_at__lt=rng.end).count(),
            "bookings_prev": bookings.filter(created_at__gte=prev.start, created_at__lt=prev.end).count(),
            "offers": Offer.objects.filter(created_at__gte=rng.start, created_at__lt=rng.end).count(),
            "offers_prev": Offer.objects.filter(created_at__gte=prev.start, created_at__lt=prev.end).count(),
        }
        offers_in = Offer.objects.filter(created_at__gte=rng.start, created_at__lt=rng.end)
        offers_prev = Offer.objects.filter(created_at__gte=prev.start, created_at__lt=prev.end)
        paid_in = paid.filter(Q(held_at__gte=rng.start, held_at__lt=rng.end) | Q(verified_at__gte=rng.start, verified_at__lt=rng.end))
        paid_prev = paid.filter(Q(held_at__gte=prev.start, held_at__lt=prev.end) | Q(verified_at__gte=prev.start, verified_at__lt=prev.end))
        bookings_in = bookings.filter(created_at__gte=rng.start, created_at__lt=rng.end)
        bookings_prev = bookings.filter(created_at__gte=prev.start, created_at__lt=prev.end)
        refunds_in = SupportFinancialAction.objects.filter(
            status=SupportFinancialAction.Status.SUCCEEDED, completed_at__gte=rng.start, completed_at__lt=rng.end
        )
        refunds_prev = SupportFinancialAction.objects.filter(
            status=SupportFinancialAction.Status.SUCCEEDED, completed_at__gte=prev.start, completed_at__lt=prev.end
        )

        accepted_in = offers_in.filter(status=Offer.Status.ACCEPTED).count()
        accepted_prev = offers_prev.filter(status=Offer.Status.ACCEPTED).count()
        paid_bookings_in = bookings_in.filter(status__in=[Booking.Status.ACCEPTED, Booking.Status.IN_PROGRESS,
                                                          Booking.Status.COMPLETION_REQUESTED, Booking.Status.COMPLETED,
                                                          Booking.Status.DISPUTED]).count()

        return {
            "range": {"key": rng.key, "label": rng.label, "start": rng.start.isoformat(), "end": rng.end.isoformat(),
                      "previous_start": prev.start.isoformat(), "previous_end": prev.end.isoformat(), "days": rng.days},
            "generated_at": timezone.now().isoformat(),
            "totals": {
                "customers": _count(customers),
                "artisans": _count(artisans),
                "verified_artisans": _count(artisans_verified),
                "active_artisans": _count(artisans.filter(profile_status="ACTIVE")),
            },
            "period": {
                "new_customers": metric("New customers", in_range["customers"], previous=in_range["customers_prev"],
                                        definition="Customer profiles created inside the range."),
                "new_artisans": metric("New artisans", in_range["artisans"], previous=in_range["artisans_prev"],
                                       definition="Artisan profiles created inside the range."),
                "requests": metric("Service requests", in_range["requests"], previous=in_range["requests_prev"],
                                   definition="Service requests created inside the range."),
                "offers_sent": metric("Offers sent", in_range["offers"], previous=in_range["offers_prev"],
                                      definition="Offers created inside the range."),
                "offers_accepted": metric("Offers accepted", accepted_in, previous=accepted_prev,
                                          definition="Offers whose current status is ACCEPTED and were created inside the range."),
                "bookings": metric("Bookings created", in_range["bookings"], previous=in_range["bookings_prev"],
                                   definition="Bookings created inside the range."),
                "paid_bookings": metric("Paid bookings", paid_bookings_in,
                                        definition="Bookings created in range that progressed past acceptance."),
                "gross_booking_value_kobo": metric("Gross booking value", int(bookings_in.aggregate(
                    t=Coalesce(Sum("price_agreed"), Decimal("0"), output_field=DecimalField(max_digits=20, decimal_places=2)))["t"] or 0),
                    unit="kobo", definition="Sum of agreed price over bookings created in range."),
                "paid_value_kobo": metric("Paid booking value", _sum(paid_in, "total_amount_kobo"),
                                          previous=_sum(paid_prev, "total_amount_kobo"), unit="kobo",
                                          definition="Sum of captured customer payments (escrow) inside the range."),
                "commission_kobo": metric("Commissions", _sum(paid_in, "platform_commission_kobo"), unit="kobo",
                                          definition="Platform commission retained from captured payments."),
                "fees_kobo": metric("Service fees", _sum(paid_in, "customer_fee_kobo"), unit="kobo",
                                    definition="Customer service fees charged on captured payments."),
                "refunds_kobo": metric("Refunds", _sum(refunds_in, "amount_kobo"),
                                       previous=_sum(refunds_prev, "amount_kobo"), unit="kobo",
                                       definition="Refunds that completed at the provider inside the range."),
                "average_transaction_kobo": metric("Average transaction", (
                    _sum(paid_in, "total_amount_kobo") // paid_in.count()) if paid_in.count() else 0,
                    unit="kobo", definition="Captured payment value divided by captured payment count."),
            },
            "bookings_by_status": _status_counts(bookings, Booking.Status),
            "escrow": {
                "held_kobo": _sum(payments.filter(status__in=HOLDING_STATUSES), "total_amount_kobo"),
                "artisan_payable_kobo": _sum(payments.filter(status__in=HOLDING_STATUSES), "artisan_payout_kobo"),
                "pending_payouts_kobo": _sum(EscrowRelease.objects.filter(status=EscrowRelease.Status.PENDING), "amount_kobo"),
                "released_kobo": _sum(EscrowRelease.objects.filter(status=EscrowRelease.Status.SUCCESSFUL), "amount_kobo"),
                "refund_liability_kobo": _sum(
                    SupportFinancialAction.objects.filter(status__in=PENDING_REFUND_STATUSES), "amount_kobo"),
            },
            "ratios": {
                "booking_conversion": _ratio_metric(
                    "Booking conversion", accepted_in, in_range["offers"],
                    definition="Accepted offers in range divided by offers created in range."),
                "request_to_booking": _ratio_metric(
                    "Request to booking", bookings_in.exclude(status=Booking.Status.CANCELLED).values("request_id").distinct().count(),
                    in_range["requests"], definition="Requests in range that produced a booking, divided by requests in range."),
                "completion_rate": _ratio_metric(
                    "Completion rate", bookings_in.filter(status=Booking.Status.COMPLETED).count(), in_range["bookings"],
                    definition="Bookings created in range that reached COMPLETED, divided by bookings created in range."),
                "cancellation_rate": _ratio_metric(
                    "Cancellation rate", bookings_in.filter(status__in=[Booking.Status.CANCELLED, Booking.Status.REJECTED]).count(),
                    in_range["bookings"], definition="Bookings created in range that were cancelled or rejected."),
                "customer_retention": _ratio_metric(
                    "Customer retention", _repeat_customers(bookings_in), in_range["bookings"] and
                    bookings_in.values("customer_id").distinct().count(),
                    definition="Customers with more than one booking in range, divided by customers with any booking in range."),
            },
            "attention": _attention_panel(),
            "data_freshness": _freshness(),
        }

    payload, hit = cached(["overview", scope, rng.key, rng.start, rng.end], build)
    payload = dict(payload)
    payload["served_from_cache"] = hit
    return payload


def _repeat_customers(bookings):
    rows = bookings.values("customer_id").annotate(n=Count("pk")).filter(n__gt=1)
    return rows.count()


def _ratio_metric(label, numerator, denominator, *, definition):
    return {
        "label": label,
        "value": ratio(numerator, denominator),
        "unit": "percent",
        "numerator": int(numerator or 0),
        "denominator": int(denominator or 0),
        "definition": definition,
    }


def _status_counts(queryset, choices):
    pairs = list(getattr(choices, "choices", choices))
    labels = {value: label for value, label in pairs}
    counts = {value: 0 for value, _label in pairs}
    for row in queryset.values("status").annotate(n=Count("pk")):
        counts[row["status"]] = row["n"]
    total = sum(counts.values()) or 1
    return [
        {"status": status, "label": labels.get(status, status.replace("_", " ").title()),
         "count": count, "percent": round(count * 100 / total, 1)}
        for status, count in counts.items()
    ]


def _attention_panel():
    now = timezone.now()
    soon = now + timedelta(hours=24)
    unpaid = Booking.objects.filter(status=Booking.Status.REQUESTED).count()
    failed_payments = EscrowPayment.objects.filter(status=EscrowPayment.Status.FAILED).count()
    unanswered = ServiceRequest.objects.filter(status=ServiceRequest.Status.OPEN,
                                               conversation__isnull=True).count()
    expiring = Offer.objects.filter(status=Offer.Status.PROPOSED, expires_at__lte=soon, expires_at__gt=now).count()
    pending_payouts = EscrowRelease.objects.filter(status=EscrowRelease.Status.PENDING).count()
    refund_requests = SupportFinancialAction.objects.filter(status__in=PENDING_REFUND_STATUSES).count()
    disputes = SupportCase.objects.filter(status__in=OPEN_CASE_STATUSES, case_type=SupportCase.CaseType.DISPUTE).count()
    tickets = SupportCase.objects.filter(status__in=OPEN_CASE_STATUSES).count()
    verification_backlog = ArtisanVerification.objects.filter(verification_status="PENDING").count()
    failed_notifications = 0
    try:
        from notifications.models import InAppNotification  # noqa: F401
        from core.models import OutboxEvent

        failed_notifications = OutboxEvent.objects.filter(dead_lettered_at__isnull=False).count()
    except Exception:  # pragma: no cover
        failed_notifications = 0
    from manager.models import BackgroundJob

    failed_jobs = BackgroundJob.objects.filter(status=BackgroundJob.Status.FAILED).count()
    items = [
        ("unpaid_bookings", "Bookings accepted but not paid", unpaid, "/manager/financial/#unpaid"),
        ("failed_payments", "Failed payments", failed_payments, "/manager/financial/"),
        ("unanswered_requests", "Requests with no artisan response", unanswered, "/manager/funnel/"),
        ("offers_expiring", "Offers expiring within 24 hours", expiring, "/manager/funnel/"),
        ("pending_payouts", "Payouts awaiting release", pending_payouts, "/manager/financial/"),
        ("refund_requests", "Refund requests in flight", refund_requests, "/manager/financial/"),
        ("open_disputes", "Open disputes", disputes, "/manager/support/"),
        ("support_tickets", "Open support cases", tickets, "/manager/support/"),
        ("verification_backlog", "Artisans awaiting verification", verification_backlog, "/manager/artisans/"),
        ("failed_notifications", "Dead-lettered notifications", failed_notifications, "/manager/health/"),
        ("failed_jobs", "Failed background jobs", failed_jobs, "/manager/health/"),
    ]
    return [{"key": key, "label": label, "count": count, "link": link} for key, label, count, link in items]


def _freshness():
    last_event = AnalyticsEvent.objects.order_by("-occurred_at").values_list("occurred_at", flat=True).first()
    last_snapshot = OperationalSnapshot.objects.order_by("-captured_for").values_list("captured_for", flat=True).first()
    return {
        "events_last_recorded_at": last_event.isoformat() if last_event else None,
        "snapshot_last_captured_at": last_snapshot.isoformat() if last_snapshot else None,
        "computed_at": timezone.now().isoformat(),
        "cache_ttl_seconds": CACHE_TTL,
    }


# --------------------------------------------------------------------------- #
# Financial intelligence
# --------------------------------------------------------------------------- #
def financial(user, params):
    rng = resolve_range(params)
    scope = viewer_fingerprint(user)

    def build():
        prev = rng.previous
        payments = EscrowPayment.objects.all()
        paid_in = payments.filter(status__in=PAID_STATUSES).filter(
            Q(held_at__gte=rng.start, held_at__lt=rng.end) | Q(verified_at__gte=rng.start, verified_at__lt=rng.end))
        paid_prev = payments.filter(status__in=PAID_STATUSES).filter(
            Q(held_at__gte=prev.start, held_at__lt=prev.end) | Q(verified_at__gte=prev.start, verified_at__lt=prev.end))
        pending = payments.filter(status=EscrowPayment.Status.PAYMENT_PENDING)
        failed = payments.filter(status=EscrowPayment.Status.FAILED)
        failed_in = failed.filter(created_at__gte=rng.start, created_at__lt=rng.end)
        refunds_in = SupportFinancialAction.objects.filter(
            status=SupportFinancialAction.Status.SUCCEEDED, completed_at__gte=rng.start, completed_at__lt=rng.end)
        releases = EscrowRelease.objects.filter(status=EscrowRelease.Status.SUCCESSFUL)
        releases_in = releases.filter(completed_at__gte=rng.start, completed_at__lt=rng.end)
        commission = _sum(paid_in, "platform_commission_kobo")
        fees = _sum(paid_in, "customer_fee_kobo")
        promotion_revenue = _promotion_revenue(rng)
        processing_cost = _processing_cost(rng)
        net_revenue = commission + fees + promotion_revenue - processing_cost - _sum(refunds_in, "amount_kobo")
        gbv = int(Booking.objects.filter(created_at__gte=rng.start, created_at__lt=rng.end)
                  .aggregate(t=Coalesce(Sum("price_agreed"), Decimal("0"),
                                        output_field=DecimalField(max_digits=20, decimal_places=2)))["t"] or 0) * 100
        paid_count = paid_in.count()
        median = _median(paid_in, "total_amount_kobo")
        customer_ids = set(paid_in.values_list("customer_id", flat=True))
        artisan_ids = set(paid_in.values_list("artisan_id", flat=True))
        captured = _sum(paid_in, "total_amount_kobo")
        return {
            "range": {"key": rng.key, "label": rng.label, "start": rng.start.isoformat(), "end": rng.end.isoformat(),
                      "days": rng.days, "previous_start": prev.start.isoformat(), "previous_end": prev.end.isoformat()},
            "generated_at": timezone.now().isoformat(),
            "kpis": {
                "gross_booking_value_kobo": metric("Gross booking value (GMV)", gbv, unit="kobo",
                                                   definition="Agreed price summed over bookings created in range. Not revenue."),
                "successful_payment_kobo": metric("Captured payments", captured, previous=_sum(paid_prev, "total_amount_kobo"),
                                                  unit="kobo", definition="Customer payments confirmed by the provider in range."),
                "pending_payment_kobo": metric("Pending payment value", _sum(pending, "total_amount_kobo"), unit="kobo",
                                               definition="Payments created but not yet confirmed. Never counted as revenue."),
                "failed_payment_kobo": metric("Failed payment value", _sum(failed_in, "total_amount_kobo"), unit="kobo",
                                              definition="Payments the provider reported as failed in range."),
                "refund_kobo": metric("Refund value", _sum(refunds_in, "amount_kobo"), unit="kobo",
                                      definition="Refunds completed at the provider in range."),
                "chargeback_kobo": metric("Chargebacks", 0, unit="kobo",
                                          definition="Chargebacks recorded by the provider. Zero until the provider reports one."),
                "escrow_deposits_kobo": metric("Escrow deposits", _sum(paid_in, "total_amount_kobo"), unit="kobo",
                                               definition="Money that entered escrow in range."),
                "escrow_releases_kobo": metric("Escrow releases", _sum(releases_in, "amount_kobo"), unit="kobo",
                                               definition="Payouts released from escrow in range."),
                "escrow_liability_kobo": metric("Current escrow liability", _sum(
                    payments.filter(status__in=HOLDING_STATUSES), "total_amount_kobo"), unit="kobo",
                    definition="Customer money the platform currently holds. A liability, never revenue."),
                "artisan_payout_value_kobo": metric("Artisan payouts", _sum(releases_in, "amount_kobo"), unit="kobo",
                                                    definition="Completed payouts to artisans in range."),
                "pending_payout_liability_kobo": metric("Pending payout liability", _sum(
                    EscrowRelease.objects.filter(status=EscrowRelease.Status.PENDING), "amount_kobo"), unit="kobo",
                    definition="Money owed to artisans whose release has not completed."),
                "commission_revenue_kobo": metric("Commission revenue", commission, unit="kobo",
                                                  definition="Platform commission on captured payments in range."),
                "service_fee_revenue_kobo": metric("Service fee revenue", fees, unit="kobo",
                                                   definition="Customer fees on captured payments in range."),
                "promotion_revenue_kobo": metric("Promotion revenue", promotion_revenue, unit="kobo",
                                                 definition="Verified promotion payments received in range."),
                "advertising_revenue_kobo": metric("Advertising revenue", 0, unit="kobo",
                                                   definition="Reserved for standalone advertising packages; none sold yet."),
                "processing_cost_kobo": metric("Payment processing cost", processing_cost, unit="kobo",
                                               definition="Provider charges recorded in range (ledger account PAYMENT_PROCESSING)."),
                "net_revenue_kobo": metric("Net platform revenue", net_revenue, unit="kobo",
                                           definition="Commission + service fees + promotion revenue - refunds - processing cost."),
                "average_transaction_kobo": metric("Average transaction", captured // paid_count if paid_count else 0,
                                                   unit="kobo", definition="Captured value divided by captured count."),
                "median_transaction_kobo": metric("Median transaction", int(median), unit="kobo",
                                                  definition="Middle captured payment value."),
                "revenue_per_customer_kobo": metric("Revenue per paying customer", captured // len(customer_ids) if customer_ids else 0,
                                                    unit="kobo", definition="Captured value divided by distinct paying customers."),
                "revenue_per_artisan_kobo": metric("Revenue per paid artisan", captured // len(artisan_ids) if artisan_ids else 0,
                                                   unit="kobo", definition="Captured value divided by distinct paid artisans."),
            },
            "definitions": [
                ("Gross booking value", "Agreed price of every booking created in range, whether or not it was paid."),
                ("Captured payments", "Provider-confirmed escrow holds. The only figure that counts as money collected."),
                ("Recognised platform revenue", "Commission and service fees on captured payments, minus refunds."),
                ("Escrow liability", "Customer money currently held. Owed either to an artisan or back to a customer."),
                ("Pending payouts", "Released-but-unpaid amounts owed to artisans."),
            ],
            "payment_status_breakdown": _status_counts(payments, EscrowPayment.Status),
            "reconciliation": reconciliation_summary(rng),
        }

    payload, hit = cached(["financial", scope, rng.key, rng.start, rng.end], build)
    payload = dict(payload)
    payload["served_from_cache"] = hit
    return payload


def _promotion_revenue(rng):
    try:
        from promotions.models import PromotionPayment

        return _sum(PromotionPayment.objects.filter(status="SUCCESSFUL", verified_at__gte=rng.start,
                                                    verified_at__lt=rng.end), "amount_kobo")
    except Exception:  # pragma: no cover - promotions app always installed
        return 0


def _processing_cost(rng):
    return _sum(PaymentLedgerLine.objects.filter(account="PAYMENT_PROCESSING", currency="NGN",
                                                 created_at__gte=rng.start, created_at__lt=rng.end), "amount_kobo")


def reconciliation_summary(rng):
    """Provider-vs-internal checks. Real comparisons only, computed in SQL."""
    payments = EscrowPayment.objects.filter(created_at__gte=rng.start, created_at__lt=rng.end)
    missing_provider_ref = payments.filter(status__in=PAID_STATUSES, provider_transaction_id__isnull=True).count()
    amount_mismatch = payments.filter(status__in=PAID_STATUSES).exclude(
        total_amount_kobo=F("service_amount_kobo") + F("customer_fee_kobo")
    ).count()
    payout_mismatch = payments.filter(status__in=PAID_STATUSES).exclude(
        service_amount_kobo=F("platform_commission_kobo") + F("artisan_payout_kobo")
    ).count()
    held_without_ledger = 0
    try:
        held = payments.filter(status__in=HOLDING_STATUSES).values_list("id", flat=True)[:500]
        ledger_ids = set(PaymentLedgerLine.objects.filter(
            entry__event_type="ESCROW_HELD", entry__payment_id__in=list(held)).values_list("entry__payment_id", flat=True))
        held_without_ledger = max(0, len(list(held)) - len(ledger_ids))
    except Exception:  # pragma: no cover
        held_without_ledger = 0
    refunds_without_payment = SupportFinancialAction.objects.filter(
        action_type__contains="REFUND", payment__isnull=True).exclude(status__in=PENDING_REFUND_STATUSES).count()
    stale_pending = EscrowPayment.objects.filter(
        status=EscrowPayment.Status.PAYMENT_PENDING,
        created_at__lt=timezone.now() - timedelta(hours=24),
    ).count()
    issues = [
        {"key": "missing_provider_reference", "label": "Paid records without a provider reference", "count": missing_provider_ref,
         "severity": "HIGH"},
        {"key": "amount_mismatch", "label": "Payments whose parts do not sum to the total", "count": amount_mismatch,
         "severity": "HIGH"},
        {"key": "payout_mismatch", "label": "Payments where commission + payout do not equal the agreed price", "count": payout_mismatch,
         "severity": "HIGH"},
        {"key": "held_without_ledger", "label": "Escrow holds with no ledger entry", "count": held_without_ledger,
         "severity": "HIGH"},
        {"key": "refunds_without_payment", "label": "Completed refunds with no payment link", "count": refunds_without_payment,
         "severity": "MEDIUM"},
        {"key": "stale_pending", "label": "Payments pending for more than 24 hours", "count": stale_pending,
         "severity": "MEDIUM"},
    ]
    return {
        "checked_at": timezone.now().isoformat(),
        "range_start": rng.start.isoformat(),
        "range_end": rng.end.isoformat(),
        "issues": issues,
        "clean": all(item["count"] == 0 for item in issues),
        "note": "Amounts come from internal records. Provider settlement files are not available in this deployment, "
                "so provider-side reconciliation is limited to stored provider references and statuses.",
    }


# --------------------------------------------------------------------------- #
# Business intelligence
# --------------------------------------------------------------------------- #
def business(user, params):
    rng = resolve_range(params)
    scope = viewer_fingerprint(user)

    def build():
        prev = rng.previous
        bookings = Booking.objects.filter(created_at__gte=rng.start, created_at__lt=rng.end)
        bookings_prev = Booking.objects.filter(created_at__gte=prev.start, created_at__lt=prev.end)
        customers = CustomerProfile.objects.filter(created_at__gte=rng.start, created_at__lt=rng.end)
        customers_prev = CustomerProfile.objects.filter(created_at__gte=prev.start, created_at__lt=prev.end)
        artisans = ArtisanProfile.objects.filter(created_at__gte=rng.start, created_at__lt=rng.end)
        requests = ServiceRequest.objects.filter(created_at__gte=rng.start, created_at__lt=rng.end)
        paid = EscrowPayment.objects.filter(status__in=PAID_STATUSES).filter(
            Q(held_at__gte=rng.start, held_at__lt=rng.end) | Q(verified_at__gte=rng.start, verified_at__lt=rng.end))
        active_customers = bookings.values("customer_id").distinct().count()
        active_artisans = bookings.values("artisan_id").distinct().count()
        repeat = bookings.values("customer_id").annotate(n=Count("pk")).filter(n__gt=1).count()
        revenue = _sum(paid, "total_amount_kobo")
        customers_with_bookings = bookings.values("customer_id").distinct().count()
        repeat_ratio = ratio(_repeat_customers(bookings), customers_with_bookings)
        ltv = revenue // active_customers if active_customers else 0
        booking_counts = bookings.count()
        return {
            "range": {"key": rng.key, "label": rng.label, "start": rng.start.isoformat(), "end": rng.end.isoformat(),
                      "days": rng.days},
            "generated_at": timezone.now().isoformat(),
            "growth": {
                "customers": metric("Customer growth", customers.count(), previous=customers_prev.count(),
                                    definition="Customer profiles created in range."),
                "artisans": metric("Artisan growth", artisans.count(), previous=ArtisanProfile.objects.filter(
                    created_at__gte=prev.start, created_at__lt=prev.end).count(),
                    definition="Artisan profiles created in range."),
                "requests": metric("Request growth", requests.count(), previous=ServiceRequest.objects.filter(
                    created_at__gte=prev.start, created_at__lt=prev.end).count(),
                    definition="Service requests created in range."),
                "bookings": metric("Booking growth", booking_counts, previous=bookings_prev.count(),
                                   definition="Bookings created in range."),
                "revenue": metric("Revenue growth", revenue, previous=_sum(
                    EscrowPayment.objects.filter(status__in=PAID_STATUSES).filter(
                        Q(held_at__gte=prev.start, held_at__lt=prev.end) | Q(verified_at__gte=prev.start, verified_at__lt=prev.end)),
                    "total_amount_kobo"), unit="kobo", definition="Captured payments in range."),
            },
            "quality": {
                "repeat_booking_rate": {**_ratio_metric("Repeat booking rate", repeat, customers_with_bookings,
                                                        definition="Customers with 2+ bookings in range, over customers with any booking."),
                                        "repeat_customers": repeat},
                "average_booking_value_kobo": metric(
                    "Average booking value", int((bookings.aggregate(t=Coalesce(Sum("price_agreed"), Decimal("0"),
                                                                          output_field=DecimalField(max_digits=20, decimal_places=2)))["t"] or 0) * 100 / booking_counts) if booking_counts else 0,
                    unit="kobo", definition="Agreed value divided by bookings created in range."),
                "revenue_per_active_customer_kobo": metric("Revenue per active customer", ltv, unit="kobo",
                                                           definition="Captured payments divided by customers with a booking."),
                "revenue_per_active_artisan_kobo": metric(
                    "Revenue per active artisan", revenue // active_artisans if active_artisans else 0, unit="kobo",
                    definition="Captured payments divided by artisans with a booking."),
                "customer_lifetime_value_kobo": metric(
                    "Estimated customer lifetime value", ltv, unit="kobo",
                    definition="Captured payment value in range divided by distinct paying-period customers. "
                               "A period estimate, not a predictive model."),
                "customer_acquisition_cost_kobo": metric(
                    "Customer acquisition cost", 0, unit="kobo",
                    definition="Requires advertising spend data. No spend records exist, so this is reported as zero."),
            },
            "supply_demand": _supply_demand(),
            "retention": retention_cohorts(rng),
        }

    payload, hit = cached(["business", scope, rng.key, rng.start, rng.end], build)
    payload = dict(payload)
    payload["served_from_cache"] = hit
    return payload


def _supply_demand():
    categories = []
    rows = (
        ServiceCategory.objects.annotate(
            request_count=Count("service_requests", distinct=True),
            artisan_count=Count("services__artisan_services_category__artisan", distinct=True),
        ).order_by("-request_count")[:12]
    )
    for row in rows:
        categories.append({
            "id": str(row.pk), "name": row.name,
            "requests": row.request_count, "artisans": row.artisan_count,
            "balance": round(row.artisan_count / row.request_count, 2) if row.request_count else None,
        })
    return {
        "categories": categories,
        "definition": "Requests per category (all time) alongside the number of artisans offering a service in that category. "
                      "A ratio below 1 indicates more demand than registered supply.",
    }


def retention_cohorts(rng, *, months=6):
    """Month-of-first-booking cohorts and how many returned in later months."""
    tz = timezone.get_current_timezone()
    start = (rng.start - timedelta(days=30 * months)).replace(day=1)
    rows = (
        Booking.objects.filter(created_at__gte=start)
        .annotate(month=TruncDate("created_at", tzinfo=tz))
        .values("customer_id", "month")
    )
    seen = {}
    for row in rows:
        key = (row["month"].year, row["month"].month)
        seen.setdefault(row["customer_id"], []).append(key)
    cohorts = {}
    for customer, months_seen in seen.items():
        first = min(months_seen)
        cohorts.setdefault(first, set()).add(customer)
    output = []
    for (year, month) in sorted(cohorts)[-months:]:
        size = len(cohorts[(year, month)])
        retained = 0
        for customer, months_seen in seen.items():
            if min(months_seen) == (year, month) and len(months_seen) > 1:
                retained += 1
        output.append({
            "cohort": f"{year}-{month:02d}",
            "new_customers": size,
            "returned": retained,
            "retention_percent": ratio(retained, size),
        })
    return output


# --------------------------------------------------------------------------- #
# Service intelligence
# --------------------------------------------------------------------------- #
def services(user, params):
    rng = resolve_range(params)
    scope = viewer_fingerprint(user)
    limit = min(200, max(5, int(params.get("limit") or 50)))
    sort = str(params.get("sort") or "requests")

    def build():
        categories = ServiceCategory.objects.annotate(
            request_count=Count("service_requests", filter=Q(service_requests__created_at__gte=rng.start,
                                                             service_requests__created_at__lt=rng.end), distinct=True),
            unique_customers=Count("service_requests__requester", filter=Q(
                service_requests__created_at__gte=rng.start, service_requests__created_at__lt=rng.end), distinct=True),
            artisan_count=Count("services__artisan_services_category__artisan", distinct=True),
        )
        rows = []
        bookings_by_cat = {
            row["request__category_id"]: row
            for row in Booking.objects.filter(created_at__gte=rng.start, created_at__lt=rng.end)
            .values("request__category_id")
            .annotate(
                bookings=Count("pk"),
                completed=Count("pk", filter=Q(status=Booking.Status.COMPLETED)),
                cancelled=Count("pk", filter=Q(status__in=[Booking.Status.CANCELLED, Booking.Status.REJECTED])),
                disputed=Count("pk", filter=Q(status=Booking.Status.DISPUTED)),
                gmv=Coalesce(Sum("price_agreed"), Decimal("0"), output_field=DecimalField(max_digits=20, decimal_places=2)),
            )
        }
        paid_by_cat = {
            row["booking__request__category_id"]: row
            for row in EscrowPayment.objects.filter(
                status__in=PAID_STATUSES, held_at__gte=rng.start, held_at__lt=rng.end)
            .values("booking__request__category_id")
            .annotate(paid=Count("pk"), revenue=Coalesce(Sum("platform_commission_kobo"), 0))
        }
        ratings = {
            row["booking__artisan__artisan_services__service__category_id"]: row["avg"]
            for row in Review.objects.values("booking__artisan__artisan_services__service__category_id")
            .annotate(avg=Avg("rating"))
        }
        for cat in categories:
            key = cat.pk
            booking_row = bookings_by_cat.get(key, {})
            paid_row = paid_by_cat.get(key, {})
            bookings = int(booking_row.get("bookings") or 0)
            completed = int(booking_row.get("completed") or 0)
            requests = int(cat.request_count or 0)
            rows.append({
                "id": str(key),
                "name": cat.name,
                "requests": requests,
                "unique_customers": int(cat.unique_customers or 0),
                "artisans": int(cat.artisan_count or 0),
                "offers": 0,
                "bookings": bookings,
                "paid_bookings": int(paid_row.get("paid") or 0),
                "completed": completed,
                "cancelled": int(booking_row.get("cancelled") or 0),
                "disputes": int(booking_row.get("disputed") or 0),
                "transaction_value": str(booking_row.get("gmv") or "0"),
                "platform_revenue_kobo": int((paid_row.get("revenue") or 0)),
                "completion_rate": ratio(completed, bookings),
                "cancellation_rate": ratio(int(booking_row.get("cancelled") or 0), bookings),
                "booking_conversion": ratio(bookings, requests),
                "abandoned_requests": max(0, requests - bookings),
                "average_rating": round(float(ratings.get(key) or 0), 2),
            })
        rows.sort(key=lambda row: row.get(sort, row["requests"]) or 0, reverse=True)
        return {
            "range": {"key": rng.key, "label": rng.label, "start": rng.start.isoformat(), "end": rng.end.isoformat()},
            "generated_at": timezone.now().isoformat(),
            "categories": rows[:limit],
            "count": len(rows),
            "definitions": {
                "requests": "Service requests created in range for this category.",
                "booking_conversion": "Bookings created in range over requests created in range for the category.",
                "abandoned_requests": "Requests in range with no booking in range.",
                "platform_revenue": "Commission on captured payments whose booking belongs to this category.",
            },
            "rankings": service_rankings(rng),
        }

    payload, hit = cached(["services", scope, rng.key, rng.start, rng.end, limit, sort], build)
    payload = dict(payload)
    payload["served_from_cache"] = hit
    return payload


def service_rankings(rng):
    in_range = Q(service_requests__created_at__gte=rng.start, service_requests__created_at__lt=rng.end)
    booking_range = Q(service_requests__booking__created_at__gte=rng.start,
                      service_requests__booking__created_at__lt=rng.end)
    rows = (
        Service.objects.values("id", "name")
        .annotate(
            bookings=Count("service_requests__booking", filter=booking_range, distinct=True),
            requests=Count("service_requests", filter=in_range, distinct=True),
            customers=Count("service_requests__requester", filter=in_range, distinct=True),
            completed=Count("service_requests__booking",
                            filter=booking_range & Q(service_requests__booking__status=Booking.Status.COMPLETED),
                            distinct=True),
            value=Coalesce(Sum("service_requests__booking__price_agreed", filter=booking_range),
                           Decimal("0"), output_field=DecimalField(max_digits=20, decimal_places=2)),
        )
        .filter(bookings__gt=0)
        .order_by("-bookings")[:20]
    )
    return {
        "by_bookings": [
            {"name": row["name"], "bookings": row["bookings"], "requests": row["requests"],
             "unique_customers": row["customers"], "completed": row["completed"], "value": str(row["value"])}
            for row in rows
        ],
        "method": "Ranked by bookings created inside the selected range, with the request count and unique customers "
                  "shown so a high request volume cannot be mistaken for a high conversion.",
    }


# --------------------------------------------------------------------------- #
# Customer intelligence
# --------------------------------------------------------------------------- #
def customers(user, params):
    rng = resolve_range(params)
    scope = viewer_fingerprint(user)

    def build():
        prev = rng.previous
        profiles = CustomerProfile.objects.all()
        new_in = profiles.filter(created_at__gte=rng.start, created_at__lt=rng.end)
        new_prev = profiles.filter(created_at__gte=prev.start, created_at__lt=prev.end)
        bookings = Booking.objects.filter(created_at__gte=rng.start, created_at__lt=rng.end)
        first_requests = ServiceRequest.objects.filter(created_at__gte=rng.start, created_at__lt=rng.end)
        customers_with_booking = bookings.values("customer_id").distinct().count()
        customers_with_request = first_requests.values("requester_id").distinct().count()
        repeat = _repeat_customers(bookings)
        paid = EscrowPayment.objects.filter(customer_id__in=bookings.values("customer__user_id"),
                                            status__in=PAID_STATUSES)
        spend_by_customer = list(paid.values("customer_id").annotate(total=Coalesce(Sum("total_amount_kobo"), 0))
                                 .order_by("-total")[:10])
        names = {
            row["user_id"]: row
            for row in CustomerProfile.objects.filter(user_id__in=[row["customer_id"] for row in spend_by_customer])
            .values("user_id", "user__first_name", "user__last_name")
        }
        top_spenders = [
            {"customer": f"{names.get(row['customer_id'], {}).get('user__first_name', '')} "
                         f"{names.get(row['customer_id'], {}).get('user__last_name', '')}".strip() or "Customer",
             "value_kobo": int(row["total"])}
            for row in spend_by_customer
        ]
        avg_spend = (_sum(paid, "total_amount_kobo") // paid.values("customer_id").distinct().count()
                     if paid.exists() else 0)
        no_booking = profiles.exclude(id__in=Booking.objects.values("customer_id")).count()
        gaps = _average_days_between_bookings(rng)
        return {
            "range": {"key": rng.key, "label": rng.label, "start": rng.start.isoformat(), "end": rng.end.isoformat(),
                      "days": rng.days},
            "generated_at": timezone.now().isoformat(),
            "growth": metric("New customers", new_in.count(), previous=new_prev.count(),
                             definition="Customer profiles created in range."),
            "activity": {
                "active_customers": metric("Active customers", customers_with_booking,
                                           definition="Customers with at least one booking created in range."),
                "customers_with_requests": metric("Customers who requested", customers_with_request,
                                                  definition="Distinct requesters with a request in range."),
                "first_request_conversion": _ratio_metric(
                    "First request conversion", customers_with_request, new_in.count() or customers_with_request,
                    definition="New customers who submitted a request in range, over new customers in range."),
                "first_booking_conversion": _ratio_metric(
                    "First booking conversion", customers_with_booking, new_in.count() or customers_with_booking,
                    definition="New customers who got a booking in range, over new customers in range."),
                "average_requests_per_customer": metric(
                    "Average requests per customer",
                    round(first_requests.count() / customers_with_request, 2) if customers_with_request else 0,
                    unit="ratio", definition="Requests in range divided by distinct requesters in range."),
                "customers_without_bookings": metric("Customers with no booking ever", no_booking,
                                                     definition="Customer profiles with no booking row at all."),
                "repeat_customers": metric("Repeat customers", repeat,
                                           definition="Customers with 2+ bookings in range."),
                "repeat_rate": _ratio_metric("Repeat booking rate", repeat, customers_with_booking,
                                             definition="Repeat customers over customers with a booking."),
            },
            "spend": {
                "average_spend_kobo": metric("Average customer spend", avg_spend, unit="kobo",
                                             definition="Captured payments divided by customers who paid."),
                "median_booking_value_kobo": metric(
                    "Median booking value", int(_median(
                        bookings.annotate(kobo=F("price_agreed") * 100), "kobo") or 0), unit="kobo",
                    definition="Middle agreed value of bookings created in range."),
                "top_spenders": top_spenders,
            },
            "time_between_bookings_days": gaps,
            "segments": _customer_segments(),
            "cohorts": retention_cohorts(rng),
        }

    payload, hit = cached(["customers", scope, rng.key, rng.start, rng.end], build)
    payload = dict(payload)
    payload["served_from_cache"] = hit
    return payload


def _average_days_between_bookings(rng):
    rows = (
        Booking.objects.filter(created_at__gte=rng.start - timedelta(days=365))
        .order_by("customer_id", "created_at")
        .values_list("customer_id", "created_at")
    )
    previous = {}
    gaps = []
    for customer_id, created_at in rows:
        if customer_id in previous:
            gaps.append((created_at - previous[customer_id]).total_seconds() / 86400)
        previous[customer_id] = created_at
    if not gaps:
        return {"value": None, "sample": 0,
                "definition": "Days between consecutive bookings for customers with more than one booking."}
    gaps.sort()
    mid = len(gaps) // 2
    median = gaps[mid] if len(gaps) % 2 else (gaps[mid - 1] + gaps[mid]) / 2
    return {
        "value": round(sum(gaps) / len(gaps), 1),
        "median": round(median, 1),
        "sample": len(gaps),
        "definition": "Days between consecutive bookings for customers with more than one booking.",
    }


def _customer_segments():
    paid_customers = set(EscrowPayment.objects.filter(status__in=PAID_STATUSES).values_list("customer_id", flat=True))
    booking_customers = set(Booking.objects.values_list("customer_id", flat=True))
    request_customers = set(ServiceRequest.objects.values_list("requester_id", flat=True))
    all_customers = set(CustomerProfile.objects.values_list("id", flat=True))
    return [
        {"key": "never_booked", "label": "Requested but never booked",
         "count": len(request_customers - booking_customers)},
        {"key": "booked_never_paid", "label": "Booked but never paid",
         "count": len(booking_customers - paid_customers)},
        {"key": "paid", "label": "Has paid at least once", "count": len(paid_customers)},
        {"key": "silent", "label": "Never requested anything", "count": len(all_customers - request_customers)},
    ]


# --------------------------------------------------------------------------- #
# Artisan intelligence
# --------------------------------------------------------------------------- #
def artisans(user, params):
    rng = resolve_range(params)
    scope = viewer_fingerprint(user)

    def build():
        prev = rng.previous
        profiles = ArtisanProfile.objects.select_related("user")
        verified = ArtisanVerification.objects.filter(verification_status="VERIFIED").values("artisan_id")
        bookings = Booking.objects.filter(created_at__gte=rng.start, created_at__lt=rng.end)
        previous_bookings = Booking.objects.filter(created_at__gte=prev.start, created_at__lt=prev.end)
        per_artisan = (
            bookings.values("artisan_id")
            .annotate(
                bookings=Count("pk"),
                completed=Count("pk", filter=Q(status=Booking.Status.COMPLETED)),
                cancelled=Count("pk", filter=Q(status__in=[Booking.Status.CANCELLED, Booking.Status.REJECTED])),
                disputed=Count("pk", filter=Q(status=Booking.Status.DISPUTED)),
                value=Coalesce(Sum("price_agreed"), Decimal("0"), output_field=DecimalField(max_digits=20, decimal_places=2)),
                repeat_customers=Count("customer_id", distinct=True),
            )
        )
        earnings = {
            row["artisan_id"]: int(row["total"] or 0)
            for row in EscrowRelease.objects.filter(status=EscrowRelease.Status.SUCCESSFUL,
                                                    completed_at__gte=rng.start, completed_at__lt=rng.end)
            .values("payment__artisan_id").annotate(total=Sum("amount_kobo"))
        }
        payouts_pending = {
            row["payment__artisan_id"]: int(row["total"] or 0)
            for row in EscrowRelease.objects.filter(status=EscrowRelease.Status.PENDING)
            .values("payment__artisan_id").annotate(total=Sum("amount_kobo"))
        }
        ratings = {row["artisan_id"]: float(row["avg"] or 0)
                   for row in Review.objects.values("artisan_id").annotate(avg=Avg("rating"))}
        names = {str(row["id"]): row for row in profiles.values("id", "user__first_name", "user__last_name", "business_name")}
        leaderboard = []
        for row in per_artisan:
            aid = row["artisan_id"]
            identity = names.get(str(aid), {})
            display = identity.get("business_name") or (
                f"{identity.get('user__first_name', '')} {identity.get('user__last_name', '')}".strip())
            leaderboard.append({
                "artisan_id": str(aid),
                "name": display or "Artisan",
                "bookings": row["bookings"],
                "completed": row["completed"],
                "cancelled": row["cancelled"],
                "disputed": row["disputed"],
                "value": str(row["value"]),
                "unique_customers": row["repeat_customers"],
                "cancellation_rate": ratio(row["cancelled"], row["bookings"]),
                "dispute_rate": ratio(row["disputed"], row["bookings"]),
                "completion_rate": ratio(row["completed"], row["bookings"]),
                "earnings_kobo": earnings.get(aid, 0),
                "pending_payout_kobo": payouts_pending.get(aid, 0),
                "rating": round(ratings.get(aid, 0), 2),
            })
        leaderboard.sort(key=lambda row: (-row["completed"], -row["bookings"]))
        response = _artisan_response_times(rng)
        return {
            "range": {"key": rng.key, "label": rng.label, "start": rng.start.isoformat(), "end": rng.end.isoformat(),
                      "days": rng.days},
            "generated_at": timezone.now().isoformat(),
            "totals": {
                "artisans": _count(profiles),
                "verified": _count(profiles.filter(id__in=verified)),
                "active": _count(profiles.filter(profile_status="ACTIVE")),
                "inactive": _count(profiles.exclude(profile_status="ACTIVE")),
                "new_in_range": metric("New artisans", profiles.filter(created_at__gte=rng.start,
                                                                       created_at__lt=rng.end).count(),
                                       previous=profiles.filter(created_at__gte=prev.start,
                                                                created_at__lt=prev.end).count(),
                                       definition="Artisan profiles created in range."),
                "bookings": metric("Bookings received", bookings.count(), previous=previous_bookings.count(),
                                   definition="Bookings created in range where this artisan was chosen."),
                "completed": metric("Completed jobs", bookings.filter(status=Booking.Status.COMPLETED).count(),
                                    definition="Bookings created in range that reached COMPLETED."),
            },
            "response": response,
            "leaderboard": leaderboard[:20],
            "ranking_method": (
                "Artisans are ordered by completed jobs in range, then by bookings. The table shows completion, "
                "cancellation and dispute rates so a high volume cannot hide poor outcomes. No composite score is "
                "invented: each ranking column is a single measured quantity."
            ),
        }

    payload, hit = cached(["artisans", scope, rng.key, rng.start, rng.end], build)
    payload = dict(payload)
    payload["served_from_cache"] = hit
    return payload


def _artisan_response_times(rng):
    rows = (
        Offer.objects.filter(created_at__gte=rng.start, created_at__lt=rng.end)
        .values("created_at", "conversation__created_at")
    )
    deltas = []
    for row in rows:
        if row["conversation__created_at"]:
            deltas.append((row["created_at"] - row["conversation__created_at"]).total_seconds() / 3600)
    if not deltas:
        return {"average_hours": None, "median_hours": None, "p90_hours": None,
                "definition": "Hours between a conversation opening and the artisan's first offer."}
    deltas.sort()
    mid = len(deltas) // 2
    median = deltas[mid] if len(deltas) % 2 else (deltas[mid - 1] + deltas[mid]) / 2
    p90 = deltas[min(len(deltas) - 1, int(len(deltas) * 0.9))]
    return {
        "average_hours": round(sum(deltas) / len(deltas), 2),
        "median_hours": round(median, 2),
        "p90_hours": round(p90, 2),
        "sample": len(deltas),
        "definition": "Hours between a conversation opening and the artisan's first offer.",
    }


# --------------------------------------------------------------------------- #
# Funnel
# --------------------------------------------------------------------------- #
FUNNEL_STAGES = [
    ("request.created", "Request started"),
    ("request.submitted", "Request submitted"),
    ("artisan.responded", "Artisan response"),
    ("conversation.started", "Conversation started"),
    ("offer.submitted", "Offer submitted"),
    ("offer.accepted", "Offer accepted"),
    ("booking.created", "Booking created"),
    ("payment.initiated", "Payment initiated"),
    ("payment.succeeded", "Payment successful"),
    ("service.started", "Service started"),
    ("service.completed", "Service completed"),
    ("service.confirmed", "Completion confirmed"),
]


def funnel(user, params):
    rng = resolve_range(params)
    scope = viewer_fingerprint(user)

    def build():
        stage_rows = {
            row["name"]: {"count": row["n"]}
            for row in AnalyticsEvent.objects.filter(occurred_at__gte=rng.start, occurred_at__lt=rng.end)
            .values("name")
            .annotate(n=Count("pk", distinct=True))
        }
        stages = []
        initial = stage_rows.get(FUNNEL_STAGES[0][0], {}).get("count", 0)
        previous_count = None
        for key, label in FUNNEL_STAGES:
            count = stage_rows.get(key, {}).get("count", 0)
            stages.append({
                "key": key,
                "label": label,
                "count": count,
                "conversion_from_previous": ratio(count, previous_count) if previous_count else None,
                "conversion_from_start": ratio(count, initial),
                "drop_off": max(0, (previous_count or 0) - count) if previous_count is not None else 0,
                "drop_off_percent": ratio(max(0, (previous_count or 0) - count), previous_count) if previous_count else None,
            })
            previous_count = count
        return {
            "range": {"key": rng.key, "label": rng.label, "start": rng.start.isoformat(), "end": rng.end.isoformat(),
                      "days": rng.days},
            "generated_at": timezone.now().isoformat(),
            "stages": stages,
            "method": "Every stage counts distinct analytics events recorded in the range, not current record statuses. "
                      "A booking cancelled last month was still created, and still counts as a booking created.",
            "operational_kpis": operational_kpis(),
            "timings": _stage_timings(rng),
        }

    payload, hit = cached(["funnel", scope, rng.key, rng.start, rng.end], build)
    payload = dict(payload)
    payload["served_from_cache"] = hit
    return payload


def operational_kpis():
    now = timezone.now()
    bookings = Booking.objects.all()
    offers = Offer.objects.all()
    requests = ServiceRequest.objects.all()
    payments = EscrowPayment.objects.all()
    return {
        "requests_not_booked": requests.filter(status__in=[ServiceRequest.Status.OPEN, ServiceRequest.Status.CLOSED]).count(),
        "requests_without_response": requests.filter(status=ServiceRequest.Status.OPEN,
                                                     conversation__isnull=True).count(),
        "requests_without_offers": requests.filter(conversation__isnull=False).exclude(
            conversation__offers__isnull=False).count(),
        "offers_awaiting_customer": offers.filter(status=Offer.Status.PROPOSED).count(),
        "offers_rejected": offers.filter(status=Offer.Status.DECLINED).count(),
        "accepted_offers_without_booking": offers.filter(status=Offer.Status.ACCEPTED,
                                                         conversation__service_request__booking__isnull=True).count(),
        "bookings_unpaid": bookings.filter(status=Booking.Status.REQUESTED).count(),
        "payments_incomplete": payments.filter(status=EscrowPayment.Status.PAYMENT_PENDING,
                                               created_at__lt=now - timedelta(hours=1)).count(),
        "payments_failed": payments.filter(status=EscrowPayment.Status.FAILED).count(),
        "paid_waiting_to_start": payments.filter(status=EscrowPayment.Status.HELD,
                                                 booking__status=Booking.Status.ACCEPTED).count(),
        "services_in_progress": bookings.filter(status=Booking.Status.IN_PROGRESS).count(),
        "services_overdue": bookings.filter(status=Booking.Status.IN_PROGRESS,
                                            scheduled_time_end__lt=now).count(),
        "completed_awaiting_confirmation": bookings.filter(status=Booking.Status.COMPLETION_REQUESTED).count(),
        "cancelled_bookings": bookings.filter(status__in=[Booking.Status.CANCELLED, Booking.Status.REJECTED]).count(),
        "expired_requests": requests.filter(status=ServiceRequest.Status.CLOSED).count(),
        "disputed_bookings": bookings.filter(status=Booking.Status.DISPUTED).count(),
        "computed_at": now.isoformat(),
    }


def _stage_timings(rng):
    """Median and p90 time between request creation and booking creation."""
    rows = (
        Booking.objects.filter(created_at__gte=rng.start, created_at__lt=rng.end)
        .values("created_at", "request__created_at")
    )
    deltas = sorted(
        (row["created_at"] - row["request__created_at"]).total_seconds() / 3600
        for row in rows if row["request__created_at"]
    )
    if not deltas:
        return {"request_to_booking_hours": None}
    mid = len(deltas) // 2
    median = deltas[mid] if len(deltas) % 2 else (deltas[mid - 1] + deltas[mid]) / 2
    return {
        "request_to_booking_hours": {
            "average": round(sum(deltas) / len(deltas), 2),
            "median": round(median, 2),
            "p90": round(deltas[min(len(deltas) - 1, int(len(deltas) * 0.9))], 2),
            "sample": len(deltas),
            "definition": "Hours from the request being created to the booking being created.",
        }
    }


# --------------------------------------------------------------------------- #
# Marketing intelligence
# --------------------------------------------------------------------------- #
def marketing(user, params):
    rng = resolve_range(params)
    scope = viewer_fingerprint(user)

    def build():
        campaigns = Campaign.objects.all()
        sent = campaigns.filter(status__in=[Campaign.Status.SENT, Campaign.Status.DISPATCHING])
        deliveries = CampaignDelivery.objects.filter(created_at__gte=rng.start, created_at__lt=rng.end)
        by_status = {
            row["status"]: row["n"] for row in deliveries.values("status").annotate(n=Count("pk"))
        }
        queued = by_status.get("QUEUED", 0)
        sent_count = by_status.get("SENT", 0) + by_status.get("DELIVERED", 0)
        failed = by_status.get("FAILED", 0)
        skipped = by_status.get("SKIPPED", 0)
        clicked = deliveries.filter(clicked_at__isnull=False).count()
        opened = deliveries.filter(opened_at__isnull=False).count()
        attributed = deliveries.filter(booking_id__gt="").values("booking_id").distinct().count()
        attributed_value = _attributed_value(deliveries, rng)
        promotion = _promotion_summary(rng)
        return {
            "range": {"key": rng.key, "label": rng.label, "start": rng.start.isoformat(), "end": rng.end.isoformat()},
            "generated_at": timezone.now().isoformat(),
            "campaigns": {
                "total": _count(campaigns),
                "scheduled": _count(campaigns.filter(status=Campaign.Status.SCHEDULED)),
                "sent": _count(sent),
                "drafts": _count(campaigns.filter(status=Campaign.Status.DRAFT)),
                "pending_approval": _count(campaigns.filter(status=Campaign.Status.PENDING_APPROVAL)),
            },
            "delivery": {
                "queued": queued,
                "delivered": sent_count,
                "failed": failed,
                "suppressed": skipped,
                "opens": opened,
                "clicks": clicked,
                "open_rate": ratio(opened, sent_count),
                "click_rate": ratio(clicked, sent_count),
                "failure_rate": ratio(failed, sent_count + failed),
                "definition": "Opens and clicks are only recorded when the channel can report them; in-app deliveries "
                              "report a click only when the recipient follows the call to action.",
            },
            "attribution": {
                "window_days": 7,
                "attributed_bookings": attributed,
                "attributed_value_kobo": attributed_value,
                "method": "A booking is attributed to a campaign when the recipient followed the campaign link and "
                          "created a booking inside the campaign's attribution window. Correlation is not causation: "
                          "these recipients may have booked anyway.",
            },
            "promotions": promotion,
            "top_campaigns": _top_campaigns(rng),
        }

    payload, hit = cached(["marketing", scope, rng.key, rng.start, rng.end], build)
    payload = dict(payload)
    payload["served_from_cache"] = hit
    return payload


def _attributed_value(deliveries, rng):
    booking_ids = [row for row in deliveries.filter(booking_id__gt="").values_list("booking_id", flat=True)[:500]]
    if not booking_ids:
        return 0
    total = Booking.objects.filter(id__in=[b for b in booking_ids if _is_uuid(b)]).aggregate(
        t=Coalesce(Sum("price_agreed"), Decimal("0"), output_field=DecimalField(max_digits=20, decimal_places=2))
    )["t"]
    return int((total or 0) * 100)


def _is_uuid(value):
    try:
        import uuid as _uuid

        _uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _top_campaigns(rng):
    rows = (
        Campaign.objects.filter(created_at__gte=rng.start - timedelta(days=365))
        .annotate(
            delivered=Count("deliveries", filter=Q(deliveries__status__in=["SENT", "DELIVERED"])),
            failed=Count("deliveries", filter=Q(deliveries__status="FAILED")),
            clicked=Count("deliveries", filter=Q(deliveries__clicked_at__isnull=False)),
        )
        .order_by("-delivered")[:10]
    )
    return [
        {"id": str(row.id), "title": row.title, "status": row.status, "audience": row.audience_count,
         "delivered": row.delivered, "failed": row.failed, "clicked": row.clicked,
         "click_rate": ratio(row.clicked, row.delivered)}
        for row in rows
    ]


def _promotion_summary(rng):
    try:
        from promotions.models import PromotionCampaign, PromotionPayment

        paid = PromotionPayment.objects.filter(status="SUCCESSFUL", verified_at__gte=rng.start,
                                               verified_at__lt=rng.end)
        return {
            "revenue_kobo": _sum(paid, "amount_kobo"),
            "active_campaigns": PromotionCampaign.objects.filter(status="ACTIVE").count(),
            "payments_verified": paid.count(),
            "definition": "Verified promotion payments inside the range.",
        }
    except Exception:  # pragma: no cover
        return {"revenue_kobo": 0, "active_campaigns": 0, "payments_verified": 0, "definition": "Unavailable."}


# --------------------------------------------------------------------------- #
# Geographic intelligence
# --------------------------------------------------------------------------- #
def geography(user, params):
    rng = resolve_range(params)
    scope = viewer_fingerprint(user)

    def build():
        rows = []
        artisan_rows = {
            row["state"]: row["n"]
            for row in ArtisanProfile.objects.exclude(state="").values("state").annotate(n=Count("pk"))
        }
        request_rows = {
            row["location__state"]: row
            for row in ServiceRequest.objects.filter(created_at__gte=rng.start, created_at__lt=rng.end)
            .exclude(location__isnull=True)
            .values("location__state")
            .annotate(requests=Count("pk"), customers=Count("requester", distinct=True))
        }
        booking_rows = {
            row["request__location__state"]: row
            for row in Booking.objects.filter(created_at__gte=rng.start, created_at__lt=rng.end)
            .exclude(request__location__isnull=True)
            .values("request__location__state")
            .annotate(bookings=Count("pk"),
                      value=Coalesce(Sum("price_agreed"), Decimal("0"),
                                     output_field=DecimalField(max_digits=20, decimal_places=2)))
        }
        states = set(artisan_rows) | set(request_rows) | set(booking_rows)
        for state in sorted(states):
            request_row = request_rows.get(state, {})
            booking_row = booking_rows.get(state, {})
            rows.append({
                "state": state,
                "artisans": artisan_rows.get(state, 0),
                "requests": int(request_row.get("requests") or 0),
                "customers": int(request_row.get("customers") or 0),
                "bookings": int(booking_row.get("bookings") or 0),
                "value": str(booking_row.get("value") or "0"),
                "conversion": ratio(int(booking_row.get("bookings") or 0), int(request_row.get("requests") or 0)),
            })
        rows.sort(key=lambda row: row["requests"], reverse=True)
        return {
            "range": {"key": rng.key, "label": rng.label, "start": rng.start.isoformat(), "end": rng.end.isoformat()},
            "generated_at": timezone.now().isoformat(),
            "states": rows,
            "definition": "Grouped by the state stored on the request location and the artisan profile. "
                          "Coordinates are not collected, so this page does not plot a map.",
        }

    payload, hit = cached(["geography", scope, rng.key, rng.start, rng.end], build)
    payload = dict(payload)
    payload["served_from_cache"] = hit
    return payload


# --------------------------------------------------------------------------- #
# Management insights (transparent rules)
# --------------------------------------------------------------------------- #
def insights(user, params):
    rng = resolve_range(params)
    scope = viewer_fingerprint(user)

    def build():
        prev = rng.previous
        rules = []
        requests_now = ServiceRequest.objects.filter(created_at__gte=rng.start, created_at__lt=rng.end).count()
        requests_prev = ServiceRequest.objects.filter(created_at__gte=prev.start, created_at__lt=prev.end).count()
        bookings_now = Booking.objects.filter(created_at__gte=rng.start, created_at__lt=rng.end).count()
        bookings_prev = Booking.objects.filter(created_at__gte=prev.start, created_at__lt=prev.end).count()
        unpaid_now = Booking.objects.filter(status=Booking.Status.REQUESTED,
                                             created_at__gte=rng.start, created_at__lt=rng.end).count()
        unpaid_prev = Booking.objects.filter(status=Booking.Status.REQUESTED,
                                             created_at__gte=prev.start, created_at__lt=prev.end).count()
        failed_now = EscrowPayment.objects.filter(status=EscrowPayment.Status.FAILED,
                                                  created_at__gte=rng.start, created_at__lt=rng.end).count()
        failed_prev = EscrowPayment.objects.filter(status=EscrowPayment.Status.FAILED,
                                                   created_at__gte=prev.start, created_at__lt=prev.end).count()

        def add(key, title, observed, change, segment, method, action, severity, evidence_link,
                previous=None):
            rules.append({
                "key": key, "title": title, "observed": observed, "change_percent": change,
                "previous": previous,
                "segment": segment, "method": method, "suggested_action": action,
                "severity": severity, "evidence_link": evidence_link,
                "range_start": rng.start.isoformat(), "range_end": rng.end.isoformat(),
                "comparison_period": f"{prev.start.date()} to {prev.end.date()}",
            })

        if requests_now > requests_prev and bookings_now <= bookings_prev:
            add("requests_up_bookings_flat",
                "Requests increased while bookings did not",
                f"{requests_now} requests vs {requests_prev}; {bookings_now} bookings vs {bookings_prev}",
                growth(bookings_now, bookings_prev), "Marketplace demand vs supply",
                "Compares request count and booking count in the selected range with the immediately preceding range "
                "of equal length.",
                "Check the funnel page for the stage where requests stop: response, offer or payment.",
                "HIGH", "/manager/funnel/")
        if unpaid_now > unpaid_prev:
            add("unpaid_bookings_up", "Unpaid bookings increased",
                f"{unpaid_now} accepted-but-unpaid bookings vs {unpaid_prev}",
                growth(unpaid_now, unpaid_prev), "Booking & escrow",
                "Counts bookings still in REQUESTED status created in each range.",
                "Review the payment page for failures and contact customers with accepted agreements.",
                "MEDIUM", "/manager/financial/", previous=unpaid_prev)
        if failed_now > failed_prev and failed_now > 0:
            add("payment_failures_up", "Payment failures increased",
                f"{failed_now} failed payments vs {failed_prev}",
                growth(failed_now, failed_prev), "Payments",
                "Counts escrow payments with status FAILED created in each range.",
                "Check the payment provider status on the system health page before assuming a product problem.",
                "HIGH", "/manager/health/", previous=failed_prev)
        refunds_now = SupportFinancialAction.objects.filter(status=SupportFinancialAction.Status.SUCCEEDED,
                                                           completed_at__gte=rng.start,
                                                           completed_at__lt=rng.end).count()
        refunds_prev = SupportFinancialAction.objects.filter(status=SupportFinancialAction.Status.SUCCEEDED,
                                                            completed_at__gte=prev.start,
                                                            completed_at__lt=prev.end).count()
        if refunds_now > refunds_prev and refunds_now > 0:
            add("refunds_up", "Refund rate increased",
                f"{refunds_now} completed refunds vs {refunds_prev}",
                growth(refunds_now, refunds_prev), "Refunds",
                "Counts refund financial actions that completed in each range.",
                "Review the disputes page for the categories driving the increase.",
                "MEDIUM", "/manager/support/")
        response = _artisan_response_times(rng)
        if response.get("median_hours") is not None and response["median_hours"] > 24:
            add("response_time_slow", "Artisan response times are long",
                f"Median {response['median_hours']} hours between conversation opening and first offer",
                None, "Artisan response",
                "Median of (offer.created_at - conversation.created_at) for offers created in range.",
                "Consider more artisans in the busiest categories on the service intelligence page.",
                "MEDIUM", "/manager/services/")
        for row in _category_gaps(rng)[:3]:
            add(f"category_gap_{row['id']}",
                f"{row['name']} has high demand and low completion",
                f"{row['requests']} requests, {row['completed']} completed ({row['completion_rate']}%)",
                None, f"Category {row['name']}",
                "Requests created in range in a category whose completed bookings divided by requests is below 20%.",
                "Inspect supply for this category: artisan count and response time.",
                "MEDIUM", "/manager/services/")
        return {
            "range": {"key": rng.key, "label": rng.label, "start": rng.start.isoformat(), "end": rng.end.isoformat()},
            "generated_at": timezone.now().isoformat(),
            "insights": rules,
            "method": "Rule based. Each rule states the two figures it compared, the periods, and the calculation. "
                      "A rule reports an association in the data; it does not claim that one figure caused the other.",
            "data_freshness": _freshness(),
        }

    payload, hit = cached(["insights", scope, rng.key, rng.start, rng.end], build)
    payload = dict(payload)
    payload["served_from_cache"] = hit
    return payload


def _category_gaps(rng):
    rows = []
    for row in ServiceCategory.objects.annotate(
        requests=Count("service_requests", filter=Q(service_requests__created_at__gte=rng.start,
                                                    service_requests__created_at__lt=rng.end), distinct=True),
        artisans=Count("services__artisan_services_category", distinct=True),
    ).filter(requests__gte=1):
        completed = Booking.objects.filter(created_at__gte=rng.start, created_at__lt=rng.end,
                                           status=Booking.Status.COMPLETED,
                                           request__category=row).count()
        rate = ratio(completed, row.requests)
        if rate < 20:
            rows.append({"id": str(row.pk), "name": row.name, "requests": row.requests,
                         "completed": completed, "completion_rate": rate, "artisans": row.artisans})
    rows.sort(key=lambda row: row["requests"], reverse=True)
    return rows


# --------------------------------------------------------------------------- #
# Data quality
# --------------------------------------------------------------------------- #
def data_quality(user, params):
    def build():
        now = timezone.now()
        day_ago = now - timedelta(days=1)
        # A duplicate is the SAME event recorded twice, which is what a broken
        # retry produces: same name, entity AND dedupe key. Counting repeats of
        # (name, entity) flagged a healthy campaign fan-out - one delivery event
        # per recipient - as a HIGH severity fault.
        duplicate_events = (
            AnalyticsEvent.objects.exclude(dedupe_key="")
            .values("dedupe_key").annotate(n=Count("pk")).filter(n__gt=1).count()
        )
        missing_booking_timestamps = Booking.objects.filter(created_at__isnull=True).count()
        bad_payment_amounts = EscrowPayment.objects.exclude(
            total_amount_kobo=F("service_amount_kobo") + F("customer_fee_kobo")).count()
        orphan_bookings = Booking.objects.filter(request__isnull=True).count()
        inconsistent_currency = PaymentLedgerLine.objects.exclude(currency="NGN").count()
        snapshot_gaps = 0
        last_snapshot = OperationalSnapshot.objects.order_by("-captured_for").values_list("captured_for", flat=True).first()
        if last_snapshot:
            expected = int((now - last_snapshot).total_seconds() // 60)
            actual = OperationalSnapshot.objects.filter(
                captured_for__gte=now - timedelta(hours=1)).values("captured_for").distinct().count()
            snapshot_gaps = max(0, min(60, expected) - actual)
        events_stale = not AnalyticsEvent.objects.filter(occurred_at__gte=day_ago).exists()
        checks = [
            {"key": "duplicate_events", "label": "Analytics events stored twice (same dedupe key)",
             "count": duplicate_events, "severity": "HIGH"},
            {"key": "missing_booking_created_at", "label": "Bookings with no creation timestamp",
             "count": missing_booking_timestamps, "severity": "HIGH"},
            {"key": "payment_amount_mismatch", "label": "Payments whose parts do not sum to the total",
             "count": bad_payment_amounts, "severity": "HIGH"},
            {"key": "orphan_bookings", "label": "Bookings with no request", "count": orphan_bookings, "severity": "HIGH"},
            {"key": "currency_mismatch", "label": "Ledger lines outside NGN", "count": inconsistent_currency, "severity": "MEDIUM"},
            {"key": "snapshot_gaps", "label": "Missing snapshot intervals in the last hour", "count": snapshot_gaps, "severity": "LOW"},
            {"key": "events_stale", "label": "No analytics events recorded in 24 hours", "count": 1 if events_stale else 0,
             "severity": "MEDIUM"},
        ]
        return {
            "generated_at": now.isoformat(),
            "checks": checks,
            "clean": all(check["count"] == 0 for check in checks),
            "note": "A clean result means these specific checks passed. It does not certify the data as complete.",
        }

    payload, hit = cached(["data_quality"], build, ttl=120)
    payload = dict(payload)
    payload["served_from_cache"] = hit
    return payload


# --------------------------------------------------------------------------- #
# Support & disputes
# --------------------------------------------------------------------------- #
def support(user, params):
    rng = resolve_range(params)
    scope = viewer_fingerprint(user)

    def build():
        cases = SupportCase.objects.filter(created_at__gte=rng.start, created_at__lt=rng.end)
        all_cases = SupportCase.objects.all()
        by_status = {row["status"]: row["n"] for row in cases.values("status").annotate(n=Count("pk"))}
        by_type = {row["case_type"]: row["n"] for row in cases.values("case_type").annotate(n=Count("pk"))}
        by_agent = [
            {"agent": row["assigned_to__first_name"] or "Unassigned", "count": row["n"]}
            for row in cases.values("assigned_to", "assigned_to__first_name").annotate(n=Count("pk")).order_by("-n")[:10]
        ]
        disputes = cases.filter(case_type=SupportCase.CaseType.DISPUTE)
        resolved = all_cases.filter(resolved_at__isnull=False)
        durations = [
            (row["resolved_at"] - row["created_at"]).total_seconds() / 3600
            for row in resolved.values("created_at", "resolved_at")[:1000]
        ]
        first_response = _first_response_hours(cases)
        return {
            "range": {"key": rng.key, "label": rng.label, "start": rng.start.isoformat(), "end": rng.end.isoformat()},
            "generated_at": timezone.now().isoformat(),
            "tickets": {
                "opened": cases.count(),
                "open": by_status.get("OPEN", 0),
                "under_review": by_status.get("UNDER_REVIEW", 0),
                "resolved": by_status.get("RESOLVED", 0) + by_status.get("CLOSED", 0),
                "escalated": by_status.get("ESCALATED", 0),
                "by_category": [{"case_type": k, "count": v} for k, v in by_type.items()],
                "by_agent": by_agent,
                "average_resolution_hours": round(sum(durations) / len(durations), 2) if durations else None,
                "sample": len(durations),
                "first_response_hours": first_response,
            },
            "disputes": {
                "opened": disputes.count(),
                "open": disputes.filter(status__in=OPEN_CASE_STATUSES).count(),
                "resolved": disputes.filter(status__in=["RESOLVED", "CLOSED"]).count(),
                "disputed_value_kobo": _sum(
                    EscrowPayment.objects.filter(booking__pk__in=disputes.values("booking_id"), status=EscrowPayment.Status.DISPUTED),
                    "total_amount_kobo"),
                "refunded_kobo": _sum(SupportFinancialAction.objects.filter(
                    case__in=disputes, status=SupportFinancialAction.Status.SUCCEEDED, action_type__contains="REFUND"), "amount_kobo"),
                "definition": "Disputes are support cases of type DISPUTE. Value is the escrow held on the disputed bookings.",
            },
            "definition": "Ticket counts are cases created in the selected range. Resolution time uses resolved_at "
                          "minus created_at over cases resolved at any time (last 1000), so long-running cases are not dropped.",
        }

    payload, hit = cached(["support", scope, rng.key, rng.start, rng.end], build)
    payload = dict(payload)
    payload["served_from_cache"] = hit
    return payload


def _first_response_hours(cases):
    rows = cases.values("created_at", "events__created_at").order_by("pk")
    firsts = {}
    for row in rows:
        if row["events__created_at"]:
            key = row["created_at"]
            if key not in firsts or row["events__created_at"] < firsts[key]:
                firsts[key] = row["events__created_at"]
    deltas = [(value - key).total_seconds() / 3600 for key, value in firsts.items()]
    if not deltas:
        return None
    return round(sum(deltas) / len(deltas), 2)


# --------------------------------------------------------------------------- #
# Risk
# --------------------------------------------------------------------------- #
def risk(user, params):
    rng = resolve_range(params)
    scope = viewer_fingerprint(user)

    def build():
        profiles = CustomerRiskProfile.objects.all()
        flagged = profiles.filter(risk_level__in=["WARNING", "REVIEW", "SUSPENDED"])
        failed_by_customer = (
            EscrowPayment.objects.filter(status=EscrowPayment.Status.FAILED,
                                         created_at__gte=rng.start, created_at__lt=rng.end)
            .values("customer_id").annotate(n=Count("pk")).filter(n__gt=2).count()
        )
        cancel_by_customer = (
            Booking.objects.filter(status__in=[Booking.Status.CANCELLED, Booking.Status.REJECTED],
                                   created_at__gte=rng.start, created_at__lt=rng.end)
            .values("customer_id").annotate(n=Count("pk")).filter(n__gt=2).count()
        )
        dispute_by_user = (
            SupportCase.objects.filter(case_type=SupportCase.CaseType.DISPUTE, opened_by__isnull=False,
                                       created_at__gte=rng.start, created_at__lt=rng.end)
            .values("opened_by_id").annotate(n=Count("pk")).filter(n__gt=1).count()
        )
        from manager.models import RiskReview

        reviews = RiskReview.objects.filter(created_at__gte=rng.start, created_at__lt=rng.end)
        return {
            "range": {"key": rng.key, "label": rng.label, "start": rng.start.isoformat(), "end": rng.end.isoformat()},
            "generated_at": timezone.now().isoformat(),
            "signals": [
                {"key": "repeat_failed_payments", "label": "Customers with 3+ failed payments in range",
                 "count": failed_by_customer, "severity": "HIGH"},
                {"key": "repeat_cancellations", "label": "Customers with 3+ cancellations in range",
                 "count": cancel_by_customer, "severity": "MEDIUM"},
                {"key": "repeat_disputes", "label": "Customers with more than one dispute in range",
                 "count": dispute_by_user, "severity": "MEDIUM"},
                {"key": "flagged_profiles", "label": "Risk profiles currently flagged",
                 "count": flagged.count(), "severity": "HIGH"},
                {"key": "locked_accounts", "label": "Accounts locked by security policy",
                 "count": CustomUser.objects.filter(security_status="LOCKED").count(), "severity": "HIGH"},
            ],
            "reviews": [
                {"id": str(row.id), "signal": row.signal, "severity": row.severity, "status": row.status,
                 "summary": row.summary, "created_at": row.created_at.isoformat()}
                for row in reviews.order_by("-created_at")[:20]
            ],
            "policy": "Automated signals create a review record only. No account is suspended by an anomaly; "
                      "a manager must record a decision after human review.",
        }

    payload, hit = cached(["risk", scope, rng.key, rng.start, rng.end], build)
    payload = dict(payload)
    payload["served_from_cache"] = hit
    return payload
