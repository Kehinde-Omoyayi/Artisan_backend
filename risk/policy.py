from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

ROLLING_WINDOW_DAYS = getattr(settings, "CANCELLATION_RISK_WINDOW_DAYS", 90)
SUSPENSION_SCORE = getattr(settings, "CANCELLATION_SUSPENSION_SCORE", 5)
REVIEW_SCORE = getattr(settings, "CANCELLATION_REVIEW_SCORE", 4)
NETWORK_OBSERVATION_DAYS = getattr(settings, "NETWORK_OBSERVATION_DAYS", 30)
NETWORK_BLOCK_FIRST_HOURS = getattr(settings, "NETWORK_BLOCK_FIRST_HOURS", 24)
NETWORK_BLOCK_ESCALATED_DAYS = getattr(settings, "NETWORK_BLOCK_ESCALATED_DAYS", 7)
NETWORK_BLOCK_SHARED_CUSTOMER_LIMIT = getattr(settings, "NETWORK_BLOCK_SHARED_CUSTOMER_LIMIT", 3)
NETWORK_MULTI_ACCOUNT_ESCALATION = getattr(settings, "NETWORK_MULTI_ACCOUNT_ESCALATION", 2)


@dataclass(frozen=True)
class CancellationReasonPolicy:
    code: str
    label: str
    base_points: int
    customer_attributable: bool


REASON_POLICIES = {
    "CUSTOMER_CHANGED_MIND": CancellationReasonPolicy(
        "CUSTOMER_CHANGED_MIND", "Changed plans", 1, True
    ),
    "SCHEDULE_CONFLICT": CancellationReasonPolicy(
        "SCHEDULE_CONFLICT", "Scheduling conflict", 1, True
    ),
    "FOUND_ALTERNATIVE": CancellationReasonPolicy(
        "FOUND_ALTERNATIVE", "Found another artisan", 1, True
    ),
    "PRICE_DISAGREEMENT": CancellationReasonPolicy(
        "PRICE_DISAGREEMENT", "Price or terms disagreement", 1, True
    ),
    "OTHER": CancellationReasonPolicy("OTHER", "Other reason", 1, True),
    "DUPLICATE_BOOKING": CancellationReasonPolicy(
        "DUPLICATE_BOOKING", "Duplicate booking", 0, False
    ),
    "PAYMENT_FAILURE": CancellationReasonPolicy(
        "PAYMENT_FAILURE", "Payment failure", 0, False
    ),
    "ARTISAN_UNRESPONSIVE": CancellationReasonPolicy(
        "ARTISAN_UNRESPONSIVE", "Artisan did not respond", 0, False
    ),
    "ARTISAN_CANCELLED": CancellationReasonPolicy(
        "ARTISAN_CANCELLED", "Artisan asked to cancel", 0, False
    ),
    "ARTISAN_LATE": CancellationReasonPolicy(
        "ARTISAN_LATE", "Artisan was late", 0, False
    ),
    "SERVICE_NOT_AS_DESCRIBED": CancellationReasonPolicy(
        "SERVICE_NOT_AS_DESCRIBED", "Service was not as described", 0, False
    ),
    "SAFETY_CONCERN": CancellationReasonPolicy(
        "SAFETY_CONCERN", "Safety concern", 0, False
    ),
    "EMERGENCY": CancellationReasonPolicy(
        "EMERGENCY", "Emergency", 0, False
    ),
}

CUSTOMER_CANCEL_REASON_CHOICES = [
    (policy.code, policy.label) for policy in REASON_POLICIES.values()
]


def rolling_cutoff(days=ROLLING_WINDOW_DAYS):
    return timezone.now() - timedelta(days=days)


def calculate_cancellation_points(*, booking, reason_code, recent_customer_cancellations, previous_status=None):
    policy = REASON_POLICIES.get(reason_code, REASON_POLICIES["OTHER"])
    if not policy.customer_attributable:
        return 0

    points = policy.base_points
    now = timezone.now()
    start = booking.scheduled_time_start
    hours_until_start = (start - now).total_seconds() / 3600

    if (previous_status or booking.status) == "ACCEPTED":
        points += 1

    if hours_until_start <= 24:
        points += 1
    if hours_until_start <= 6:
        points += 1
    if hours_until_start <= 1:
        points += 1

    if recent_customer_cancellations >= 2:
        points += 1

    # Normal customer-entered reasons cannot independently trigger an automatic
    # 5/5 suspension. Score 5 is reserved for cumulative repeated behavior.
    return min(points, 4)
