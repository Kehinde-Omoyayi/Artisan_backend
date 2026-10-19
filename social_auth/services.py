"""Social identity persistence, OTP handoff, and provider sign-in services."""

import logging
import secrets

from django.contrib.auth import get_user_model
from django.contrib.auth.models import update_last_login
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone

from home.auth_tokens import issue_marketplace_tokens
from home.generic import is_user_locked
from home.models import ArtisanProfile, CustomerProfile
from home.otp_security import hash_otp_code, otp_cache_key
from home.password_policy import is_staff_account
from home.security_mail import encrypt_email_code
from home.staff_mfa import begin_staff_mfa_challenge, mask_staff_email, staff_identity_payload
from home.tasks import send_verification_email
from risk.network import record_customer_network_observation

from .exceptions import SocialAuthError
from .models import SocialIdentity

logger = logging.getLogger(__name__)
User = get_user_model()
OTP_TTL_SECONDS = 300
PENDING_PROFILE_TTL_SECONDS = 600


def _pending_key(token):
    import hashlib

    return "social_auth:pending_profile:" + hashlib.sha256(token.encode("utf-8")).hexdigest()


def role_for_user(user):
    # Artisan remains the default workspace for dual-role accounts. Marketplace
    # service conversations resolve the acting role from their own participants.
    if hasattr(user, "artisan_profile"):
        return "artisan"
    if hasattr(user, "customer_profile"):
        return "customer"
    return "staff"


def _identity_data(provider, identity, role, first_name="", last_name=""):
    if provider not in ("google", "apple") or identity.get("provider") != provider:
        raise SocialAuthError("The identity provider response could not be verified.")
    if identity.get("email_verified") is not True:
        raise SocialAuthError("The identity provider did not verify this email address.")
    if role not in ("customer", "artisan"):
        raise SocialAuthError("Choose customer or artisan before creating an account.", fields={"role": ["Select a role first."]})
    email = str(identity.get("email") or "").strip().lower()
    subject = str(identity.get("subject") or "").strip()
    if not email or not subject:
        raise SocialAuthError("The identity provider did not return the required account details.")
    return {
        "provider": provider,
        "subject": subject,
        "email": email,
        "role": role,
        "first_name": (str(identity.get("first_name") or "").strip() or str(first_name or "").strip())[:40],
        "last_name": (str(identity.get("last_name") or "").strip() or str(last_name or "").strip())[:40],
    }


def _check_registration_conflict(provider, identity):
    linked = SocialIdentity.objects.select_related("user").filter(
        provider=provider,
        subject=identity["subject"],
    ).first()
    if linked:
        if not linked.user.is_active and linked.user.security_status != "LOCKED":
            return {
                "status": 202,
                "payload": {
                    "message": "This provider account already has a registration awaiting email verification.",
                    "action": "redirect_to_otp_verification",
                    "email": linked.user.email,
                    "redirect_to": "/api/v1/verify-otp/",
                },
            }
        raise SocialAuthError(
            "This provider account is already registered. Sign in with that provider instead.",
            status_code=409,
            action="social_login",
        )

    if User.objects.filter(email__iexact=identity["email"]).exists():
        raise SocialAuthError(
            "An account with this email already exists. Use that account’s existing sign-in method; provider linking is not available here.",
            status_code=409,
            action="account_exists",
        )
    return None


def pending_profile_details(registration_token):
    if not isinstance(registration_token, str) or not registration_token:
        return None
    pending = cache.get(_pending_key(registration_token))
    if not isinstance(pending, dict):
        return None
    return {
        "email": pending.get("email", ""),
        "first_name": pending.get("first_name", ""),
        "last_name": pending.get("last_name", ""),
    }


def _store_pending_profile(identity):
    for _attempt in range(3):
        token = secrets.token_urlsafe(32)
        if cache.add(_pending_key(token), identity, timeout=PENDING_PROFILE_TTL_SECONDS):
            return token
    raise SocialAuthError("Please restart social registration and try again.", status_code=503)


def begin_social_registration(*, provider, identity, role, first_name="", last_name=""):
    """Validate profile completeness, then create a pending OTP account or a short-lived name setup."""
    normalized = _identity_data(provider, identity, role, first_name, last_name)
    conflict = _check_registration_conflict(provider, normalized)
    if conflict:
        return conflict

    missing = [name for name in ("first_name", "last_name") if not normalized[name]]
    if missing:
        token = _store_pending_profile(normalized)
        return {
            "status": 202,
            "payload": {
                "message": "Your provider identity is verified. Add the missing name details to finish registration.",
                "action": "collect_profile_details",
                "email": normalized["email"],
                "first_name": normalized["first_name"],
                "last_name": normalized["last_name"],
                "registration_token": token,
                "fields_required": missing,
            },
        }
    return {"status": 201, "payload": create_pending_account(normalized)}


def complete_pending_profile(*, registration_token, first_name="", last_name=""):
    if not isinstance(registration_token, str) or not registration_token or len(registration_token) > 256:
        raise SocialAuthError("This profile setup has expired. Start social registration again.")
    key = _pending_key(registration_token)
    lock_key = f"{key}:processing"
    if not cache.add(lock_key, True, timeout=60):
        raise SocialAuthError("This profile setup is already being completed.")
    try:
        pending = cache.get(key)
        if not isinstance(pending, dict):
            raise SocialAuthError("This profile setup has expired. Start social registration again.")
        normalized = dict(pending)
        normalized["first_name"] = (str(first_name or "").strip() or normalized.get("first_name", ""))[:40]
        normalized["last_name"] = (str(last_name or "").strip() or normalized.get("last_name", ""))[:40]
        missing = [name for name in ("first_name", "last_name") if not normalized[name]]
        if missing:
            raise SocialAuthError(
                "Enter your first and last name to finish registration.",
                fields={name: ["This field is required."] for name in missing},
            )
        conflict = _check_registration_conflict(normalized["provider"], normalized)
        if conflict:
            raise SocialAuthError(
                conflict["payload"]["message"],
                status_code=conflict["status"],
                action=conflict["payload"].get("action"),
            )
        payload = create_pending_account(normalized)
        cache.delete(key)
        return payload
    finally:
        cache.delete(lock_key)


def create_pending_account(identity):
    """Create the same inactive user/profile and email-OTP state as normal registration."""
    email = identity["email"].strip().lower()
    first_name = identity["first_name"].strip()[:40]
    last_name = identity["last_name"].strip()[:40]
    role = identity["role"]
    if not first_name or not last_name:
        raise SocialAuthError("Enter your first and last name to finish registration.")

    try:
        with transaction.atomic():
            if User.objects.filter(email__iexact=email).exists():
                raise SocialAuthError(
                    "An account with this email already exists. Use that account’s existing sign-in method; provider linking is not available here.",
                    status_code=409,
                    action="account_exists",
                )
            user = User.objects.create_user(
                email=email,
                first_name=first_name,
                last_name=last_name,
                password=None,
                is_active=False,
            )
            if role == "customer":
                CustomerProfile.objects.create(user=user)
            elif role == "artisan":
                ArtisanProfile.objects.create(user=user)
                CustomerProfile.objects.create(user=user)
            else:
                raise SocialAuthError("Choose customer or artisan before creating an account.")
            SocialIdentity.objects.create(
                user=user,
                provider=identity["provider"],
                subject=identity["subject"],
                email=email,
            )
    except SocialAuthError:
        raise
    except IntegrityError as exc:
        raise SocialAuthError(
            "An account with this email or provider identity already exists. Sign in instead.",
            status_code=409,
            action="account_exists",
        ) from exc

    otp_code = f"{secrets.randbelow(900000) + 100000:06d}"
    cache.set(
        otp_cache_key("verification", email),
        hash_otp_code("verification", email, otp_code),
        timeout=OTP_TTL_SECONDS,
    )
    try:
        send_verification_email.delay(user.first_name, user.email, encrypt_email_code(otp_code))
    except Exception:
        # Keep account-creation semantics consistent with RegisterView when the email queue is unavailable.
        logger.exception("social_registration_email_enqueue_failed")

    return {
        "message": f"Dear esteem {user.first_name}! Your account has been successfully registered.",
        "action": "redirect_to_otp_verification",
        "email": user.email,
        "redirect_to": "/api/v1/verify-otp/",
    }


def social_login(*, provider, identity, request=None):
    if (
        provider not in ("google", "apple")
        or identity.get("provider") != provider
        or identity.get("email_verified") is not True
    ):
        raise SocialAuthError("The identity provider response could not be verified.")
    social_identity = SocialIdentity.objects.select_related("user").filter(
        provider=provider,
        subject=identity.get("subject", ""),
    ).first()
    if social_identity is None:
        raise SocialAuthError(
            "No account is linked to this provider. Create an account first.",
            status_code=404,
            action="social_register",
        )
    user = social_identity.user
    if is_user_locked(user):
        raise SocialAuthError(
            "Your account is currently locked. Please contact support.",
            status_code=403,
            action="contact_support",
        )
    if not user.is_active:
        raise SocialAuthError(
            "Your account has not been verified. Enter the email verification code first.",
            status_code=403,
            action="redirect_to_otp_verification",
            fields={"email": user.email},
        )

    if is_staff_account(user):
        challenge, resend_after, _sent = begin_staff_mfa_challenge(
            user=user,
            request=request,
            method="social",
        )
        payload = {
            "mfa_required": True,
            "mfa_challenge_id": str(challenge.pk),
            "masked_email": mask_staff_email(user.email),
            "expires_in": max(0, int((challenge.expires_at - timezone.now()).total_seconds())),
            "resend_after": resend_after,
            "user": staff_identity_payload(user),
            "message": "Enter the one-time code sent to your registered staff email to finish signing in.",
        }
        return payload, user, "staff"

    role = role_for_user(user)
    if request is not None and role == "customer":
        profile = getattr(user, "customer_profile", None)
        if profile is not None:
            record_customer_network_observation(profile, request, observation_type="LOGIN")
    update_last_login(None, user)
    tokens = issue_marketplace_tokens(user)
    payload = {
        "message": f"Distinguished {user.first_name}, Account Login successful, see you at the other side",
        "access": tokens["access"],
        "refresh": tokens["refresh"],
        "user": {
            "id": str(user.pk),
            "first_name": user.first_name,
            "last_name": user.last_name,
            "email": user.email,
            "is_staff": False,
            "is_superuser": False,
            "role": role,
            "session_version": tokens["session_version"],
        },
        "redirect_to": "/dashboard/",
    }
    return payload, user, role
