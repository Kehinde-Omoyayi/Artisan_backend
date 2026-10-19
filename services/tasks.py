from celery import shared_task
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from notifications.events import schedule_email
from home.models import CustomUser

from .identity_names import (
    IDENTITY_NAME_UNAVAILABLE_MESSAGE,
    PROFILE_NAME_CORRECTION_MESSAGE,
    extract_provider_identity_name,
    profile_name_matches_identity,
    provider_reports_name_mismatch,
    provider_result_is_pending,
)
from .kyc_crypto import (
    decrypt_bvn,
    decrypt_nin,
    decrypt_otp,
    encrypt_identity_name,
)
from .models import ArtisanVerification
from .mono import MonoVerificationService
from .verification import reject_verification, reset_verification
from .verification_messages import *


NIN_SERVICE_ERROR = (
    "We’re sorry, but we were unable to complete your NIN verification "
    "because our verification service could not process the request at "
    "this time. Please try again later. We apologise for the inconvenience."
)

NIN_RATE_LIMIT = (
    "Your NIN verification could not be completed because the verification "
    "service is temporarily busy processing too many of your requests. Please wait "
    "a little while before trying again. We apologise for the inconvenience."
)

BVN_SERVICE_ERROR = (
    "We’re sorry, but we were unable to complete your BVN verification "
    "because our verification service could not process the request at "
    "this time. Please try again later. We apologise for the inconvenience."
)

BVN_RATE_LIMIT = (
    "Your BVN verification could not be completed because the verification "
    "service is temporarily busy processing too many of your requests. Please wait "
    "a little while before trying again. We apologise for the inconvenience."
)


def _bvn_session_key(verification_id):
    return f"bvn_session:{verification_id}"


def _bvn_state_key(verification_id):
    return f"bvn_state:{verification_id}"


def _bvn_lock_key(verification_id, step):
    return f"bvn_lock:{step}:{verification_id}"


def set_bvn_session(verification_id, session_id, timeout=600):
    cache.set(
        _bvn_session_key(verification_id),
        session_id,
        timeout,
    )


def get_bvn_session(verification_id):
    return cache.get(
        _bvn_session_key(verification_id)
    )


def delete_bvn_session(verification_id):
    cache.delete(
        _bvn_session_key(verification_id)
    )


def set_bvn_state(verification_id, **state):
    cache.set(
        _bvn_state_key(verification_id),
        state,
        timeout=600,
    )


def get_bvn_state(verification_id):
    return (
        cache.get(
            _bvn_state_key(verification_id)
        )
        or {}
    )


def delete_bvn_state(verification_id):
    cache.delete(
        _bvn_state_key(verification_id)
    )


def acquire_bvn_lock(verification_id, step, timeout=120):
    return cache.add(
        _bvn_lock_key(verification_id, step),
        "1",
        timeout,
    )


def release_bvn_lock(verification_id, step):
    cache.delete(
        _bvn_lock_key(verification_id, step)
    )


def _get_bvn_verification(verification_id):
    return (
        ArtisanVerification.objects
        .select_related("artisan__user")
        .filter(
            pk=verification_id,
            verification_status="PENDING",
            verification_method="BVN",
        )
        .first()
    )


def _send_email(
    verification,
    event,
    subject,
    message,
):
    user = verification.artisan.user

    if not user or not user.email:
        return

    schedule_email(
        event_key=f"verification:{verification.id}:{event}",
        recipient=user,
        subject=subject,
        message=message,
    )


def _nin_service_failure(verification, message):
    reset_verification(verification)

    save_verification_result(
        verification.artisan_id,
        {
            "status": "SERVICE_ERROR",
            "title": "NIN verification unavailable",
            "message": message,
            "retryable": True,
        },
    )

    _send_email(
        verification,
        "nin-service-error",
        "Your NIN verification could not be completed",
        message,
    )

    return {
        "status": "SERVICE_ERROR",
        "message": message,
    }


def _bvn_service_failure(
    verification_id,
    message,
    lock_step,
):
    verification = (
        ArtisanVerification.objects
        .select_related("artisan__user")
        .filter(pk=verification_id)
        .first()
    )

    release_bvn_lock(
        verification_id,
        lock_step,
    )

    if not verification:
        return {
            "status": "IGNORED",
            "message": "Verification request no longer exists.",
        }

    reset_verification(verification)
    delete_bvn_session(verification_id)
    delete_bvn_state(verification_id)

    set_bvn_state(
        verification_id,
        status="SERVICE_ERROR",
        message=message,
        next_step="START_BVN_AGAIN",
    )

    _send_email(
        verification,
        "bvn-service-error",
        "Your BVN verification could not be completed",
        message,
    )

    return {
        "status": "SERVICE_ERROR",
        "message": message,
    }


def reject_bvn(
    verification_id,
    message,
    lock_step=None,
):
    with transaction.atomic():
        verification = (
            ArtisanVerification.objects
            .select_for_update(of=("self",))
            .select_related("artisan__user")
            .get(pk=verification_id)
        )

        if verification.verification_status == "PENDING":
            verification.verification_status = "REJECTED"
            verification.verification_reference = ""
            verification.verified_identity_name_encrypted = ""
            verification.verified_at = None

            verification.save(
                update_fields=[
                    "verification_status",
                    "verification_reference",
                    "verified_identity_name_encrypted",
                    "verified_at",
                    "updated_at",
                ]
            )

    if lock_step:
        release_bvn_lock(
            verification_id,
            lock_step,
        )

    delete_bvn_session(
        verification_id
    )

    set_bvn_state(
        verification_id,
        status="REJECTED",
        message=message,
        next_step="START_BVN_AGAIN",
    )

    _send_email(
        verification,
        "bvn-rejected",
        "Your BVN verification could not be completed",
        message,
    )

    return {
        "status": "REJECTED",
        "message": message,
    }


def _retry_bvn(
    self,
    verification_id,
    message,
    lock_step,
    countdown,
):
    release_bvn_lock(
        verification_id,
        lock_step,
    )

    if self.request.retries >= self.max_retries:
        return _bvn_service_failure(
            verification_id,
            message,
            lock_step,
        )

    raise self.retry(
        exc=RuntimeError(message),
        countdown=countdown,
    )


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=30,
)
def verify_nin_async(
    self,
    verification_id,
    encrypted_nin,
):
    verification = (
        ArtisanVerification.objects
        .select_related("artisan__user")
        .filter(
            pk=verification_id,
            verification_status="PENDING",
            verification_method="NIN",
        )
        .first()
    )

    if not verification:
        return {"status": "IGNORED"}

    try:
        nin = decrypt_nin(encrypted_nin)
        result = MonoVerificationService().lookup_nin(nin)

    except ConnectionError:
        if self.request.retries >= self.max_retries:
            return _nin_service_failure(
                verification,
                NIN_SERVICE_ERROR,
            )

        raise self.retry(countdown=30) from None

    except Exception:
        return _nin_service_failure(
            verification,
            NIN_SERVICE_ERROR,
        )

    finally:
        nin = None

    http_status = int(result.get("_http_status") or 500)
    provider_status = str(result.get("status") or "FAILED").lower()

    if http_status == 429:
        if self.request.retries >= self.max_retries:
            return _nin_service_failure(verification, NIN_RATE_LIMIT)
        raise self.retry(exc=RuntimeError(NIN_RATE_LIMIT), countdown=60)

    if http_status >= 500:
        if self.request.retries >= self.max_retries:
            return _nin_service_failure(verification, NIN_SERVICE_ERROR)
        raise self.retry(exc=RuntimeError(NIN_SERVICE_ERROR), countdown=60)

    outcome = classify_nin_result(result)
    if provider_reports_name_mismatch(result):
        outcome = {
            "status": "REJECTED",
            "title": "Profile name needs correction",
            "message": PROFILE_NAME_CORRECTION_MESSAGE,
            "retryable": True,
        }
    elif provider_result_is_pending(result):
        outcome = {
            "status": "PENDING",
            "title": "NIN verification submitted",
            "message": "Your NIN verification is still being processed. No verification tier has been added.",
            "retryable": False,
        }

    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    reference = data.get("tracking_id") or result.get("tracking_id") or ""
    identity_name = extract_provider_identity_name(result, "NIN")

    if provider_status == "successful" and outcome.get("status") == "VERIFIED":
        if not identity_name:
            outcome = {
                "status": "REJECTED",
                "title": "NIN verification could not be completed",
                "message": IDENTITY_NAME_UNAVAILABLE_MESSAGE,
                "retryable": True,
            }

    final_status = "REJECTED"
    final_message = outcome.get("message") or NIN_SERVICE_ERROR
    email_event = "nin-rejected"
    email_subject = outcome.get("title") or "Your NIN verification could not be completed"
    success = False
    pending = False

    with transaction.atomic():
        verification = (
            ArtisanVerification.objects
            .select_for_update(of=("self",))
            .select_related("artisan__user")
            .get(pk=verification_id)
        )

        if verification.verification_status != "PENDING":
            return {"status": "IGNORED"}

        if outcome.get("status") == "PENDING" and provider_result_is_pending(result):
            # A provider-accepted request is not a successful identity verification.
            # Keep the existing PENDING state and tier unchanged.
            pending = True
            final_status = "PENDING"
        elif provider_status != "successful" or outcome.get("status") != "VERIFIED":
            verification.verification_status = "REJECTED"
            verification.verification_reference = reference
            verification.verified_identity_name_encrypted = ""
            verification.verified_at = None
            verification.save(update_fields=[
                "verification_status",
                "verification_reference",
                "verified_identity_name_encrypted",
                "verified_at",
                "updated_at",
            ])
        else:
            user = CustomUser.objects.select_for_update(of=("self",)).get(
                pk=verification.artisan.user_id
            )
            if not profile_name_matches_identity(
                identity_name,
                user.first_name,
                user.last_name,
            ):
                outcome = {
                    "status": "REJECTED",
                    "title": "Profile name needs correction",
                    "message": PROFILE_NAME_CORRECTION_MESSAGE,
                    "retryable": True,
                }
                final_message = outcome["message"]
                email_subject = outcome["title"]
                verification.verification_status = "REJECTED"
                verification.verification_reference = reference
                verification.verified_identity_name_encrypted = ""
                verification.verified_at = None
                verification.save(update_fields=[
                    "verification_status",
                    "verification_reference",
                    "verified_identity_name_encrypted",
                    "verified_at",
                    "updated_at",
                ])
            else:
                verification.verification_status = "VERIFIED"
                verification.verification_tier += 1
                verification.verified_identity_name_encrypted = encrypt_identity_name(identity_name)
                verification.verification_provider = "mono"
                verification.verification_reference = reference
                verification.verified_at = timezone.now()
                verification.save(update_fields=[
                    "verification_status",
                    "verification_tier",
                    "verified_identity_name_encrypted",
                    "verification_provider",
                    "verification_reference",
                    "verified_at",
                    "updated_at",
                ])
                final_status = "VERIFIED"
                final_message = (
                    "Your NIN verification has been completed successfully. "
                    "Your identity has been verified."
                )
                email_event = "nin-approved"
                email_subject = "Your NIN verification was successful"
                success = True

    if pending:
        outcome = {
            "status": "PENDING",
            "title": "NIN verification submitted",
            "message": outcome.get("message") or "Your NIN verification is still being processed. No verification tier has been added.",
            "retryable": False,
        }
        save_verification_result(verification.artisan_id, outcome)
        return {"status": "PENDING", "message": outcome["message"], "reference": reference}

    if not success:
        outcome = {
            **outcome,
            "status": "REJECTED",
            "message": final_message,
        }
        save_verification_result(verification.artisan_id, outcome)
        _send_email(verification, email_event, email_subject, final_message)
        return {"status": "REJECTED", "message": final_message, "reference": reference}

    save_verification_result(verification.artisan_id, {
        "status": "VERIFIED",
        "title": email_subject,
        "message": final_message,
        "retryable": False,
    })
    _send_email(verification, email_event, email_subject, final_message)
    return {"status": final_status, "message": final_message, "reference": reference}


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=30,
)
def initiate_bvn_async(
    self,
    verification_id,
    encrypted_bvn,
):
    verification = _get_bvn_verification(
        verification_id
    )

    if not verification:
        return {"status": "IGNORED"}

    try:
        bvn = decrypt_bvn(encrypted_bvn)
        result = MonoVerificationService().initiate_bvn(bvn)

    except ConnectionError:
        return _retry_bvn(
            self,
            verification_id,
            BVN_SERVICE_ERROR,
            "initiate",
            30,
        )

    except Exception:
        return reject_bvn(
            verification_id,
            (
                "We’re sorry, but we were unable to start "
                "your BVN verification at this time. "
                "Please try again later. "
                "We apologise for the inconvenience."
            ),
            "initiate",
        )

    finally:
        bvn = None

    http_status = int(
        result.get("_http_status") or 500
    )

    provider_status = str(
        result.get("status") or "FAILED"
    ).lower()

    data = result.get("data") or {}
    session_id = data.get("session_id")
    methods = data.get("methods") or []

    if http_status == 429:
        return _retry_bvn(
            self,
            verification_id,
            BVN_RATE_LIMIT,
            "initiate",
            60,
        )

    if http_status >= 500:
        return _retry_bvn(
            self,
            verification_id,
            BVN_SERVICE_ERROR,
            "initiate",
            60,
        )

    if provider_status != "successful":
        return reject_bvn(
            verification_id,
            bvn_initiate_message(result),
            "initiate",
        )

    if not session_id:
        return reject_bvn(
            verification_id,
            (
                "We’re sorry, but we were unable to continue "
                "your BVN verification because the verification "
                "service did not provide the required verification "
                "session. Please try again later. "
                "We apologise for the inconvenience."
            ),
            "initiate",
        )

    email_available = any(
        isinstance(item, dict)
        and item.get("method") == "email"
        for item in methods
    )

    if not email_available:
        return reject_bvn(
            verification_id,
            (
                "Your BVN verification cannot continue because "
                "email verification is not currently available "
                "for this verification request. Please try again "
                "later. We apologise for the inconvenience."
            ),
            "initiate",
        )

    set_bvn_session(
        verification_id,
        session_id,
    )

    set_bvn_state(
        verification_id,
        status="OTP_REQUIRED",
        message=(
            "Your BVN verification request has been received "
            "successfully. Email verification is available. "
            "Please request the verification code to continue."
        ),
        next_step="REQUEST_EMAIL_OTP",
        next_endpoint=(
            "/api/v1/services/verification/bvn/method/"
        ),
    )

    release_bvn_lock(
        verification_id,
        "initiate",
    )

    return {
        "status": "OTP_REQUIRED",
        "verification_id": str(
            verification_id
        ),
    }


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=30,
)
def request_bvn_otp_async(
    self,
    verification_id,
):
    verification = _get_bvn_verification(
        verification_id
    )

    if not verification:
        return {"status": "IGNORED"}

    session_id = get_bvn_session(
        verification_id
    )

    if not session_id:
        return reject_bvn(
            verification_id,
            (
                "Your BVN verification session has expired. "
                "Please start BVN verification again."
            ),
            "method",
        )

    try:
        result = MonoVerificationService().verify_bvn(
            session_id
        )

    except ConnectionError:
        return _retry_bvn(
            self,
            verification_id,
            BVN_SERVICE_ERROR,
            "method",
            30,
        )

    except Exception:
        release_bvn_lock(
            verification_id,
            "method",
        )

        message = (
            "We were unable to send your BVN verification "
            "code at this time. Please try again shortly. "
            "We apologise for the inconvenience."
        )

        set_bvn_state(
            verification_id,
            status="OTP_REQUIRED",
            message=message,
            next_step="REQUEST_EMAIL_OTP",
            next_endpoint=(
                "/api/v1/services/verification/bvn/method/"
            ),
        )

        return {
            "status": "FAILED",
            "message": message,
        }

    http_status = int(
        result.get("_http_status") or 500
    )

    provider_status = str(
        result.get("status") or "FAILED"
    ).lower()

    if http_status == 429:
        return _retry_bvn(
            self,
            verification_id,
            BVN_RATE_LIMIT,
            "method",
            60,
        )

    if http_status >= 500:
        return _retry_bvn(
            self,
            verification_id,
            BVN_SERVICE_ERROR,
            "method",
            60,
        )

    if provider_status != "successful":
        custom_message = bvn_otp_request_message(
            result
        )

        release_bvn_lock(
            verification_id,
            "method",
        )

        set_bvn_state(
            verification_id,
            status="OTP_REQUIRED",
            message=custom_message,
            next_step="REQUEST_EMAIL_OTP",
            next_endpoint=(
                "/api/v1/services/verification/bvn/method/"
            ),
        )

        return {
            "status": "FAILED",
            "message": custom_message,
        }

    custom_message = bvn_otp_request_message(
        result
    )

    set_bvn_state(
        verification_id,
        status="OTP_SENT",
        message=custom_message,
        next_step="SUBMIT_BVN_OTP",
        next_endpoint=(
            "/api/v1/services/verification/bvn/details/"
        ),
    )

    release_bvn_lock(
        verification_id,
        "method",
    )

    return {
        "status": "OTP_SENT",
        "message": custom_message,
    }


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=30,
)
def complete_bvn_async(
    self,
    verification_id,
    encrypted_otp,
):
    verification = _get_bvn_verification(
        verification_id
    )

    if not verification:
        return {"status": "IGNORED"}

    session_id = get_bvn_session(
        verification_id
    )

    if not session_id:
        return reject_bvn(
            verification_id,
            (
                "Your BVN verification session has expired. "
                "Please start BVN verification again."
            ),
            "details",
        )

    try:
        otp = decrypt_otp(encrypted_otp)

        result = MonoVerificationService().fetch_bvn_details(
            session_id,
            otp,
        )

    except ConnectionError:
        return _retry_bvn(
            self,
            verification_id,
            BVN_SERVICE_ERROR,
            "details",
            30,
        )

    except Exception:
        release_bvn_lock(
            verification_id,
            "details",
        )

        message = (
            "We were unable to complete your BVN verification "
            "at this time. Please check the verification code "
            "and try again. We apologise for the inconvenience."
        )

        set_bvn_state(
            verification_id,
            status="OTP_REQUIRED",
            message=message,
            next_step="SUBMIT_BVN_OTP",
            next_endpoint=(
                "/api/v1/services/verification/bvn/details/"
            ),
        )

        return {
            "status": "FAILED",
            "message": message,
        }

    finally:
        otp = None

    http_status = int(
        result.get("_http_status") or 500
    )

    provider_status = str(
        result.get("status") or "FAILED"
    ).lower()

    data = result.get("data") if isinstance(result.get("data"), dict) else {}

    reference = (
        data.get("tracking_id")
        or result.get("tracking_id")
        or ""
    )

    if http_status == 429:
        return _retry_bvn(
            self,
            verification_id,
            BVN_RATE_LIMIT,
            "details",
            60,
        )

    if http_status >= 500:
        return _retry_bvn(
            self,
            verification_id,
            BVN_SERVICE_ERROR,
            "details",
            60,
        )

    if provider_result_is_pending(result):
        pending_message = "Your BVN verification is still being processed. No verification tier has been added."
        release_bvn_lock(verification_id, "details")
        set_bvn_state(
            verification_id,
            status="PENDING",
            message=pending_message,
            reference=reference,
            next_step="CHECK_STATUS",
        )
        return {"status": "PENDING", "message": pending_message, "reference": reference}

    if provider_status != "successful":
        custom_message = bvn_otp_result_message(
            result
        )

        release_bvn_lock(
            verification_id,
            "details",
        )

        set_bvn_state(
            verification_id,
            status="OTP_REQUIRED",
            message=custom_message,
            reference=reference,
            next_step="SUBMIT_BVN_OTP",
            next_endpoint=(
                "/api/v1/services/verification/bvn/details/"
            ),
        )

        return {
            "status": "OTP_REJECTED",
            "message": custom_message,
            "reference": reference,
        }

    identity_name = extract_provider_identity_name(result, "BVN")
    mismatch_reported = provider_reports_name_mismatch(result)
    failure_message = ""
    if mismatch_reported:
        failure_message = PROFILE_NAME_CORRECTION_MESSAGE
    elif not identity_name:
        failure_message = IDENTITY_NAME_UNAVAILABLE_MESSAGE

    success_message = (
        "Your BVN verification has been completed successfully. "
        "Your identity has been verified."
    )
    email_subject = "Your BVN verification was successful"
    email_event = "bvn-approved"
    verified = False

    with transaction.atomic():
        verification = (
            ArtisanVerification.objects
            .select_for_update(of=("self",))
            .select_related("artisan__user")
            .get(pk=verification_id)
        )

        if verification.verification_status != "PENDING":
            return {"status": "IGNORED"}

        if not failure_message:
            user = CustomUser.objects.select_for_update(of=("self",)).get(
                pk=verification.artisan.user_id
            )
            if not profile_name_matches_identity(
                identity_name,
                user.first_name,
                user.last_name,
            ):
                failure_message = PROFILE_NAME_CORRECTION_MESSAGE

        if failure_message:
            verification.verification_status = "REJECTED"
            verification.verification_reference = reference
            verification.verified_identity_name_encrypted = ""
            verification.verified_at = None
            verification.save(update_fields=[
                "verification_status",
                "verification_reference",
                "verified_identity_name_encrypted",
                "verified_at",
                "updated_at",
            ])
            email_subject = "Your BVN verification could not be completed"
            email_event = "bvn-rejected"
        else:
            verification.verification_status = "VERIFIED"
            verification.verification_tier += 1
            verification.verified_identity_name_encrypted = encrypt_identity_name(identity_name)
            verification.verification_provider = "mono"
            verification.verification_reference = reference
            verification.verified_at = timezone.now()
            verification.save(update_fields=[
                "verification_status",
                "verification_tier",
                "verified_identity_name_encrypted",
                "verification_provider",
                "verification_reference",
                "verified_at",
                "updated_at",
            ])
            verified = True

    delete_bvn_session(verification_id)
    if not verified:
        set_bvn_state(
            verification_id,
            status="REJECTED",
            message=failure_message,
            reference=reference,
            next_step="START_BVN_AGAIN",
        )
        release_bvn_lock(verification_id, "details")
        _send_email(verification, email_event, email_subject, failure_message)
        return {
            "status": "REJECTED",
            "message": failure_message,
            "reference": reference,
        }

    set_bvn_state(
        verification_id,
        status="VERIFIED",
        message=success_message,
        reference=reference,
        next_step="VERIFICATION_COMPLETE",
    )
    release_bvn_lock(verification_id, "details")
    _send_email(verification, email_event, email_subject, success_message)
    return {
        "status": "VERIFIED",
        "message": success_message,
        "reference": reference,
    }
