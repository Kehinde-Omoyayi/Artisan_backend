from rest_framework.exceptions import ValidationError
from .models import SupportCase

TRANSITIONS = {
    SupportCase.Status.OPEN: {SupportCase.Status.UNDER_REVIEW, SupportCase.Status.RESOLVED, SupportCase.Status.CLOSED},
    SupportCase.Status.UNDER_REVIEW: {SupportCase.Status.RESOLVED, SupportCase.Status.CLOSED},
    SupportCase.Status.RESOLVED: {SupportCase.Status.CLOSED},
    SupportCase.Status.CLOSED: set(),
}

def require_transition(current, target):
    if target not in TRANSITIONS.get(current, set()): raise ValidationError({"status": f"Invalid support case transition: {current} → {target}."})
    return target
