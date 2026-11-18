"""Password-age and staff-recovery state shared by authentication and the admin gate."""
from __future__ import annotations

from datetime import timedelta

from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone


STAFF_PASSWORD_MAX_AGE = timedelta(days=7)


def is_staff_account(user) -> bool:
    return bool(user and (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)))


def staff_password_change_required(user, *, now=None) -> bool:
    """Fail closed for missing timestamps; ordinary customer/artisan accounts are unaffected."""
    if not is_staff_account(user):
        return False
    changed_at = getattr(user, "password_changed_at", None)
    if changed_at is None:
        return True
    now = now or timezone.now()
    return changed_at <= now - STAFF_PASSWORD_MAX_AGE


def staff_security_questions_required(user) -> bool:
    if not is_staff_account(user):
        return False
    try:
        return not bool(user.staff_security_questions)
    except ObjectDoesNotExist:
        return True
