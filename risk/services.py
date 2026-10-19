from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from home.generic import suspend_user
from notifications.events import notify_account_suspended, notify_customer_cancellation_risk_warning

from .models import (
    CustomerCancellationEvent,
    CustomerNetworkBlock,
    CustomerNetworkObservation,
    CustomerRiskProfile,
)
from .network import record_customer_network_observation
from .policy import (
    NETWORK_BLOCK_ESCALATED_DAYS,
    NETWORK_BLOCK_FIRST_HOURS,
    NETWORK_OBSERVATION_DAYS,
    NETWORK_BLOCK_SHARED_CUSTOMER_LIMIT,
    NETWORK_MULTI_ACCOUNT_ESCALATION,
    REASON_POLICIES,
    REVIEW_SCORE,
    ROLLING_WINDOW_DAYS,
    SUSPENSION_SCORE,
    calculate_cancellation_points,
)


# Keep the window explicit in one place so the metric is reproducible.
TERMINAL_BOOKING_STATUSES = ("COMPLETED", "CANCELLED", "REJECTED", "DISPUTED")


def _risk_level(score):
    if score >= SUSPENSION_SCORE:
        return "SUSPENDED"
    if score >= REVIEW_SCORE:
        return "REVIEW"
    if score >= 2:
        return "WARNING"
    return "NORMAL"


def _compute_profile(customer):
    cutoff = timezone.now() - timedelta(days=ROLLING_WINDOW_DAYS)
    events = CustomerCancellationEvent.objects.filter(
        customer=customer,
        occurred_at__gte=cutoff,
    )

    points = min(
        SUSPENSION_SCORE,
        int(events.aggregate(total=Sum("points"))["total"] or 0),
    )
    cancellation_count = events.filter(points__gt=0).count()

    eligible_booking_count = customer.booking.filter(
        created_at__gte=cutoff,
        status__in=TERMINAL_BOOKING_STATUSES,
    ).count()

    weighted_fraction = (
        events.aggregate(total=Sum("points"))["total"] or 0
    ) / Decimal("5")
    weighted_rate = Decimal("0")
    if eligible_booking_count:
        weighted_rate = (
            Decimal(str(weighted_fraction))
            / Decimal(eligible_booking_count)
            * Decimal("100")
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        weighted_rate = min(weighted_rate, Decimal("100.00"))

    profile, _ = CustomerRiskProfile.objects.get_or_create(customer=customer)
    profile.cancellation_points = points
    profile.cancellation_count = cancellation_count
    profile.eligible_booking_count = eligible_booking_count
    profile.weighted_cancellation_rate = weighted_rate
    profile.risk_level = _risk_level(points)
    profile.last_evaluated_at = timezone.now()
    profile.save(
        update_fields=[
            "cancellation_points",
            "cancellation_count",
            "eligible_booking_count",
            "weighted_cancellation_rate",
            "risk_level",
            "last_evaluated_at",
            "updated_at",
        ]
    )
    return profile


def _network_confidence(*, observation_count, shared_customer_count):
    confidence = Decimal("1.00")
    if observation_count >= 2:
        confidence += Decimal("0.10")
    if shared_customer_count > 1:
        confidence -= Decimal("0.15") * Decimal(shared_customer_count - 1)
    return max(Decimal("0.50"), min(Decimal("0.99"), confidence))


def _apply_network_blocks(customer, *, reason, suspension_score):
    cutoff = timezone.now() - timedelta(days=getattr(settings, "NETWORK_OBSERVATION_DAYS", 30))
    observations = CustomerNetworkObservation.objects.filter(
        customer=customer,
        last_seen_at__gte=cutoff,
    )

    for observation in observations:
        shared_customer_count = (
            CustomerNetworkObservation.objects.filter(
                ip_hash=observation.ip_hash,
                last_seen_at__gte=cutoff,
            )
            .values("customer_id")
            .distinct()
            .count()
        )

        # IP blocks are a defense-in-depth control. Automatically blocking an IP
        # that is demonstrably shared by multiple customers can punish households,
        # offices, campuses, or carrier NATs, so only a single-customer IP identity
        # is eligible for automatic restriction. A higher shared-customer count is
        # retained as a signal but never becomes a standalone network ban.
        if shared_customer_count != 1:
            continue
        if shared_customer_count > NETWORK_BLOCK_SHARED_CUSTOMER_LIMIT:
            continue

        if suspension_score < SUSPENSION_SCORE:
            continue
        if observation.observation_count < 2:
            continue

        prior_block = (
            CustomerNetworkBlock.objects
            .select_for_update(of=("self",))
            .filter(ip_hash=observation.ip_hash)
            .first()
        )
        prior_blocks = prior_block.block_count if prior_block else 0

        if prior_blocks >= NETWORK_MULTI_ACCOUNT_ESCALATION:
            duration = timedelta(days=NETWORK_BLOCK_ESCALATED_DAYS)
        else:
            duration = timedelta(hours=NETWORK_BLOCK_FIRST_HOURS)

        confidence = _network_confidence(
            observation_count=observation.observation_count,
            shared_customer_count=shared_customer_count,
        )
        expires = timezone.now() + duration

        if prior_block is None:
            CustomerNetworkBlock.objects.create(
                ip_hash=observation.ip_hash,
                scope="AUTH_BOOKING",
                reason=reason,
                confidence=confidence,
                block_count=1,
                blocked_until=expires,
            )
        else:
            prior_block.scope = "AUTH_BOOKING"
            prior_block.reason = reason
            prior_block.confidence = confidence
            prior_block.block_count = prior_blocks + 1
            prior_block.blocked_until = expires
            prior_block.save(
                update_fields=[
                    "scope", "reason", "confidence", "block_count",
                    "blocked_until", "updated_at",
                ]
            )


def evaluate_customer_after_cancellation(customer, *, suspension_reason):
    with transaction.atomic():
        profile = (
            CustomerRiskProfile.objects
            .select_for_update(of=("self",))
            .get_or_create(customer=customer)[0]
        )
        previous_score = profile.cancellation_points
        profile = _compute_profile(customer)

        suspended = profile.cancellation_points >= SUSPENSION_SCORE
        if profile.cancellation_points in {2, REVIEW_SCORE} and profile.cancellation_points > previous_score:
            notify_customer_cancellation_risk_warning(
                customer.user,
                profile.cancellation_points,
                SUSPENSION_SCORE,
            )

        if suspended:
            user = customer.user

            if user.is_staff or user.is_superuser:
                # Privileged accounts require manual review rather than an automated lock.
                profile.risk_level = "REVIEW"
                profile.suspension_reason = "Customer cancellation threshold reached; privileged account requires manual review."
                profile.save(update_fields=["risk_level", "suspension_reason", "updated_at"])
                return profile, False

            profile.suspended_at = profile.suspended_at or timezone.now()
            profile.suspension_reason = suspension_reason
            profile.risk_level = "SUSPENDED"
            profile.save(update_fields=["suspended_at", "suspension_reason", "risk_level", "updated_at"])

            if user.security_status != "LOCKED" or user.is_active:
                suspend_user(user)

            _apply_network_blocks(
                customer,
                reason=suspension_reason,
                suspension_score=profile.cancellation_points,
            )

            notify_account_suspended(user, suspension_reason)

        return profile, suspended


def record_customer_cancellation(*, booking, reason_code, reason_text, request, previous_status=None):
    customer = booking.customer
    cutoff = timezone.now() - timedelta(days=ROLLING_WINDOW_DAYS)
    recent = CustomerCancellationEvent.objects.filter(
        customer=customer,
        occurred_at__gte=cutoff,
        points__gt=0,
    ).count()

    policy = REASON_POLICIES.get(reason_code, REASON_POLICIES["OTHER"])
    points = calculate_cancellation_points(
        booking=booking,
        reason_code=reason_code,
        recent_customer_cancellations=recent,
        previous_status=previous_status,
    )
    requires_review = (
        not policy.customer_attributable
        and CustomerCancellationEvent.objects.filter(
            customer=customer,
            occurred_at__gte=timezone.now() - timedelta(days=30),
            points=0,
        ).count() >= 2
    )

    ip_hash = record_customer_network_observation(
        customer,
        request,
        observation_type="CANCELLATION",
    ) or ""

    CustomerCancellationEvent.objects.create(
        customer=customer,
        booking=booking,
        reason_code=reason_code,
        reason_text=reason_text,
        severity=points,
        points=points,
        customer_attributable=policy.customer_attributable,
        requires_review=requires_review,
        ip_hash=ip_hash,
        scheduled_time_start=booking.scheduled_time_start,
    )

    return evaluate_customer_after_cancellation(
        customer,
        suspension_reason=(
            "Cancellation risk score reached 5/5 within the rolling "
            f"{ROLLING_WINDOW_DAYS}-day policy window."
        ),
    )


def get_customer_risk_profile(customer):
    return _compute_profile(customer)
