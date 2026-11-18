"""Public possession-code endpoints used only after a staff primary sign-in."""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import update_last_login
from django.db import transaction
from django.utils import timezone
from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from .security_http import NoStoreAPIView

from core.audit import audit_request_action
from .auth_tokens import issue_marketplace_tokens
from .models import StaffSecurityChallenge
from .password_policy import is_staff_account, staff_password_change_required, staff_security_questions_required
from .staff_mfa import (
    mask_staff_email,
    resend_staff_mfa_challenge,
    verify_staff_mfa_challenge,
)

User = get_user_model()


def _no_store(response):
    response["Cache-Control"] = "no-store, private"
    response["Pragma"] = "no-cache"
    response["Referrer-Policy"] = "no-referrer"
    return response


class StaffMFAVerifySerializer(serializers.Serializer):
    challenge_id = serializers.UUIDField()
    code = serializers.RegexField(r"^[0-9]{6}$", write_only=True)


class StaffMFAResendSerializer(serializers.Serializer):
    challenge_id = serializers.UUIDField()


def _invalid_challenge():
    return _no_store(Response({"error": "Invalid or expired staff sign-in code."}, status=status.HTTP_400_BAD_REQUEST))


class StaffMFAVerifyView(NoStoreAPIView):
    """Complete staff sign-in with a single-use short-lived code."""

    authentication_classes: list[type] = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "staff_mfa_verify"

    @extend_schema(
        tags=["Staff security"],
        request=StaffMFAVerifySerializer,
        responses={
            200: inline_serializer(
                name="StaffMFAVerifyResponse",
                fields={
                    "message": serializers.CharField(),
                    "access": serializers.CharField(),
                    "refresh": serializers.CharField(),
                    "staff_mfa_authenticated_at": serializers.IntegerField(allow_null=True),
                    "staff_password_change_required": serializers.BooleanField(),
                    "staff_security_setup_required": serializers.BooleanField(),
                    "user": inline_serializer(
                        name="StaffMFAVerifyUser",
                        fields={
                            "id": serializers.CharField(),
                            "first_name": serializers.CharField(),
                            "last_name": serializers.CharField(),
                            "email": serializers.EmailField(),
                            "is_staff": serializers.BooleanField(),
                            "is_superuser": serializers.BooleanField(),
                            "role": serializers.CharField(),
                            "session_version": serializers.IntegerField(),
                        },
                    ),
                },
            ),
            400: OpenApiResponse(description="The staff challenge or verification code is invalid or expired."),
        },
    )
    def post(self, request):
        serializer = StaffMFAVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = verify_staff_mfa_challenge(
            challenge_id=serializer.validated_data["challenge_id"],
            code=serializer.validated_data["code"],
            request=request,
        )
        if result is None:
            return _invalid_challenge()

        user, now = result
        update_last_login(None, user)
        tokens = issue_marketplace_tokens(
            user,
            staff_mfa_verified=True,
            authenticated_at=now,
        )
        return Response({
            "message": "Staff sign-in verified.",
            "access": tokens["access"],
            "refresh": tokens["refresh"],
            "staff_mfa_authenticated_at": tokens["staff_mfa_at"],
            "staff_password_change_required": staff_password_change_required(user, now=now),
            "staff_security_setup_required": staff_security_questions_required(user),
            "user": {
                "id": str(user.pk),
                "first_name": user.first_name,
                "last_name": user.last_name,
                "email": user.email,
                "is_staff": bool(user.is_staff),
                "is_superuser": bool(user.is_superuser),
                "role": "staff",
                "session_version": tokens["session_version"],
            },
        })


class StaffMFAResendView(NoStoreAPIView):
    """Resend to the address bound to an active password/social challenge; never accepts an email."""

    authentication_classes: list[type] = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "staff_mfa_resend"

    @extend_schema(
        tags=["Staff security"],
        request=StaffMFAResendSerializer,
        responses={
            202: inline_serializer(
                name="StaffMFAResendResponse",
                fields={
                    "message": serializers.CharField(),
                    "challenge_id": serializers.UUIDField(),
                    "masked_email": serializers.CharField(),
                    "expires_in": serializers.IntegerField(),
                    "resend_after": serializers.IntegerField(),
                },
            ),
            400: OpenApiResponse(description="The staff challenge is invalid or expired."),
            429: OpenApiResponse(description="The staff resend limit was reached."),
        },
    )
    def post(self, request):
        serializer = StaffMFAResendSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        challenge, retry_after, sent = resend_staff_mfa_challenge(
            challenge_id=serializer.validated_data["challenge_id"],
            request=request,
        )
        if challenge is None:
            return _invalid_challenge()
        now = timezone.now()
        return _no_store(Response({
            "message": "A sign-in code was sent to the registered staff email."
            if sent
            else "A sign-in code was recently sent. Check the registered staff email.",
            "challenge_id": str(challenge.pk),
            "masked_email": mask_staff_email(challenge.user.email),
            "expires_in": max(0, int((challenge.expires_at - now).total_seconds())),
            "resend_after": retry_after,
        }, status=status.HTTP_202_ACCEPTED))
