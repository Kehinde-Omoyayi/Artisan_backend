from rest_framework.exceptions import ValidationError

BOOKING_TRANSITIONS = {
    "REQUESTED": {"ACCEPTED", "REJECTED", "CANCELLED", "DISPUTED"},
    "ACCEPTED": {"IN_PROGRESS", "CANCELLED", "DISPUTED"},
    "IN_PROGRESS": {"COMPLETION_REQUESTED", "DISPUTED", "CANCELLED"},
    "COMPLETION_REQUESTED": {"COMPLETED", "DISPUTED"},
    "COMPLETED": set(), "CANCELLED": set(), "REJECTED": set(),
    # Only the support resolution service may move a disputed booking out of this state.
    "DISPUTED": {"ACCEPTED", "IN_PROGRESS", "COMPLETION_REQUESTED", "CANCELLED"},
}

BOOKING_ACTIVE_STATUSES = {
    "REQUESTED",
    "ACCEPTED",
    "IN_PROGRESS",
    "COMPLETION_REQUESTED",
    "DISPUTED",
}

BOOKING_TERMINAL_STATUSES = {
    "COMPLETED",
    "CANCELLED",
    "REJECTED",
}

PAYMENT_TRANSITIONS = {
    "PAYMENT_PENDING": {"HELD", "FAILED", "DISPUTED"},
    "HELD": {"DISPUTED", "RELEASE_PENDING", "REFUND_PENDING"},
    "DISPUTED": {"HELD", "REFUND_PENDING", "RELEASE_PENDING"},
    "RELEASE_PENDING": {"RELEASED", "DISPUTED", "REFUND_PENDING"},
    "RELEASED": set(),
    "REFUND_PENDING": {"REFUNDED", "HELD"},
    "REFUNDED": set(),
    "FAILED": set(),
}

def require_transition(table, current, target, *, resource="state"):
    allowed = table.get(str(current), set())
    if target not in allowed:
        raise ValidationError({
            "state": f"Invalid {resource} transition: {current} → {target}."
        })
    return target