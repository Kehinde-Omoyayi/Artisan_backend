"""Staff password age, security-question, and manager-directed recovery endpoints."""
from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password, make_password
from django.core.exceptions import ObjectDoesNotExist, ValidationError as DjangoValidationError
from django.contrib.auth.password_validation import validate_password
from django.db import transaction
from django.utils import timezone

from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from .security_http import NoStoreAPIView

from core.audit import audit_request_action
from home.password_policy import is_staff_account
from home.permissions import IsPlatformSuperuser
from home.security_mail import encrypt_staff_code
from home.security_questions import SECURITY_QUESTION_OPTIONS, normalize_security_answer
from home.tasks import send_staff_security_code
from .models import StaffPasswordResetToken, StaffSecurityChallenge, StaffSecurityProfile
from .security_serializers import (
    StaffPasswordChangeSerializer,
    StaffPasswordResetCompleteSerializer,
    StaffPasswordResetVerifySerializer,
    StaffSecurityQuestionsSerializer,
    StaffSecuritySetupCompleteSerializer,
)

logger = logging.getLogger(__name__)
User = get_user_model()

STAFF_OTP_TTL = timedelta(minutes=5)
STAFF_RESET_TOKEN_TTL = timedelta(minutes=10)
STAFF_CHALLENGE_COOLDOWN = timedelta(seconds=60)
STAFF_RECOVERY_MAX_ATTEMPTS = 5
STAFF_PASSWORD_CHANGE_REQUIRED_MESSAGE = "Your password was changed. Sign in again with the new password."  # noqa: S105  # nosec B105: user-facing message, not a credential
STAFF_SECURITY_SETUP_REQUIRED_MESSAGE = "Register all three security questions before continuing."


def _no_store(response):
    response["Cache-Control"] = "no-store, private"
    response["Pragma"] = "no-cache"
    return response


def _private_error(detail="Invalid or expired staff verification code.", *, status_code=status.HTTP_400_BAD_REQUEST):
    return _no_store(Response({"error": detail}, status=status_code))


def _profile_questions(profile):
    return {
        profile.question_1: profile.answer_hash_1,
        profile.question_2: profile.answer_hash_2,
        profile.question_3: profile.answer_hash_3,
    }


def _answers_match(profile, submitted_answers):
    """Require the complete, exact registered prompt set and every answer."""
    expected = _profile_questions(profile)
    submitted = {item["question_id"]: item["answer"] for item in submitted_answers}
    all_answers_match = True
    for question_id, answer_hash in expected.items():
        candidate = normalize_security_answer(submitted.get(question_id, "invalid answer"))
        answer_matches = check_password(candidate, answer_hash)
        all_answers_match = answer_matches and all_answers_match
    return set(submitted) == set(expected) and all_answers_match


def _hash_submitted_answers(questions):
    result = {}
    for item in questions:
        result[item["question_id"]] = make_password(
            normalize_security_answer(item["answer"])
        )
    return result


def _store_question_profile(user, questions, *, profile=None):
    hashes = _hash_submitted_answers(questions)
    values = list(hashes.items())
    if profile is None:
        return StaffSecurityProfile.objects.create(
            user=user,
            question_1=values[0][0],
            answer_hash_1=values[0][1],
            question_2=values[1][0],
            answer_hash_2=values[1][1],
            question_3=values[2][0],
            answer_hash_3=values[2][1],
        )
    profile.question_1, profile.answer_hash_1 = values[0]
    profile.question_2, profile.answer_hash_2 = values[1]
    profile.question_3, profile.answer_hash_3 = values[2]
    profile.save(update_fields=[
        "question_1", "answer_hash_1", "question_2", "answer_hash_2",
        "question_3", "answer_hash_3", "updated_at",
    ])
    return profile


def _invalidate_recovery_state(user, *, now=None):
    now = now or timezone.now()
    StaffSecurityChallenge.objects.filter(
        user=user, consumed_at__isnull=True, invalidated_at__isnull=True
    ).update(invalidated_at=now)
    StaffPasswordResetToken.objects.filter(
        user=user, consumed_at__isnull=True
    ).update(consumed_at=now)


def _generic_recovery_failure(detail="Invalid or expired staff verification code."):
    return _private_error(detail)


class StaffSecurityQuestionOptionsView(NoStoreAPIView):
    """Return prompt labels only; never return registered answers or hashes."""

    permission_classes = [AllowAny]

    @extend_schema(
        tags=["Staff security"],
        request=None,
        responses={
            200: inline_serializer(
                name="StaffSecurityQuestionOptionsResponse",
                fields={
                    "questions": serializers.ListField(
                        child=inline_serializer(
                            name="StaffSecurityQuestionPrompt",
                            fields={
                                "id": serializers.CharField(),
                                "prompt": serializers.CharField(),
                            },
                        ),
                    ),
                },
            ),
        },
    )
    def get(self, request):
        response = Response({
            "questions": [{"id": question_id, "prompt": prompt}
                          for question_id, prompt in SECURITY_QUESTION_OPTIONS]
        })
        return _no_store(response)


class StaffPasswordChangeView(NoStoreAPIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "staff_password_change"

    @extend_schema(
        tags=["Staff security"],
        request=StaffPasswordChangeSerializer,
        responses={
            200: inline_serializer(
                name="StaffPasswordChangeResponse",
                fields={
                    "message": serializers.CharField(),
                    "action": serializers.CharField(),
                    "redirect_to": serializers.CharField(),
                },
            ),
            400: OpenApiResponse(description="The current password or new password is invalid."),
        },
    )
    def post(self, request):
        serializer = StaffPasswordChangeSerializer(
            data=request.data, context={"user": request.user}
        )
        serializer.is_valid(raise_exception=True)
        user = request.user
        current_password = serializer.validated_data["current_password"]
        new_password = serializer.validated_data["new_password"]

        if not user.check_password(current_password):
            audit_request_action(
                request=request,
                action="STAFF_PASSWORD_CHANGE_REJECTED",
                resource_type="CustomUser",
                resource_id=str(user.pk),
                metadata={"reason": "current_password_mismatch"},
            )
            return Response(
                {"current_password": ["Current password is incorrect."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            user = User.objects.select_for_update().get(pk=user.pk)
            if not user.check_password(current_password):
                return Response(
                    {"current_password": ["Current password is incorrect."]},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            user.set_password(new_password)
            user.password_changed_at = timezone.now()
            user.token_version += 1
            user.save(update_fields=["password", "password_changed_at", "token_version"])
            _invalidate_recovery_state(user)
            audit_request_action(
                request=request,
                action="STAFF_PASSWORD_CHANGED" if is_staff_account(user) else "ACCOUNT_PASSWORD_CHANGED",
                resource_type="CustomUser",
                resource_id=str(user.pk),
                metadata={"method": "authenticated_current_password"},
            )

        response = Response({
            "message": STAFF_PASSWORD_CHANGE_REQUIRED_MESSAGE,
            "action": "redirect_to_login",
            "redirect_to": "/login/",
        })
        return _no_store(response)


class StaffSecurityQuestionsView(NoStoreAPIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "staff_security_questions"

    @extend_schema(
        tags=["Staff security"],
        request=None,
        responses={
            200: inline_serializer(
                name="StaffSecurityQuestionsResponse",
                fields={
                    "registered": serializers.BooleanField(),
                    "questions": serializers.ListField(
                        child=inline_serializer(
                            name="StaffRegisteredSecurityQuestionPrompt",
                            fields={
                                "id": serializers.CharField(),
                                "prompt": serializers.CharField(),
                            },
                        ),
                    ),
                    "message": serializers.CharField(required=False),
                },
            ),
        },
    )
    def get(self, request):
        if not is_staff_account(request.user):
            raise PermissionDenied("Security-question management is available only to staff accounts.")
        profile = StaffSecurityProfile.objects.filter(user=request.user).first()
        if profile is None:
            return _no_store(Response({
                "registered": False,
                "questions": [],
                "message": STAFF_SECURITY_SETUP_REQUIRED_MESSAGE,
            }))
        labels = dict(SECURITY_QUESTION_OPTIONS)
        return _no_store(Response({
            "registered": True,
            "questions": [
                {"id": getattr(profile, f"question_{index}"),
                 "prompt": labels[getattr(profile, f"question_{index}")]}
                for index in range(1, 4)
            ],
        }))

    @extend_schema(
        tags=["Staff security"],
        request=StaffSecurityQuestionsSerializer,
        responses={
            200: inline_serializer(
                name="StaffSecurityQuestionsSavedResponse",
                fields={
                    "registered": serializers.BooleanField(),
                    "message": serializers.CharField(),
                },
            ),
            400: OpenApiResponse(description="Current password or registered security answers are invalid."),
            403: OpenApiResponse(description="Active staff access is required."),
        },
    )
    def put(self, request):
        user = request.user
        if not is_staff_account(user) or not user.is_active:
            raise PermissionDenied("Security-question management is available only to active staff accounts.")

        serializer = StaffSecurityQuestionsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        if not user.check_password(values["current_password"]):
            audit_request_action(
                request=request,
                action="STAFF_SECURITY_QUESTIONS_UPDATE_REJECTED",
                resource_type="CustomUser",
                resource_id=str(user.pk),
                metadata={"reason": "current_password_mismatch"},
            )
            return Response(
                {"current_password": ["Current password is incorrect."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            user = User.objects.select_for_update().get(pk=user.pk)
            if not user.check_password(values["current_password"]):
                return Response(
                    {"current_password": ["Current password is incorrect."]},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            profile = StaffSecurityProfile.objects.select_for_update().filter(user=user).first()
            if profile is not None:
                submitted_current = values.get("current_answers") or []
                if len(submitted_current) != 3 or not _answers_match(profile, submitted_current):
                    audit_request_action(
                        request=request,
                        action="STAFF_SECURITY_QUESTIONS_UPDATE_REJECTED",
                        resource_type="CustomUser",
                        resource_id=str(user.pk),
                        metadata={"reason": "registered_answers_mismatch"},
                    )
                    return Response(
                        {"current_answers": ["All three registered answers are required and must match."]},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            _store_question_profile(user, values["questions"], profile=profile)
            _invalidate_recovery_state(user)
            audit_request_action(
                request=request,
                action="STAFF_SECURITY_QUESTIONS_REGISTERED" if profile is None else "STAFF_SECURITY_QUESTIONS_UPDATED",
                resource_type="CustomUser",
                resource_id=str(user.pk),
                metadata={"question_count": 3},
            )

        return _no_store(Response({"registered": True, "message": "Security questions saved."}))


class _ManagerInitiatedRecoveryView(NoStoreAPIView):
    permission_classes = [IsAuthenticated, IsPlatformSuperuser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "staff_recovery_init"
    purpose: tuple[str, str] | None = None

    @extend_schema(
        tags=["Staff security"],
        request=None,
        responses={
            202: inline_serializer(
                name="StaffRecoveryInitiateResponse",
                fields={
                    "message": serializers.CharField(),
                    "expires_in": serializers.IntegerField(),
                },
            ),
            404: OpenApiResponse(description="Active staff account not found."),
            409: OpenApiResponse(description="The account cannot use this recovery flow."),
            429: OpenApiResponse(description="A recovery message was recently requested."),
        },
    )
    def post(self, request, user_id):
        now = timezone.now()
        with transaction.atomic():
            target = User.objects.select_for_update().filter(
                pk=user_id, is_staff=True, is_active=True, is_superuser=False
            ).first()
            if target is None:
                return Response({"detail": "Active staff account not found."}, status=status.HTTP_404_NOT_FOUND)
            if target.security_status != "NORMAL":
                return Response({"detail": "This staff account is unavailable for recovery."}, status=status.HTTP_409_CONFLICT)

            profile_exists = StaffSecurityProfile.objects.filter(user=target).exists()
            if self.purpose == StaffSecurityChallenge.Purpose.PASSWORD_RESET and not profile_exists:
                return Response({
                    "code": "STAFF_SECURITY_SETUP_REQUIRED",
                    "detail": "Complete the manager-authorized security-question setup before requesting a password reset.",
                }, status=status.HTTP_409_CONFLICT)
            if self.purpose == StaffSecurityChallenge.Purpose.SECURITY_SETUP and profile_exists:
                return Response({
                    "code": "STAFF_SECURITY_ALREADY_REGISTERED",
                    "detail": "Security questions are already registered; use the authenticated update process.",
                }, status=status.HTTP_409_CONFLICT)

            recent_challenge = StaffSecurityChallenge.objects.filter(
                user=target,
                created_at__gt=now - STAFF_CHALLENGE_COOLDOWN,
                consumed_at__isnull=True,
                invalidated_at__isnull=True,
            ).exists()
            if recent_challenge:
                return Response(
                    {"detail": "A staff recovery message was recently sent. Please wait before requesting another."},
                    status=status.HTTP_429_TOO_MANY_REQUESTS,
                )

            _invalidate_recovery_state(target, now=now)
            otp_code = f"{secrets.randbelow(900000) + 100000:06d}"
            challenge = StaffSecurityChallenge.objects.create(
                user=target,
                initiated_by=request.user,
                purpose=self.purpose,
                otp_hash=make_password(otp_code),
                created_at=now,
                expires_at=now + STAFF_OTP_TTL,
            )
            encrypted_code = encrypt_staff_code(otp_code)
            audit_request_action(
                request=request,
                action="STAFF_RECOVERY_INITIATED",
                resource_type="CustomUser",
                resource_id=str(target.pk),
                metadata={"purpose": self.purpose, "challenge_id": str(challenge.pk)},
            )

            def enqueue_recovery_email():
                try:
                    send_staff_security_code.delay(str(challenge.pk), encrypted_code)
                except Exception:
                    StaffSecurityChallenge.objects.filter(
                        pk=challenge.pk, consumed_at__isnull=True
                    ).update(invalidated_at=timezone.now())
                    logger.exception("staff_recovery_email_dispatch_failed challenge_id=%s", challenge.pk)

            transaction.on_commit(enqueue_recovery_email)

        return Response({
            "message": "A verification code was sent to the staff member's registered email.",
            "expires_in": int(STAFF_OTP_TTL.total_seconds()),
        }, status=status.HTTP_202_ACCEPTED)


class StaffPasswordResetInitiateView(_ManagerInitiatedRecoveryView):
    purpose = StaffSecurityChallenge.Purpose.PASSWORD_RESET


class StaffSecurityQuestionSetupInitiateView(_ManagerInitiatedRecoveryView):
    purpose = StaffSecurityChallenge.Purpose.SECURITY_SETUP


class StaffSecuritySetupCompleteView(NoStoreAPIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "staff_recovery_verify"

    @extend_schema(
        tags=["Staff security"],
        request=StaffSecuritySetupCompleteSerializer,
        responses={
            200: inline_serializer(
                name="StaffSecuritySetupCompleteResponse",
                fields={
                    "registered": serializers.BooleanField(),
                    "message": serializers.CharField(),
                },
            ),
            400: OpenApiResponse(description="The staff identity, code, or security answers are invalid."),
        },
    )
    def post(self, request):
        serializer = StaffSecuritySetupCompleteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"].strip().casefold()
        otp = serializer.validated_data["otp"]
        answers = serializer.validated_data["questions"]
        now = timezone.now()

        with transaction.atomic():
            user = User.objects.filter(email__iexact=email, is_staff=True, is_active=True, is_superuser=False).first()
            if user is None:
                return _generic_recovery_failure()
            user = User.objects.select_for_update().filter(pk=user.pk, security_status="NORMAL").first()
            if user is None:
                return _generic_recovery_failure()
            challenge = StaffSecurityChallenge.objects.select_for_update().filter(
                user=user,
                purpose=StaffSecurityChallenge.Purpose.SECURITY_SETUP,
                consumed_at__isnull=True,
                invalidated_at__isnull=True,
                expires_at__gt=now,
            ).order_by("-created_at").first()
            if challenge is None or StaffSecurityProfile.objects.filter(user=user).exists():
                return _generic_recovery_failure()

            if challenge.attempts >= STAFF_RECOVERY_MAX_ATTEMPTS:
                challenge.invalidated_at = now
                challenge.save(update_fields=["invalidated_at"])
                return _generic_recovery_failure()

            otp_matches = check_password(otp, challenge.otp_hash)
            if not otp_matches:
                challenge.attempts += 1
                if challenge.attempts >= STAFF_RECOVERY_MAX_ATTEMPTS:
                    challenge.invalidated_at = now
                challenge.save(update_fields=["attempts", "invalidated_at"])
                audit_request_action(
                    request=request,
                    action="STAFF_SECURITY_SETUP_VERIFICATION_FAILED",
                    resource_type="CustomUser",
                    resource_id=str(user.pk),
                    metadata={"attempt": challenge.attempts},
                )
                return _generic_recovery_failure()

            _store_question_profile(user, answers)
            challenge.consumed_at = now
            challenge.save(update_fields=["consumed_at"])
            audit_request_action(
                request=request,
                action="STAFF_SECURITY_QUESTIONS_REGISTERED",
                resource_type="CustomUser",
                resource_id=str(user.pk),
                metadata={"method": "manager_initiated_email_verification", "question_count": 3},
            )

        return _no_store(Response({"registered": True, "message": "Security questions were registered successfully."}))


class StaffPasswordResetVerifyView(NoStoreAPIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "staff_recovery_verify"

    @extend_schema(
        tags=["Staff security"],
        request=StaffPasswordResetVerifySerializer,
        responses={
            200: inline_serializer(
                name="StaffPasswordResetVerifyResponse",
                fields={
                    "message": serializers.CharField(),
                    "reset_token": serializers.CharField(),
                    "expires_in": serializers.IntegerField(),
                },
            ),
            400: OpenApiResponse(description="The staff identity, code, or security answers are invalid."),
        },
    )
    def post(self, request):
        serializer = StaffPasswordResetVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"].strip().casefold()
        otp = serializer.validated_data["otp"]
        answers = serializer.validated_data["questions"]
        now = timezone.now()

        with transaction.atomic():
            user = User.objects.filter(email__iexact=email, is_staff=True, is_active=True, is_superuser=False).first()
            if user is None:
                return _generic_recovery_failure()
            user = User.objects.select_for_update().filter(pk=user.pk, security_status="NORMAL").first()
            if user is None:
                return _generic_recovery_failure()
            profile = StaffSecurityProfile.objects.select_for_update().filter(user=user).first()
            challenge = StaffSecurityChallenge.objects.select_for_update().filter(
                user=user,
                purpose=StaffSecurityChallenge.Purpose.PASSWORD_RESET,
                consumed_at__isnull=True,
                invalidated_at__isnull=True,
                expires_at__gt=now,
            ).order_by("-created_at").first()
            if profile is None or challenge is None:
                return _generic_recovery_failure()

            if challenge.attempts >= STAFF_RECOVERY_MAX_ATTEMPTS:
                challenge.invalidated_at = now
                challenge.save(update_fields=["invalidated_at"])
                return _generic_recovery_failure()

            otp_matches = check_password(otp, challenge.otp_hash)
            answers_match = _answers_match(profile, answers)
            if not (otp_matches and answers_match):
                challenge.attempts += 1
                if challenge.attempts >= STAFF_RECOVERY_MAX_ATTEMPTS:
                    challenge.invalidated_at = now
                challenge.save(update_fields=["attempts", "invalidated_at"])
                audit_request_action(
                    request=request,
                    action="STAFF_PASSWORD_RESET_VERIFICATION_FAILED",
                    resource_type="CustomUser",
                    resource_id=str(user.pk),
                    metadata={"attempt": challenge.attempts},
                )
                return _generic_recovery_failure()

            raw_token = secrets.token_urlsafe(48)
            token_hash = hashlib.sha256(raw_token.encode("ascii")).hexdigest()
            StaffPasswordResetToken.objects.filter(
                user=user, consumed_at__isnull=True
            ).update(consumed_at=now)
            StaffPasswordResetToken.objects.create(
                user=user,
                challenge=challenge,
                token_hash=token_hash,
                created_at=now,
                expires_at=now + STAFF_RESET_TOKEN_TTL,
            )
            challenge.consumed_at = now
            challenge.save(update_fields=["consumed_at"])
            audit_request_action(
                request=request,
                action="STAFF_PASSWORD_RESET_IDENTITY_VERIFIED",
                resource_type="CustomUser",
                resource_id=str(user.pk),
                metadata={"method": "otp_and_three_security_answers"},
            )

        response = Response({
            "message": "Identity verified. Continue to set a new staff password.",
            "reset_token": raw_token,
            "expires_in": int(STAFF_RESET_TOKEN_TTL.total_seconds()),
        })
        return _no_store(response)


class StaffPasswordResetCompleteView(NoStoreAPIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "staff_recovery_verify"

    @extend_schema(
        tags=["Staff security"],
        request=StaffPasswordResetCompleteSerializer,
        responses={
            200: inline_serializer(
                name="StaffPasswordResetCompleteResponse",
                fields={
                    "message": serializers.CharField(),
                    "action": serializers.CharField(),
                    "redirect_to": serializers.CharField(),
                },
            ),
            400: OpenApiResponse(description="The reset token or new password is invalid."),
        },
    )
    def post(self, request):
        serializer = StaffPasswordResetCompleteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        raw_token = serializer.validated_data["reset_token"]
        new_password = serializer.validated_data["new_password"]
        token_hash = hashlib.sha256(raw_token.encode("ascii")).hexdigest()
        now = timezone.now()

        with transaction.atomic():
            reset_record = StaffPasswordResetToken.objects.select_for_update().select_related("user").filter(
                token_hash=token_hash,
                consumed_at__isnull=True,
                expires_at__gt=now,
            ).first()
            if reset_record is None:
                return _generic_recovery_failure("Invalid or expired staff reset token.")
            user = User.objects.select_for_update().filter(
                pk=reset_record.user_id,
                is_staff=True,
                is_active=True,
                is_superuser=False,
                security_status="NORMAL",
            ).first()
            if user is None or StaffSecurityProfile.objects.filter(user=user).first() is None:
                reset_record.consumed_at = now
                reset_record.save(update_fields=["consumed_at"])
                return _generic_recovery_failure("Invalid or expired staff reset token.")

            try:
                validate_password(new_password, user=user)
            except DjangoValidationError as exc:
                return Response({"new_password": exc.messages}, status=status.HTTP_400_BAD_REQUEST)

            user.set_password(new_password)
            user.password_changed_at = now
            user.token_version += 1
            user.save(update_fields=["password", "password_changed_at", "token_version"])
            reset_record.consumed_at = now
            reset_record.save(update_fields=["consumed_at"])
            _invalidate_recovery_state(user, now=now)
            audit_request_action(
                request=request,
                action="STAFF_PASSWORD_RESET_COMPLETED",
                resource_type="CustomUser",
                resource_id=str(user.pk),
                metadata={"method": "manager_initiated_otp_and_security_answers"},
            )

        response = Response({
            "message": "Staff password reset successfully. Sign in using the new password.",
            "action": "redirect_to_login",
            "redirect_to": "/login/",
        })
        return _no_store(response)
