"""Manager identity, second factor and step-up verification.

Sign-in MFA already exists for staff (``home.staff_mfa``: single-use, expiring,
rate limited email codes enforced before a token is issued). This module adds:

* the manager gate - a signed-in staff user must be an active manager,
* authenticator-app (RFC 6238 TOTP) enrol/verify, with replay protection,
* step-up grants: short lived, single use and scoped to one sensitive operation,
* a session registry so staff can see and revoke their own devices.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from core.services import create_audit_log
from manager.models import (
    ManagerLoginThrottle,
    ManagerOTPChallenge,
    ManagerProfile,
    ManagerRole,
    ManagerSession,
    StepUpGrant,
)
from manager.permissions import PERMISSIONS, SUPER_ADMIN, SYSTEM_ROLES

User = get_user_model()

TOTP_STEP_SECONDS = 30
TOTP_DIGITS = 6
TOTP_WINDOW = 1
STEP_UP_TTL = timedelta(minutes=10)
STEP_UP_MAX_ATTEMPTS = 5
LOGIN_LOCK_THRESHOLD = 5
LOGIN_LOCK_MINUTES = 15


class ManagerAccessError(PermissionDenied):
    default_detail = "Manager console access is restricted."


# --------------------------------------------------------------------------- #
# Roles
# --------------------------------------------------------------------------- #
@transaction.atomic
def ensure_system_roles():
    created = {}
    for slug, spec in SYSTEM_ROLES.items():
        role, _ = ManagerRole.objects.get_or_create(
            slug=slug,
            defaults={
                "name": spec["name"],
                "description": spec["description"],
                "is_system": True,
                "permissions": spec["permissions"],
            },
        )
        created[slug] = role
    return created


def ensure_profile(user) -> ManagerProfile | None:
    """Give a staff account its manager profile.

    A Django superuser is provisioned as Super Administrator on first use - this is
    what keeps the existing superuser console working. A customer or artisan gets
    nothing, however they reach the URL.
    """
    if not user or not user.is_authenticated or not user.is_active:
        return None
    profile = ManagerProfile.objects.select_related("role").filter(user=user).first()
    if profile:
        return profile
    if not (user.is_superuser or user.is_staff):
        return None
    roles = ensure_system_roles()
    role_slug = SUPER_ADMIN if user.is_superuser else "support-agent"
    permissions = sorted(PERMISSIONS) if user.is_superuser else list(SYSTEM_ROLES[role_slug]["permissions"])
    profile = ManagerProfile.objects.create(user=user, role=roles[role_slug])
    if role_slug == SUPER_ADMIN and permission_missing(roles[role_slug], sorted(PERMISSIONS)):
        roles[role_slug].permissions = permissions
        roles[role_slug].save(update_fields=["permissions", "updated_at"])
    return profile


def permission_missing(role, expected):
    return sorted(set(expected) - set(role.permissions or [])) != []


def effective_permissions(user) -> set[str]:
    profile = ensure_profile(user)
    if not profile or profile.is_suspended or not user.is_active:
        return set()
    return {code for code in (profile.role.permissions or []) if code in PERMISSIONS}


def has_perm(user, code: str) -> bool:
    if not code:
        return False
    return code in effective_permissions(user)


def require_perm(user, code: str):
    if not has_perm(user, code):
        raise ManagerAccessError(f"Your role does not include '{code}'.")


def is_manager(user) -> bool:
    profile = ensure_profile(user)
    return bool(profile and not profile.is_suspended and user.is_active)


# --------------------------------------------------------------------------- #
# TOTP
# --------------------------------------------------------------------------- #
class SecondFactorUnavailable(Exception):
    """The stored authenticator secret cannot be decrypted.

    Happens when the encryption key was rotated or the row was written by another
    environment. It must never become a 500: the manager has to be told to have
    the secret reset rather than left staring at an error page.
    """

def generate_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _totp(secret: str, step: int) -> str:
    padded = secret + "=" * ((8 - len(secret) % 8) % 8)
    key = base64.b32decode(padded, casefold=True)
    counter = struct.pack(">Q", step)
    digest = hmac.new(key, counter, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10 ** TOTP_DIGITS)).zfill(TOTP_DIGITS)


def current_totp(secret: str, *, at: float | None = None) -> str:
    step = int((at if at is not None else time.time()) // TOTP_STEP_SECONDS)
    return _totp(secret, step)


def provisioning_uri(secret: str, email: str) -> str:
    from urllib.parse import quote

    label = quote(f"Artisan Market:{email}", safe="")
    return f"otpauth://totp/{label}?secret={secret}&issuer=Artisan%20Market&digits={TOTP_DIGITS}&period={TOTP_STEP_SECONDS}"


def begin_totp_enrollment(user) -> dict:
    profile = ensure_profile(user)
    if not profile:
        raise ManagerAccessError()
    secret = generate_totp_secret()
    from services.kyc_crypto import encrypt_value

    profile.totp_secret_encrypted = encrypt_value(secret)
    profile.totp_confirmed_at = None
    profile.save(update_fields=["totp_secret_encrypted", "totp_confirmed_at", "updated_at"])
    return {"secret": secret, "otpauth_uri": provisioning_uri(secret, user.email)}


def confirm_totp_enrollment(user, code: str) -> bool:
    profile = ensure_profile(user)
    if not profile or not profile.totp_secret_encrypted:
        raise ManagerAccessError("Start authenticator setup first.")
    from services.kyc_crypto import decrypt_value

    try:
        secret = decrypt_value(profile.totp_secret_encrypted)
    except ValueError as exc:
        raise SecondFactorUnavailable() from exc
    if verify_totp_code(secret, code):
        profile.totp_confirmed_at = timezone.now()
        profile.totp_last_used_step = int(time.time() // TOTP_STEP_SECONDS)
        profile.save(update_fields=["totp_confirmed_at", "totp_last_used_step", "updated_at"])
        create_audit_log(action="MANAGER_TOTP_ENROLLED", resource_type="MANAGER_PROFILE",
                         resource_id=str(profile.id), actor=user, metadata={})
        return True
    profile.totp_failure_count += 1
    profile.save(update_fields=["totp_failure_count", "updated_at"])
    return False


def verify_totp_code(secret: str, code: str, *, at: float | None = None) -> bool:
    code = str(code or "").strip()
    if not code.isdigit() or len(code) != TOTP_DIGITS:
        return False
    base = int((at if at is not None else time.time()) // TOTP_STEP_SECONDS)
    for offset in range(-TOTP_WINDOW, TOTP_WINDOW + 1):
        if hmac.compare_digest(_totp(secret, base + offset), code):
            return True
    return False


def verify_manager_totp(user, code: str, *, at: float | None = None) -> bool:
    """TOTP check with replay protection (a code cannot be used twice)."""
    profile = ensure_profile(user)
    if not profile or not profile.totp_enrolled:
        return False
    from services.kyc_crypto import decrypt_value

    try:
        secret = decrypt_value(profile.totp_secret_encrypted)
    except ValueError as exc:
        create_audit_log(action="MANAGER_TOTP_SECRET_UNREADABLE", resource_type="MANAGER_PROFILE",
                         resource_id=str(profile.id), actor=user, metadata={})
        raise SecondFactorUnavailable() from exc
    now_step = int((at if at is not None else time.time()) // TOTP_STEP_SECONDS)
    for offset in range(-TOTP_WINDOW, TOTP_WINDOW + 1):
        step = now_step + offset
        if step <= profile.totp_last_used_step:
            continue
        if str(code).strip().isdigit() and hmac.compare_digest(_totp(secret, step), str(code).strip()):
            profile.totp_last_used_step = step
            profile.totp_failure_count = 0
            profile.save(update_fields=["totp_last_used_step", "totp_failure_count", "updated_at"])
            return True
    profile.totp_failure_count += 1
    profile.save(update_fields=["totp_failure_count", "updated_at"])
    return False


# --------------------------------------------------------------------------- #
# Step-up
# --------------------------------------------------------------------------- #
def _hash_token(token: str) -> str:
    from django.conf import settings

    return hmac.new(str(settings.SECRET_KEY).encode(), f"stepup\0{token}".encode(), hashlib.sha256).hexdigest()


def issue_step_up(user, scope: str, *, ip_hash: str = "") -> dict:
    """Mint a single-use grant after the caller has re-proved password + second factor."""
    token = secrets.token_urlsafe(32)
    grant = StepUpGrant.objects.create(
        user=user,
        scope=scope,
        token_hash=_hash_token(token),
        expires_at=timezone.now() + STEP_UP_TTL,
        ip_hash=ip_hash,
    )
    create_audit_log(action="MANAGER_STEP_UP_ISSUED", resource_type="STEP_UP_GRANT",
                     resource_id=str(grant.id), actor=user, metadata={"scope": scope})
    return {"token": token, "scope": scope, "expires_at": grant.expires_at.isoformat(),
            "expires_in": int(STEP_UP_TTL.total_seconds())}


def consume_step_up(user, scope: str, token: str) -> bool:
    if not token:
        return False
    grant = StepUpGrant.objects.filter(
        user=user, scope=scope, token_hash=_hash_token(token), consumed_at__isnull=True
    ).first()
    if not grant or grant.expires_at <= timezone.now():
        return False
    # Compare-and-set so two concurrent requests cannot spend the same grant.
    updated = StepUpGrant.objects.filter(pk=grant.pk, consumed_at__isnull=True).update(consumed_at=timezone.now())
    if not updated:
        return False
    create_audit_log(action="MANAGER_STEP_UP_CONSUMED", resource_type="STEP_UP_GRANT",
                     resource_id=str(grant.id), actor=user, metadata={"scope": scope})
    return True


def require_step_up(user, scope: str, token: str):
    if not consume_step_up(user, scope, token):
        raise PermissionDenied("Re-verify your password and second factor to continue (step-up token missing, used or expired).")


# --------------------------------------------------------------------------- #
# Sign-in throttling and the session registry
# --------------------------------------------------------------------------- #
def _throttle_key(scope, value):
    from django.conf import settings

    return hmac.new(str(settings.SECRET_KEY).encode(), f"{scope}\0{value}".encode(), hashlib.sha256).hexdigest()


def register_login_failure(*, email, ip):
    now = timezone.now()
    locked = False
    for scope, value in (("ACCOUNT", (email or "").strip().casefold()), ("IP", ip or "")):
        if not value:
            continue
        row, _ = ManagerLoginThrottle.objects.get_or_create(
            scope=scope, key_hash=_throttle_key(scope, value),
            defaults={"first_failure_at": now},
        )
        row.failures += 1
        if row.failures >= LOGIN_LOCK_THRESHOLD:
            row.locked_until = now + timedelta(minutes=LOGIN_LOCK_MINUTES)
            locked = True
        row.save(update_fields=["failures", "locked_until", "updated_at"])
    return locked


def clear_login_failures(*, email, ip):
    keys = [("ACCOUNT", (email or "").strip().casefold()), ("IP", ip or "")]
    for scope, value in keys:
        if value:
            ManagerLoginThrottle.objects.filter(scope=scope, key_hash=_throttle_key(scope, value)).delete()


def login_locked(*, email, ip) -> bool:
    now = timezone.now()
    keys = [("ACCOUNT", (email or "").strip().casefold()), ("IP", ip or "")]
    for scope, value in keys:
        if not value:
            continue
        row = ManagerLoginThrottle.objects.filter(scope=scope, key_hash=_throttle_key(scope, value)).first()
        if row and row.locked_until and row.locked_until > now:
            return True
    return False


def register_session(user, session_key: str, *, ip_hash="", user_agent_hash="") -> ManagerSession:
    digest = hashlib.sha256(str(session_key).encode()).hexdigest()
    now = timezone.now()
    session, _ = ManagerSession.objects.update_or_create(
        session_key_hash=digest,
        defaults={
            "user": user,
            "ip_hash": ip_hash,
            "user_agent_hash": user_agent_hash,
            "last_seen_at": now,
            "mfa_verified_at": now,
        },
    )
    return session


def touch_session(session_key: str):
    digest = hashlib.sha256(str(session_key).encode()).hexdigest()
    ManagerSession.objects.filter(session_key_hash=digest, revoked_at__isnull=True).update(last_seen_at=timezone.now())


def revoke_session(user, session_id, *, reason="Revoked by the account owner"):
    session = ManagerSession.objects.filter(pk=session_id, user=user).first()
    if not session:
        return False
    session.revoked_at = timezone.now()
    session.revoked_reason = reason[:120]
    session.save(update_fields=["revoked_at", "revoked_reason"])
    create_audit_log(action="MANAGER_SESSION_REVOKED", resource_type="MANAGER_SESSION",
                     resource_id=str(session.id), actor=user, metadata={"reason": reason[:120]})
    return True


def active_sessions(user):
    return [
        {
            "id": str(row.id),
            "ip_hash": row.ip_hash[:12],
            "user_agent_hash": row.user_agent_hash[:12],
            "created_at": row.created_at.isoformat(),
            "last_seen_at": row.last_seen_at.isoformat(),
            "mfa_verified_at": row.mfa_verified_at.isoformat(),
        }
        for row in ManagerSession.objects.filter(user=user, revoked_at__isnull=True).order_by("-last_seen_at")[:25]
    ]


# --------------------------------------------------------------------------- #
# Email-code second factor (used when TOTP is not enrolled yet)
# --------------------------------------------------------------------------- #
OTP_TTL = timedelta(minutes=5)
OTP_MAX_ATTEMPTS = 5


def issue_email_challenge(user, purpose: str, *, ip_hash="", code=None) -> ManagerOTPChallenge:
    """Create and deliver a one-time code.

    ``code`` exists so the verification harness can assert the flow end to end;
    the API never passes it, so production codes are always random.
    """
    from django.conf import settings

    code = code or f"{secrets.randbelow(900000) + 100000}"
    digest = hmac.new(
        str(settings.SECRET_KEY).encode(), f"mgr-otp:{purpose}:{user.pk}\0{code}".encode(), hashlib.sha256
    ).hexdigest()
    challenge = ManagerOTPChallenge.objects.create(
        user=user,
        purpose=purpose,
        code_hash=digest,
        expires_at=timezone.now() + OTP_TTL,
        ip_hash=ip_hash,
    )
    _deliver_code(user, code, purpose, challenge)
    return challenge


def _deliver_code(user, code, purpose, challenge=None):
    """Hand the code to the email worker; never log it.

    The task re-reads the challenge it is delivering for, so a code that was
    consumed, invalidated or expired before delivery is dropped instead of mailed.
    Earlier versions passed a made-up challenge id, which meant the task silently
    found nothing and no manager without an authenticator ever received a code.
    """
    import logging

    logger = logging.getLogger(__name__)
    try:
        from home.security_mail import encrypt_email_code
        from manager import tasks

        challenge_id = str(getattr(challenge, "id", "") or "")
        if not challenge_id:
            logger.error("manager_otp_challenge_missing user=%s purpose=%s", user.pk, purpose)
            return
        tasks.send_manager_step_up_code.apply_async(
            args=[challenge_id, encrypt_email_code(code)], queue="manager", retry=False
        )
    except Exception:  # pragma: no cover - delivery is best effort, verification is not
        logger.exception("manager_otp_email_failed user=%s", user.pk)


def verify_email_challenge(user, purpose: str, code: str) -> bool:
    from django.conf import settings

    challenge = ManagerOTPChallenge.objects.filter(
        user=user, purpose=purpose, consumed_at__isnull=True, invalidated_at__isnull=True
    ).order_by("-created_at").first()
    if not challenge:
        return False
    now = timezone.now()
    if challenge.expires_at <= now or challenge.attempts >= OTP_MAX_ATTEMPTS:
        challenge.invalidated_at = now
        challenge.save(update_fields=["invalidated_at"])
        return False
    digest = hmac.new(
        str(settings.SECRET_KEY).encode(), f"mgr-otp:{purpose}:{user.pk}\0{str(code).strip()}".encode(), hashlib.sha256
    ).hexdigest()
    if hmac.compare_digest(digest, challenge.code_hash):
        challenge.consumed_at = now
        challenge.save(update_fields=["consumed_at"])
        return True
    challenge.attempts += 1
    challenge.save(update_fields=["attempts"])
    return False


def manager_bootstrap_payload(user) -> dict:
    profile = ensure_profile(user)
    return {
        "is_manager": bool(profile and not profile.is_suspended and user.is_active),
        "user_id": str(user.pk),
        "name": user.get_full_name() or user.email,
        "email": user.email,
        "role": {"slug": profile.role.slug, "name": profile.role.name} if profile else None,
        "permissions": sorted(effective_permissions(user)),
        "totp_enrolled": bool(profile and profile.totp_enrolled),
        "email_otp_enabled": bool(profile and profile.email_otp_enabled),
        "last_authenticated_at": profile.last_authenticated_at.isoformat() if profile and profile.last_authenticated_at else None,
        "step_up_ttl_seconds": int(STEP_UP_TTL.total_seconds()),
    }


def record_manager_auth(user):
    profile = ensure_profile(user)
    if profile:
        profile.last_authenticated_at = timezone.now()
        profile.save(update_fields=["last_authenticated_at", "updated_at"])
        cache.delete(f"manager:perms:{user.pk}")
