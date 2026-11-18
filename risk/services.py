from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from notifications.events import (
    notify_customer_payment_cancellation_risk,
    notify_risk_adjustment_outcome,
    notify_risk_review_required,
)

from .models import (
    CustomerCancellationEvent,
    CustomerNetworkBlock,
    CustomerNetworkObservation,
    CustomerPaymentCancellationEvent,
    CustomerRiskAdjustment,
    CustomerRiskProfile,
)
from .network import record_customer_network_observation
from .policy import (
    NETWORK_BLOCK_ESCALATED_DAYS,
    NETWORK_BLOCK_FIRST_HOURS,
    NETWORK_OBSERVATION_DAYS,
    NETWORK_BLOCK_SHARED_CUSTOMER_LIMIT,
    NETWORK_MULTI_ACCOUNT_ESCALATION,
    PAYMENT_CANCELLATION_POINTS,
    REASON_POLICIES,
    REVIEW_SCORE,
    ROLLING_WINDOW_DAYS,
    SUSPENSION_SCORE,
)


# Keep the window explicit in one place so the metric is reproducible.
TERMINAL_BOOKING_STATUSES = ("COMPLETED", "CANCELLED", "REJECTED", "DISPUTED")


def _risk_level(score):
    """Risk levels are advisory only.

    Reaching the top of the scale raises a review flag and notifies support; it
    never suspends, locks or otherwise blocks the account.
    """
    if score >= SUSPENSION_SCORE:
        return "REVIEW"
    if score >= REVIEW_SCORE:
        return "REVIEW"
    if score >= 2:
        return "WARNING"
    return "NORMAL"


def effective_points(event_points, manual_adjustment_points):
    """Points shown to the customer: events plus any support correction."""
    return max(0, min(SUSPENSION_SCORE, int(event_points) + int(manual_adjustment_points)))


def risk_percentage(points):
    """0/5 = 0%, 1/5 = 20% … 5/5 = 100%."""
    return int(round(points * 100 / SUSPENSION_SCORE))


def _compute_profile(customer):
    cutoff = timezone.now() - timedelta(days=ROLLING_WINDOW_DAYS)
    events = CustomerPaymentCancellationEvent.objects.filter(
        customer=customer,
        occurred_at__gte=cutoff,
    )

    event_points = min(
        SUSPENSION_SCORE,
        int(events.aggregate(total=Sum("points"))["total"] or 0),
    )
    cancellation_count = events.count()

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
    points = effective_points(event_points, profile.manual_adjustment_points)
    profile.event_points = event_points
    profile.cancellation_points = points
    profile.cancellation_count = cancellation_count
    profile.eligible_booking_count = eligible_booking_count
    profile.weighted_cancellation_rate = weighted_rate
    profile.risk_level = _risk_level(points)
    profile.last_evaluated_at = timezone.now()
    profile.save(
        update_fields=[
            "event_points",
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


def record_customer_payment_cancellation(
    *,
    payment,
    reason_code="",
    reason_text="",
    initiated_by=CustomerPaymentCancellationEvent.Initiator.CUSTOMER,
    points=None,
):
    """Score one qualifying customer-initiated payment cancellation.

    Exactly one point is added per payment transaction, and only when the
    cancellation is genuinely customer-initiated: the caller records the event
    *after* the payment row has been moved to CANCELLED inside the same
    transaction, so a cancellation that fails to be recorded never scores.

    The OneToOne on the payment makes the write idempotent, so retries,
    refreshes, webhook replays and duplicate background tasks are free.
    """
    user = getattr(payment, "customer", None)
    customer = getattr(user, "customer_profile", None)
    if customer is None:
        from home.models import CustomerProfile

        customer = (
            CustomerProfile.objects.filter(user=user).first() if user is not None else None
        )
    if customer is None:
        return None, False
    with transaction.atomic():
        profile = (
            CustomerRiskProfile.objects.select_for_update(of=("self",))
            .get_or_create(customer=customer)[0]
        )
        # Only a customer-initiated cancellation of a payment scores. Provider
        # failures, expiries, administrator actions and system cleanups are
        # recorded for history with zero points.
        awarded = (
            int(points)
            if points is not None
            else (
                PAYMENT_CANCELLATION_POINTS
                if initiated_by == CustomerPaymentCancellationEvent.Initiator.CUSTOMER
                else 0
            )
        )
        event, created = CustomerPaymentCancellationEvent.objects.get_or_create(
            payment=payment,
            defaults={
                "customer": customer,
                "booking": payment.booking,
                "payment_reference": payment.reference,
                "reason_code": reason_code or "",
                "reason_text": reason_text or "",
                "points": max(0, min(SUSPENSION_SCORE, awarded)),
                "initiated_by": initiated_by,
            },
        )
        profile = _compute_profile(customer)
        if not created:
            return profile, False

        if event.initiated_by == CustomerPaymentCancellationEvent.Initiator.CUSTOMER:
            notify_customer_payment_cancellation_risk(
                customer.user,
                profile.cancellation_points,
                SUSPENSION_SCORE,
                payment_reference=payment.reference,
            )
        if profile.cancellation_points >= SUSPENSION_SCORE:
            # Review, never an automatic block: support decides what happens.
            notify_risk_review_required(
                customer.user, profile.cancellation_points, SUSPENSION_SCORE
            )
        return profile, True


def adjust_customer_risk_points(
    *,
    customer,
    actor,
    action,
    reason,
    points=None,
    support_case=None,
    payment_event=None,
    notify_customer=True,
):
    """Apply a manual, audited correction to a customer's risk score.

    Support can add, remove, set or reset points after an appeal. Only the
    *adjustment* is written: the original payment cancellation history is never
    modified or deleted, and the effective score is recomputed from
    events + this adjustment, so the correction survives every future
    recalculation.
    """
    reason = (reason or "").strip()
    if len(reason) < 5:
        raise ValueError("A reason of at least five characters is required.")
    action = str(action).upper()
    if action not in set(CustomerRiskAdjustment.Action.values):
        raise ValueError("Choose a valid adjustment action.")

    with transaction.atomic():
        profile = (
            CustomerRiskProfile.objects.select_for_update(of=("self",))
            .get_or_create(customer=customer)[0]
        )
        profile = _compute_profile(customer)
        previous_points = profile.cancellation_points

        if action == CustomerRiskAdjustment.Action.ADD:
            manual = profile.manual_adjustment_points + 1
        elif action == CustomerRiskAdjustment.Action.REMOVE:
            manual = profile.manual_adjustment_points - 1
        elif action == CustomerRiskAdjustment.Action.RESET:
            # Wipe the whole score by cancelling out the event points too.
            manual = -int(profile.event_points)
        else:  # SET
            if points is None:
                raise ValueError("A target score is required for a SET adjustment.")
            target = max(0, min(SUSPENSION_SCORE, int(points)))
            manual = target - int(profile.event_points)

        profile.manual_adjustment_points = manual
        profile.save(update_fields=["manual_adjustment_points", "updated_at"])
        profile = _compute_profile(customer)
        new_points = profile.cancellation_points
        delta = new_points - previous_points

        adjustment = CustomerRiskAdjustment.objects.create(
            customer=customer,
            action=action,
            previous_points=previous_points,
            new_points=new_points,
            delta=delta,
            reason=reason,
            actor=actor,
            support_case=support_case,
            payment_event=payment_event,
            notify_customer=bool(notify_customer),
        )
        if notify_customer:
            notify_risk_adjustment_outcome(
                customer.user,
                previous_points=previous_points,
                new_points=new_points,
                reason=reason,
            )
        return profile, adjustment


def record_booking_cancellation_history(
    *, booking, reason_code, reason_text, request=None
):
    """Keep why a booking was cancelled, without ever scoring it.

    Support needs the customer's stated reason when they investigate a dispute.
    A booking cancellation is not a payment cancellation, so it is history only:
    points stay at zero and the account is never restricted because of it.
    """
    customer = booking.customer
    CustomerCancellationEvent.objects.update_or_create(
        booking=booking,
        defaults={
            "customer": customer,
            "reason_code": reason_code,
            "reason_text": reason_text,
            "severity": 0,
            "points": 0,
            "customer_attributable": False,
            "requires_review": False,
            "ip_hash": "",
            "scheduled_time_start": booking.scheduled_time_start,
        },
    )
    return _compute_profile(customer)


# Historical name: booking cancellations still flow through here, but they are
# recorded as history and never scored.
record_customer_cancellation = record_booking_cancellation_history


def get_customer_risk_profile(customer):
    return _compute_profile(customer)
