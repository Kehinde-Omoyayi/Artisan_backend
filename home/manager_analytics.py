"""Aggregate, read-only platform analytics for the superuser manager dashboard.

Only totals and operational summaries are returned. Provider payloads, credentials,
and individual customer or artisan contact details are intentionally excluded.
"""
from datetime import datetime, time, timedelta
import math

from django.db.models import BigIntegerField, Count, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from booking.models import Booking
from conversations.models import Conversation, Message, Offer
from home.models import ArtisanProfile, CustomerProfile, CustomUser, Service, ServiceCategory
from payments.models import EscrowPayment, EscrowRelease, PaymentLedgerLine
from risk.models import CustomerCancellationEvent, CustomerRiskProfile
from services.models import ArtisanVerification, ServiceRequest
from support.models import SupportCase, SupportFinancialAction


PAID_PAYMENT_STATUSES = (
    EscrowPayment.Status.HELD,
    EscrowPayment.Status.DISPUTED,
    EscrowPayment.Status.RELEASE_PENDING,
    EscrowPayment.Status.RELEASED,
    EscrowPayment.Status.REFUND_PENDING,
    EscrowPayment.Status.REFUNDED,
)
ACTIVE_ESCROW_STATUSES = (
    EscrowPayment.Status.HELD,
    EscrowPayment.Status.DISPUTED,
    EscrowPayment.Status.RELEASE_PENDING,
    EscrowPayment.Status.REFUND_PENDING,
)
ACTIVE_BOOKING_STATUSES = (
    Booking.Status.REQUESTED,
    Booking.Status.ACCEPTED,
    Booking.Status.IN_PROGRESS,
    Booking.Status.COMPLETION_REQUESTED,
    Booking.Status.DISPUTED,
)
ACTIVE_CASE_STATUSES = (SupportCase.Status.OPEN, SupportCase.Status.UNDER_REVIEW)
PENDING_REFUND_STATUSES = (
    SupportFinancialAction.Status.REQUESTED,
    SupportFinancialAction.Status.APPROVED,
    SupportFinancialAction.Status.PROCESSING,
    SupportFinancialAction.Status.PROCESSING_UNKNOWN,
)
PLATFORM_REVENUE_ACCOUNTS = ("PLATFORM_COMMISSION_REVENUE", "PLATFORM_FEE_REVENUE")


def _sum(queryset, field):
    return int(queryset.aggregate(total=Sum(field, output_field=BigIntegerField()))["total"] or 0)


def _percent(part, total):
    return round((part * 100 / total), 1) if total else 0.0


def _status_breakdown(queryset, choices, *, status_field="status"):
    counts = {value: 0 for value, _label in choices}
    for row in queryset.values(status_field).annotate(count=Count("pk")):
        status = row[status_field]
        counts[status] = row["count"]
    total = sum(counts.values()) or 1
    labels = dict(choices)
    return [
        {
            "status": status,
            "label": labels.get(status, status.replace("_", " ").title()),
            "count": count,
            "percent": round(count * 100 / total),
        }
        for status, count in counts.items()
    ]


def _day_bounds(day):
    tz = timezone.get_current_timezone()
    return timezone.make_aware(datetime.combine(day, time.min), tz)


def _daily_counts(queryset, start_at, end_at, *, field="created_at"):
    tz = timezone.get_current_timezone()
    rows = (
        queryset.filter(**{f"{field}__gte": start_at, f"{field}__lt": end_at})
        .annotate(day=TruncDate(field, tzinfo=tz))
        .values("day")
        .annotate(count=Count("pk"))
    )
    return {row["day"].isoformat(): int(row["count"]) for row in rows if row["day"]}


def _level(value, maximum):
    if not value:
        return 0
    return max(1, min(8, math.ceil(value * 8 / max(maximum, 1))))


def _build_promotion_overview(now):
    from promotions.models import (
        PromotionApplication,
        PromotionApplicationDecision,
        PromotionCampaign,
        PromotionCampaignPerformanceDaily,
        PromotionInventory,
        PromotionPlan,
    )
    from promotions.campaign_services import ensure_homepage_inventory
    from promotions.placement_services import eligible_public_active_campaigns

    ensure_homepage_inventory()
    public_active = eligible_public_active_campaigns(now=now)
    active_geo = [
        item for item in public_active
        if item.promotion_type in {
            PromotionApplication.PromotionType.INSTITUTION,
            PromotionApplication.PromotionType.AREA,
            PromotionApplication.PromotionType.STATE,
        }
    ]
    active_homepage = [
        item for item in public_active
        if item.promotion_type == PromotionApplication.PromotionType.HOMEPAGE
    ]
    queued = PromotionCampaign.objects.filter(status=PromotionCampaign.Status.QUEUED)
    scheduled = PromotionCampaign.objects.filter(status=PromotionCampaign.Status.SCHEDULED)
    expiring = [
        item for item in public_active
        if item.expires_at and item.expires_at < now + timedelta(days=7)
    ]
    pending_applications = PromotionApplication.objects.filter(
        status__in=(PromotionApplication.Status.PENDING_REVIEW, PromotionApplication.Status.CHANGES_REQUIRED)
    )
    application_status_counts = {
        row["status"]: int(row["count"])
        for row in PromotionApplication.objects.values("status").annotate(count=Count("pk"))
    }
    decision_status_counts = {
        row["decision_type"]: int(row["count"])
        for row in PromotionApplicationDecision.objects.values("decision_type").annotate(count=Count("pk"))
    }
    review_statistics = {
        "pending_review": application_status_counts.get(PromotionApplication.Status.PENDING_REVIEW, 0),
        "changes_required": application_status_counts.get(PromotionApplication.Status.CHANGES_REQUIRED, 0),
        "approved": application_status_counts.get(PromotionApplication.Status.APPROVED, 0),
        "rejected": application_status_counts.get(PromotionApplication.Status.REJECTED, 0),
        "decisions_last_30_days": PromotionApplicationDecision.objects.filter(
            created_at__gte=now - timedelta(days=30)
        ).count(),
        "decision_actions": {
            "approved": decision_status_counts.get(PromotionApplicationDecision.DecisionType.APPROVED, 0),
            "changes_required": decision_status_counts.get(PromotionApplicationDecision.DecisionType.CHANGES_REQUIRED, 0),
            "rejected": decision_status_counts.get(PromotionApplicationDecision.DecisionType.REJECTED, 0),
        },
    }

    campaign_status_counts = {
        row["status"]: int(row["count"])
        for row in PromotionCampaign.objects.values("status").annotate(count=Count("pk"))
    }
    active_by_type = {"INSTITUTION": 0, "AREA": 0, "STATE": 0}
    active_targets = {}
    active_count_by_inventory = {}
    for campaign in public_active:
        active_count_by_inventory[campaign.inventory_id] = active_count_by_inventory.get(campaign.inventory_id, 0) + 1
    for campaign in active_geo:
        active_by_type[campaign.promotion_type] = active_by_type.get(campaign.promotion_type, 0) + 1
        target_key = campaign.inventory.target_key
        target_row = active_targets.setdefault(
            target_key,
            {
                "promotion_type": campaign.promotion_type,
                "target_key": target_key,
                "target_label": campaign.inventory.target_label,
                "active_campaigns": 0,
            },
        )
        target_row["active_campaigns"] += 1

    expiring_campaigns = [
        {
            "campaign_id": str(item.pk),
            "promotion_type": item.promotion_type,
            "target_label": item.inventory.target_label,
            "expires_at": item.expires_at,
        }
        for item in sorted(expiring, key=lambda row: (row.expires_at, str(row.pk)))[:25]
    ]
    inventory_rows = []
    inventory_counts = (
        PromotionInventory.objects.annotate(
            scheduled_count=Count(
                "campaigns",
                filter=Q(
                    campaigns__status=PromotionCampaign.Status.SCHEDULED,
                    campaigns__payment_verified_at__isnull=False,
                ),
            ),
            queued_count=Count(
                "campaigns",
                filter=Q(
                    campaigns__status=PromotionCampaign.Status.QUEUED,
                    campaigns__payment_verified_at__isnull=False,
                ),
            ),
        )
        .order_by("promotion_type", "target_label", "target_key")
    )
    for inventory in inventory_counts:
        active_count = active_count_by_inventory.get(inventory.pk, 0)
        inventory_rows.append(
            {
                "id": str(inventory.pk),
                "promotion_type": inventory.promotion_type,
                "target_key": inventory.target_key,
                "target_label": inventory.target_label,
                "capacity": inventory.capacity,
                "active_count": active_count,
                "scheduled_count": inventory.scheduled_count,
                "queued_count": inventory.queued_count,
                "remaining_capacity": max(inventory.capacity - active_count, 0),
            }
        )
    plan_rows = [
        {
            "promotion_type": plan.promotion_type,
            "name": plan.name,
            "is_enabled": plan.is_enabled,
            "price_kobo": plan.price_kobo,
            "currency": plan.currency,
            "duration_days": plan.duration_days,
            "version": plan.version,
            "updated_at": plan.updated_at,
            "updated_by": plan.updated_by.get_full_name() if plan.updated_by else "",
        }
        for plan in PromotionPlan.objects.select_related("updated_by").order_by("promotion_type")
    ]

    campaign_totals = PromotionCampaign.objects.aggregate(
        impressions=Sum("performance_impressions"),
        profile_visits=Sum("performance_profile_visits"),
    )
    lifetime_impressions = int(campaign_totals["impressions"] or 0)
    lifetime_visits = int(campaign_totals["profile_visits"] or 0)
    today = timezone.localdate(now)
    recent_start = today - timedelta(days=29)
    recent_totals = PromotionCampaignPerformanceDaily.objects.filter(
        event_day__gte=recent_start,
        event_day__lte=today,
    ).aggregate(
        impressions=Sum("impressions"),
        profile_visits=Sum("profile_visits"),
    )
    recent_impressions = int(recent_totals["impressions"] or 0)
    recent_visits = int(recent_totals["profile_visits"] or 0)
    by_placement = {}
    for row in PromotionCampaign.objects.values("promotion_type").annotate(
        impressions=Sum("performance_impressions"),
        profile_visits=Sum("performance_profile_visits"),
    ):
        by_placement[row["promotion_type"]] = {
            "impressions": int(row["impressions"] or 0),
            "profile_visits": int(row["profile_visits"] or 0),
        }
    for promotion_type in PromotionApplication.PromotionType.values:
        by_placement.setdefault(promotion_type, {"impressions": 0, "profile_visits": 0})
    queued_count = queued.count()
    scheduled_count = scheduled.count()
    return {
        "active_geographic_campaigns": len(active_geo),
        "active_geographic_by_type": active_by_type,
        "active_targets": sorted(active_targets.values(), key=lambda row: (row["promotion_type"], row["target_label"])),
        "queued_campaigns": queued_count,
        "scheduled_campaigns": scheduled_count,
        "pending_paid_campaigns": {
            "awaiting_payment": campaign_status_counts.get(PromotionCampaign.Status.PENDING_PAYMENT, 0),
            "payment_review": campaign_status_counts.get(PromotionCampaign.Status.PAYMENT_REVIEW, 0),
            "queued": queued_count,
            "scheduled": scheduled_count,
            "paid_waiting_for_placement": queued_count + scheduled_count,
        },
        "active_homepage_placements": len(active_homepage),
        "upcoming_expirations_7d": len(expiring),
        "upcoming_expirations": expiring_campaigns,
        "pending_applications": pending_applications.count(),
        "review_statistics": review_statistics,
        "inventory": inventory_rows,
        "plans": plan_rows,
        "performance": {
            "data_source": "Deduplicated first-party promotion events with privacy-minimized aggregates",
            "lifetime": {
                "impressions": lifetime_impressions,
                "profile_visits": lifetime_visits,
                "useful_rate_percent": round(lifetime_visits * 100 / lifetime_impressions, 2) if lifetime_impressions else None,
            },
            "last_30_days": {
                "impressions": recent_impressions,
                "profile_visits": recent_visits,
                "useful_rate_percent": round(recent_visits * 100 / recent_impressions, 2) if recent_impressions else None,
            },
            "by_placement_type": by_placement,
        },
    }


def build_manager_overview(*, now=None):
    """Build the aggregate data contract consumed by the superuser-only dashboard."""
    now = now or timezone.now()
    promotion_overview = _build_promotion_overview(now)
    local_now = timezone.localtime(now)
    today = local_now.date()
    thirty_days_ago = now - timedelta(days=30)
    month_start = _day_bounds(today.replace(day=1))
    paid_period_filter = Q(held_at__gte=thirty_days_ago) | Q(verified_at__gte=thirty_days_ago)
    paid_month_filter = Q(held_at__gte=month_start) | Q(verified_at__gte=month_start)

    customers = CustomerProfile.objects.all()
    artisans = ArtisanProfile.objects.select_related("user")
    requests = ServiceRequest.objects.all()
    bookings = Booking.objects.all()
    payments = EscrowPayment.objects.all()
    ledger_fees = PaymentLedgerLine.objects.filter(
        entry__event_type="ESCROW_HELD",
        account__in=PLATFORM_REVENUE_ACCOUNTS,
        direction=PaymentLedgerLine.Direction.CREDIT,
        currency="NGN",
    )
    releases = EscrowRelease.objects.all()
    refunds = SupportFinancialAction.objects.all()
    support_cases = SupportCase.objects.all()
    paid_payments = payments.filter(status__in=PAID_PAYMENT_STATUSES)
    active_payments = payments.filter(status__in=ACTIVE_ESCROW_STATUSES)

    customer_total = customers.count()
    artisan_total = artisans.count()
    service_request_total = requests.count()
    booking_total = bookings.count()
    booking_completed = bookings.filter(status=Booking.Status.COMPLETED).count()
    booking_cancelled = bookings.filter(status=Booking.Status.CANCELLED).count()
    booking_disputed = bookings.filter(status=Booking.Status.DISPUTED).count()
    request_30d = requests.filter(created_at__gte=thirty_days_ago)
    bookings_30d = bookings.filter(created_at__gte=thirty_days_ago)
    booked_requests_30d = request_30d.filter(booking__isnull=False).distinct().count()
    request_count_30d = request_30d.count()
    paid_30d = paid_payments.filter(paid_period_filter)
    paid_month = paid_payments.filter(paid_month_filter)
    paid_lifetime_gmv = _sum(paid_payments, "service_amount_kobo")
    paid_30d_gmv = _sum(paid_30d, "service_amount_kobo")
    paid_month_gmv = _sum(paid_month, "service_amount_kobo")
    paid_lifetime_collections = _sum(paid_payments, "total_amount_kobo")
    paid_30d_collections = _sum(paid_30d, "total_amount_kobo")
    paid_month_collections = _sum(paid_month, "total_amount_kobo")

    fee_lifetime = _sum(ledger_fees, "amount_kobo")
    fee_30d = _sum(ledger_fees.filter(entry__created_at__gte=thirty_days_ago), "amount_kobo")
    fee_month = _sum(ledger_fees.filter(entry__created_at__gte=month_start), "amount_kobo")
    escrow_fund_lines = PaymentLedgerLine.objects.filter(account="ESCROW_FUNDS", currency="NGN")
    escrow_debits = _sum(
        escrow_fund_lines.filter(direction=PaymentLedgerLine.Direction.DEBIT),
        "amount_kobo",
    )
    escrow_credits = _sum(
        escrow_fund_lines.filter(direction=PaymentLedgerLine.Direction.CREDIT),
        "amount_kobo",
    )
    successful_releases = releases.filter(status=EscrowRelease.Status.SUCCESSFUL)
    payouts_lifetime = _sum(successful_releases, "amount_kobo")
    payouts_30d = _sum(successful_releases.filter(completed_at__gte=thirty_days_ago), "amount_kobo")
    payouts_month = _sum(successful_releases.filter(completed_at__gte=month_start), "amount_kobo")
    successful_refunds = refunds.filter(status=SupportFinancialAction.Status.SUCCEEDED)
    refunds_lifetime = _sum(successful_refunds, "amount_kobo")
    refunds_30d = _sum(successful_refunds.filter(completed_at__gte=thirty_days_ago), "amount_kobo")
    refunds_month = _sum(successful_refunds.filter(completed_at__gte=month_start), "amount_kobo")
    pending_refunds = refunds.filter(status__in=PENDING_REFUND_STATUSES)

    active_cases = support_cases.filter(status__in=ACTIVE_CASE_STATUSES)
    active_disputes = active_cases.filter(case_type=SupportCase.CaseType.DISPUTE)
    risk_flagged = CustomerRiskProfile.objects.filter(
        risk_level__in=("WARNING", "REVIEW", "SUSPENDED")
    ).count()
    user_security_alerts = CustomUser.objects.filter(
        security_status__in=("REVIEW", "LOCKED")
    ).count()

    offer_30d = Offer.objects.filter(created_at__gte=thirty_days_ago)
    decided_offers_30d = offer_30d.filter(
        status__in=(Offer.Status.ACCEPTED, Offer.Status.DECLINED, Offer.Status.EXPIRED, Offer.Status.CANCELLED)
    ).count()
    accepted_offers_30d = offer_30d.filter(status=Offer.Status.ACCEPTED).count()

    kpis = {
        "customers_total": customer_total,
        "customers_active": customers.filter(user__is_active=True).count(),
        "customers_new_30d": customers.filter(created_at__gte=thirty_days_ago).count(),
        "artisans_total": artisan_total,
        "artisans_active": artisans.filter(profile_status="ACTIVE", user__is_active=True).count(),
        "artisans_new_30d": artisans.filter(created_at__gte=thirty_days_ago).count(),
        "artisans_pending_verification": ArtisanVerification.objects.filter(
            verification_status="PENDING"
        ).count(),
        "artisans_verified": ArtisanVerification.objects.filter(
            verification_status="VERIFIED"
        ).count(),
        "artisans_unverified": ArtisanVerification.objects.filter(
            verification_status="UNVERIFIED"
        ).count(),
        "artisans_verification_rejected": ArtisanVerification.objects.filter(
            verification_status="REJECTED"
        ).count(),
        "artisans_suspended": ArtisanVerification.objects.filter(
            verification_status="SUSPENDED"
        ).count(),

        "services_active": Service.objects.filter(is_active=True).count(),
        "categories_total": ServiceCategory.objects.count(),
        "service_requests_total": service_request_total,
        "service_requests_30d": request_count_30d,
        "emergency_requests_30d": request_30d.filter(urgency="EMERGENCY").count(),
        "bookings_total": booking_total,
        "bookings_30d": bookings_30d.count(),
        "bookings_active": bookings.filter(status__in=ACTIVE_BOOKING_STATUSES).count(),
        "bookings_completed": booking_completed,
        "bookings_completed_30d": bookings.filter(customer_completed_at__gte=thirty_days_ago).count(),
        "bookings_cancelled": booking_cancelled,
        "bookings_disputed": booking_disputed,
        "booking_conversion_rate_30d": _percent(booked_requests_30d, request_count_30d),
        "booking_completion_rate": _percent(booking_completed, booking_total),
        "booking_cancellation_rate": _percent(booking_cancelled, booking_total),
        "booking_dispute_rate": _percent(booking_disputed, booking_total),
        "payments_total_count": payments.count(),
        "paid_payments_total": paid_payments.count(),
        "paid_payments_30d": paid_30d.count(),
        "paid_volume_lifetime_kobo": paid_lifetime_gmv,
        "paid_volume_30d_kobo": paid_30d_gmv,
        "paid_volume_month_kobo": paid_month_gmv,
        "customer_collections_lifetime_kobo": paid_lifetime_collections,
        "customer_collections_30d_kobo": paid_30d_collections,
        "customer_collections_month_kobo": paid_month_collections,
        "platform_fees_lifetime_kobo": fee_lifetime,
        "platform_fees_30d_kobo": fee_30d,
        "platform_fees_month_kobo": fee_month,
        "payouts_lifetime_kobo": payouts_lifetime,
        "payouts_30d_kobo": payouts_30d,
        "payouts_month_kobo": payouts_month,
        "refunds_lifetime_kobo": refunds_lifetime,
        "refunds_30d_kobo": refunds_30d,
        "refunds_month_kobo": refunds_month,
        "active_escrow_kobo": escrow_debits - escrow_credits,
        "escrow_funds_debits_lifetime_kobo": escrow_debits,
        "escrow_funds_credits_lifetime_kobo": escrow_credits,
        "active_escrow_payment_count": active_payments.count(),
        "payments_pending_count": payments.filter(
            status=EscrowPayment.Status.PAYMENT_PENDING
        ).count(),
        "payments_failed_count": payments.filter(status=EscrowPayment.Status.FAILED).count(),
        "refunds_pending_count": pending_refunds.count(),
        "refunds_pending_kobo": _sum(pending_refunds, "amount_kobo"),
        "refunds_unknown_count": refunds.filter(
            status=SupportFinancialAction.Status.PROCESSING_UNKNOWN
        ).count(),
        "refunds_failed_count": refunds.filter(
            status=SupportFinancialAction.Status.FAILED
        ).count(),
        "refunds_rejected_count": refunds.filter(
            status=SupportFinancialAction.Status.REJECTED
        ).count(),
        "refunds_total_count": refunds.count(),
        "payouts_pending_count": releases.filter(
            status__in=(EscrowRelease.Status.PENDING, EscrowRelease.Status.SUBMITTED)
        ).count(),
        "payouts_pending_kobo": _sum(
            releases.filter(status__in=(EscrowRelease.Status.PENDING, EscrowRelease.Status.SUBMITTED)),
            "amount_kobo",
        ),
        "support_cases_total": support_cases.count(),
        "support_cases_open": support_cases.filter(status=SupportCase.Status.OPEN).count(),
        "support_cases_review": support_cases.filter(status=SupportCase.Status.UNDER_REVIEW).count(),
        "support_cases_urgent": support_cases.filter(
            priority=SupportCase.Priority.URGENT,
            status__in=ACTIVE_CASE_STATUSES,
        ).count(),
        "support_disputes_total": support_cases.filter(
            case_type=SupportCase.CaseType.DISPUTE
        ).count(),
        "support_disputes_open": active_disputes.count(),
        "risk_customers_flagged": risk_flagged,
        "user_security_alerts": user_security_alerts,
        "cancellation_reviews_pending": CustomerCancellationEvent.objects.filter(
            requires_review=True
        ).count(),
        "conversations_total": Conversation.objects.count(),
        "conversations_active_30d": Conversation.objects.filter(
            updated_at__gte=thirty_days_ago
        ).count(),
        "messages_total": Message.objects.count(),
        "messages_30d": Message.objects.filter(created_at__gte=thirty_days_ago).count(),
        "offers_total": Offer.objects.count(),
        "offers_active": Offer.objects.filter(
            status__in=(Offer.Status.PROPOSED, Offer.Status.COUNTERED)
        ).count(),
        "offers_decided_30d": decided_offers_30d,
        "offers_accepted_30d": accepted_offers_30d,
        "offer_acceptance_rate_30d": _percent(accepted_offers_30d, decided_offers_30d),
        "promotion_active_geographic_campaigns": promotion_overview["active_geographic_campaigns"],
        "promotion_active_institution_campaigns": promotion_overview["active_geographic_by_type"]["INSTITUTION"],
        "promotion_active_area_campaigns": promotion_overview["active_geographic_by_type"]["AREA"],
        "promotion_active_state_campaigns": promotion_overview["active_geographic_by_type"]["STATE"],
        "promotion_campaigns_queued": promotion_overview["queued_campaigns"],
        "promotion_campaigns_scheduled": promotion_overview["scheduled_campaigns"],
        "promotion_homepage_placements_active": promotion_overview["active_homepage_placements"],
        "promotion_expiring_7d": promotion_overview["upcoming_expirations_7d"],
        "promotion_applications_pending": promotion_overview["pending_applications"],
        "promotion_applications_changes_required": promotion_overview["review_statistics"]["changes_required"],
        "promotion_applications_approved": promotion_overview["review_statistics"]["approved"],
        "promotion_applications_rejected": promotion_overview["review_statistics"]["rejected"],
        "promotion_payments_awaiting": promotion_overview["pending_paid_campaigns"]["awaiting_payment"],
        "promotion_payments_review": promotion_overview["pending_paid_campaigns"]["payment_review"],
        "promotion_impressions_30d": promotion_overview["performance"]["last_30_days"]["impressions"],
        "promotion_profile_visits_30d": promotion_overview["performance"]["last_30_days"]["profile_visits"],
        "promotion_useful_rate_30d_percent": promotion_overview["performance"]["last_30_days"]["useful_rate_percent"],
    }

    booking_status = _status_breakdown(bookings, Booking.Status.choices)
    payment_status = _status_breakdown(payments, EscrowPayment.Status.choices)
    support_case_status = _status_breakdown(support_cases, SupportCase.Status.choices)
    refund_status = _status_breakdown(refunds, SupportFinancialAction.Status.choices)
    verification_status = _status_breakdown(
        ArtisanVerification.objects.all(),
        ArtisanVerification.VERIFICATION_STATUS,
        status_field="verification_status",
    )

    start_day = today - timedelta(days=13)
    trend_start = _day_bounds(start_day)
    trend_end = _day_bounds(today + timedelta(days=1))
    trend_sources = {
        "requests": _daily_counts(requests, trend_start, trend_end),
        "bookings": _daily_counts(bookings, trend_start, trend_end),
        "payments": _daily_counts(
            paid_payments.filter(held_at__isnull=False),
            trend_start,
            trend_end,
            field="held_at",
        ),
        "new_customers": _daily_counts(customers, trend_start, trend_end),
        "new_artisans": _daily_counts(artisans, trend_start, trend_end),
    }
    trend_maxima = {
        key: max(values.values(), default=0) or 1
        for key, values in trend_sources.items()
    }
    activity_trend = []
    for day_offset in range(14):
        day = start_day + timedelta(days=day_offset)
        date_key = day.isoformat()
        row = {
            "date": date_key,
            "weekday": day.strftime("%a"),
            "requests": trend_sources["requests"].get(date_key, 0),
            "bookings": trend_sources["bookings"].get(date_key, 0),
            "payments": trend_sources["payments"].get(date_key, 0),
            "new_customers": trend_sources["new_customers"].get(date_key, 0),
            "new_artisans": trend_sources["new_artisans"].get(date_key, 0),
        }
        for metric in trend_sources:
            row[f"{metric}_level"] = _level(row[metric], trend_maxima[metric])
        activity_trend.append(row)

    requests_30d_total = request_count_30d or 1
    top_categories = [
        {
            "name": row["category__name"],
            "requests": int(row["count"]),
            "share_percent": round(row["count"] * 100 / requests_30d_total),
        }
        for row in request_30d.values("category__name")
        .annotate(count=Count("pk"))
        .order_by("-count", "category__name")[:6]
    ]
    top_regions = [
        {
            "name": row["location__state"] or "Unspecified",
            "requests": int(row["count"]),
            "share_percent": round(row["count"] * 100 / requests_30d_total),
        }
        for row in request_30d.values("location__state")
        .annotate(count=Count("pk"))
        .order_by("-count", "location__state")[:6]
    ]

    recent_cases = [
        {
            "id": item.id,
            "reference": item.reference,
            "subject": item.subject,
            "case_type": item.case_type,
            "status": item.status,
            "priority": item.priority,
            "updated_at": item.updated_at,
        }
        for item in support_cases.order_by("-updated_at")[:6]
    ]
    recent_requests = [
        {
            "id": item.id,
            "category": item.category.name,
            "service": item.service.name if item.service_id else "General request",
            "urgency": item.urgency,
            "created_at": item.created_at,
        }
        for item in requests.select_related("category", "service").order_by("-created_at")[:5]
    ]
    recent_bookings = [
        {
            "id": str(item.id),
            "status": item.status,
            "service": item.request.service.name if item.request.service_id else item.request.category.name,
            "price_agreed": str(item.price_agreed),
            "created_at": item.created_at,
        }
        for item in bookings.select_related("request__service", "request__category")
        .order_by("-created_at")[:5]
    ]

    return {
        "generated_at": now,
        "period_days": 30,
        "trend_days": 14,
        "timezone": str(timezone.get_current_timezone()),
        "kpis": kpis,
        "promotions": promotion_overview,
        "booking_status": booking_status,
        "payment_status": payment_status,
        "support_case_status": support_case_status,
        "refund_status": refund_status,
        "verification_status": verification_status,
        "activity_trend": activity_trend,
        "top_categories": top_categories,
        "top_regions": top_regions,
        "recent_cases": recent_cases,
        "recent_requests": recent_requests,
        "recent_bookings": recent_bookings,
        "financial_note": (
            "GMV is service value on held payments; customer collections also include customer fees. "
            "Captured platform fees are escrow-ledger credits before any later refund adjustments. "
            "The current escrow balance is net posted ESCROW_FUNDS ledger activity. Payouts and refunds "
            "count completed records only. All amounts are NGN aggregates."
        ),
    }
