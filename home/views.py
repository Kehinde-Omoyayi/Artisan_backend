import hashlib
import logging
import secrets
import time

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.throttling import ScopedRateThrottle
from rest_framework_simplejwt.views import TokenRefreshView
from rest_framework.parsers import FormParser, MultiPartParser, JSONParser

from django.core.cache import cache
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import update_last_login
from django.db import IntegrityError, transaction
from django.utils import timezone

from drf_spectacular.utils import extend_schema, OpenApiExample, OpenApiResponse

from .serializers import *
from .generic import *
from .tasks import *
from .auth_tokens import issue_marketplace_tokens
from .otp_security import (
    hash_otp_code,
    otp_attempts_cache_key,
    otp_cache_key,
    otp_cooldown_cache_key,
    reset_token_cache_key,
    verify_otp_code,
)
from .security_mail import encrypt_email_code
from .staff_mfa import begin_staff_mfa_challenge, mask_staff_email, staff_identity_payload
from .security_http import NoStoreAPIView, NoStoreResponseMixin
from .password_policy import (
    is_staff_account,
    staff_password_change_required,
    staff_security_questions_required,
)
from risk.network import record_customer_network_observation

User = get_user_model()

AUTH_OTP_MAX_ATTEMPTS = 5
AUTH_OTP_TTL_SECONDS = 300

def _otp_attempts_key(prefix, email):
    return otp_attempts_cache_key(prefix, email)

def _otp_attempt_is_exhausted(prefix, email):
    key = _otp_attempts_key(prefix, email)
    try:
        return int(cache.get(key) or 0) >= AUTH_OTP_MAX_ATTEMPTS
    except (TypeError, ValueError):
        return False

def _claim_otp_cooldown(key):
    now = time.time()
    cooldown_until = now + AUTH_OTP_TTL_SECONDS
    if cache.add(key, cooldown_until, timeout=AUTH_OTP_TTL_SECONDS):
        return True, AUTH_OTP_TTL_SECONDS

    existing = cache.get(key)
    if existing:
        try:
            remaining = int(float(existing) - now)
        except (TypeError, ValueError):
            remaining = AUTH_OTP_TTL_SECONDS
        if remaining > 0:
            return False, remaining

    # The key may have expired between the read and add. Retry once using the cache's atomic add.
    if cache.add(key, cooldown_until, timeout=AUTH_OTP_TTL_SECONDS):
        return True, AUTH_OTP_TTL_SECONDS

    return False, 1

def _record_failed_otp_attempt(prefix, email):
    key = _otp_attempts_key(prefix, email)
    try:
        if cache.add(key, 1, timeout=AUTH_OTP_TTL_SECONDS):
            return 1
        return int(cache.incr(key))
    except ValueError:
        # The key may have expired between add/incr; restart the window atomically.
        cache.add(key, 1, timeout=AUTH_OTP_TTL_SECONDS)
        return int(cache.get(key) or 1)

logger = logging.getLogger(__name__)

GENERIC_OTP_ERROR = "Invalid or expired OTP. Please request a new one."
AUTH_LOGIN_MAX_FAILURES = getattr(settings, "AUTH_LOGIN_MAX_FAILURES", 10)
AUTH_LOGIN_FAILURE_WINDOW = getattr(settings, "AUTH_LOGIN_FAILURE_WINDOW_SECONDS", 900)


def _validated_payload(serializer_class, request):
    """Validate the body with a serializer; non-object or mistyped JSON becomes a 400, never a 500."""
    data = request.data if hasattr(request.data, "get") else {}
    serializer = serializer_class(data=data)
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data


def _enqueue_email(task, *args):
    """Hand an email to Celery without letting a broker outage turn a committed action into a 500."""
    try:
        task.delay(*args)
        return True
    except Exception:
        logger.exception("email_enqueue_failed task=%s", getattr(task, "name", task))
        return False


def _login_failure_key(email):
    return "login_fail_" + hashlib.sha256(email.encode("utf-8")).hexdigest()[:32]


def _login_failures_exhausted(email):
    return int(cache.get(_login_failure_key(email), 0) or 0) >= AUTH_LOGIN_MAX_FAILURES


def _record_login_failure(email):
    key = _login_failure_key(email)
    if cache.add(key, 1, timeout=AUTH_LOGIN_FAILURE_WINDOW):
        return 1
    try:
        return cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=AUTH_LOGIN_FAILURE_WINDOW)
        return 1


def _check_credentials(email, password):
    """Return the user only when the password is correct, in near-constant time for unknown accounts."""
    user = User.objects.filter(email__iexact=email).order_by("id").first()
    if user is None:
        make_password(password)
        return None
    return user if user.check_password(password) else None


class RegisterView(NoStoreAPIView):
    permission_classes = [AnonymousOnly]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth_register"
    parser_classes = [JSONParser]

    @extend_schema(
        request=RegisterSerializer,
        responses={
            201: RegisterResponseSerializer,
            400: ErrorResponseSerializer,
        },
        examples=[
            OpenApiExample(
                "Registration Request",
                request_only=True,
                value={
                    "email": "kehinde@example.com",
                    "first_name": "Kehinde",
                    "last_name": "Omoyayi",
                    "password": "StrongPassword123",  # nosec B105: OpenAPI example only, never used as a credential
                    "role": "customer",
                },
            ),
            OpenApiExample(
                "Registration Response",
                response_only=True,
                value={
                    "message": "Dear esteem Kehinde! Your account has been successfully registered.",
                    "action": "redirect_to_otp_verification",
                    "email": "kehinde@example.com",
                },
            ),
        ],
        tags=["Authentication"],
    )
    def post(self, request, *args, **kwargs):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            with transaction.atomic():
                user = serializer.save()
        except IntegrityError:
            return Response({"email": ["A user with this email already exists."]}, status=status.HTTP_400_BAD_REQUEST)
        email = user.email.lower().strip()
        otp_code = f"{secrets.randbelow(900000) + 100000:06d}"

        cache.set(otp_cache_key("verification", email), hash_otp_code("verification", email, otp_code), timeout=AUTH_OTP_TTL_SECONDS)

        _enqueue_email(send_verification_email, user.first_name, user.email, encrypt_email_code(otp_code))

        return Response(
            {
                "message": f"Dear esteem {user.first_name}! Your account has been successfully registered.",
                "action": "redirect_to_otp_verification",
                "email": user.email,
                "redirect_to": "/api/v1/verify-otp/",
            },
            status=status.HTTP_201_CREATED,
        )

class VerifyOTPView(NoStoreAPIView):
    permission_classes = [AnonymousOnly]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth_verify_otp"
    parser_classes = [JSONParser]

    @extend_schema(
        request=VerifyOTPSerializer,
        responses={
            200: OTPResponseSerializer,
            400: ErrorResponseSerializer,
            403: ErrorResponseSerializer,
            404: ErrorResponseSerializer,
        },
        tags=["Authentication"],
    )
    def post(self, request, *args, **kwargs):
        payload = _validated_payload(VerifyOTPSerializer, request)
        email = payload["email"].strip().lower()
        user_otp = payload["otp"]
        cache_key = otp_cache_key("verification", email)

        if _otp_attempt_is_exhausted("otp", email):
            cache.delete(cache_key)
            return Response({"error": "Too many invalid OTP attempts. Please request a new code."}, status=status.HTTP_429_TOO_MANY_REQUESTS)

        user = User.objects.filter(email__iexact=email).order_by("id").first()
        # Unknown, locked, already verified and previously active (deactivated) accounts all receive the
        # same answer, so this endpoint cannot be used to discover accounts or to reactivate them.
        if user is None or is_user_locked(user) or user.is_active or user.last_login is not None:
            _record_failed_otp_attempt("otp", email)
            return Response({"error": GENERIC_OTP_ERROR, "action": "request_new_otp"}, status=status.HTTP_400_BAD_REQUEST)

        saved_otp = cache.get(cache_key)
        if saved_otp is None:
            _record_failed_otp_attempt("otp", email)
            return Response({"error": GENERIC_OTP_ERROR, "action": "request_new_otp"}, status=status.HTTP_400_BAD_REQUEST)

        if not verify_otp_code("verification", email, user_otp, saved_otp):
            attempts = _record_failed_otp_attempt("otp", email)
            if attempts >= AUTH_OTP_MAX_ATTEMPTS:
                cache.delete(cache_key)
                return Response({"error": "Too many invalid OTP attempts. Please request a new code."}, status=status.HTTP_429_TOO_MANY_REQUESTS)
            return Response({"error": "The code you entered is not right. Please try again."}, status=status.HTTP_400_BAD_REQUEST)

        # Only activation: a risk-review or other security status set by staff is never reset here.
        user.is_active = True
        user.save(update_fields=["is_active"])

        cache.delete(cache_key)
        cache.delete(_otp_attempts_key("otp", email))
        cache.delete(otp_cooldown_cache_key("verification", email))

        return Response(
            {
                "message": "OTP verification successful! Account is fully ready.",
                "action": "redirect_to_login",
                "redirect_to": "/api/v1/login/",
                "email": user.email,
            },
            status=status.HTTP_200_OK,
        )

class RequestOTPView(NoStoreAPIView):
    permission_classes = [AnonymousOnly]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth_request_otp"
    parser_classes = [JSONParser]

    @extend_schema(
        request=RequestOTPSerializer,
        responses={
            200: OTPResponseSerializer,
            400: ErrorResponseSerializer,
            403: ErrorResponseSerializer,
            404: ErrorResponseSerializer,
            429: ErrorResponseSerializer,
        },
        tags=["Authentication"],
    )
    def post(self, request, *args, **kwargs):
        email = _validated_payload(RequestOTPSerializer, request)["email"].strip().lower()

        def uniform():
            # Identical for unknown, locked, verified, cooling-down and eligible accounts (no enumeration).
            return Response(
                {
                    "message": "If the account is eligible, a verification code has been sent. A new code can be requested every 5 minutes.",
                    "action": "enter_otp",
                    "email": email,
                    "retry_after": AUTH_OTP_TTL_SECONDS,
                },
                status=status.HTTP_200_OK,
            )

        user = User.objects.filter(email__iexact=email).order_by("id").first()
        if user is None or is_user_locked(user) or user.is_active or user.last_login is not None:
            return uniform()

        claimed, _retry_after = _claim_otp_cooldown(otp_cooldown_cache_key("verification", email))
        if not claimed:
            return uniform()

        otp_code = f"{secrets.randbelow(900000) + 100000:06d}"
        cache.set(otp_cache_key("verification", email), hash_otp_code("verification", email, otp_code), timeout=AUTH_OTP_TTL_SECONDS)
        _enqueue_email(send_otp_email, user.first_name, user.email, encrypt_email_code(otp_code))
        return uniform()

class LoginView(NoStoreAPIView):
    permission_classes = [AnonymousOnly]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth_login"
    parser_classes = [JSONParser, FormParser, MultiPartParser]

    @extend_schema(
    request=LoginSerializer,
    responses={
        200: LoginResponseSerializer,
        400: ErrorResponseSerializer,
        401: ErrorResponseSerializer,
        403: ErrorResponseSerializer,
    },
    examples=[
        OpenApiExample(
            "Login Request",
            request_only=True,
            value={
                "email": "kehinde@example.com",
                "password": "StrongPassword123"  # nosec B105: OpenAPI example only, never used as a credential
            }
        ),
        OpenApiExample(
            "Login Success",
            response_only=True,
            value={
                "message": "Distinguished Kehinde, Account Login successful, see you at the other side",
                "access": "eyJhbGciOiJIUzI1NiIs...",
                "refresh": "eyJhbGciOiJIUzI1NiIs...",
                "user": {
                    "first_name": "Kehinde",
                    "last_name": "Omoyayi",
                    "email": "kehinde@example.com"
                }
            }
        ),
        OpenApiExample(
            "Unverified Account",
            response_only=True,
            value={
                "error": "Distinguished Kehinde. Your account has not been verified.",
                "action": "redirect_to_otp_verification"
            }
        ),
    ])

    def post(self, request, *args, **kwargs):
        try:
            payload = _validated_payload(LoginSerializer, request)
        except Exception:
            return Response({"error": "Enter a valid email address and password."}, status=status.HTTP_400_BAD_REQUEST)
        email = payload["email"].strip().casefold()
        password = payload["password"]

        if _login_failures_exhausted(email):
            return Response({"error": "Too many failed sign-in attempts. Please wait a few minutes and try again."}, status=status.HTTP_429_TOO_MANY_REQUESTS)

        user = _check_credentials(email, password)
        if user is None:
            _record_login_failure(email)
            return Response({"error": "invalid email or password."}, status=status.HTTP_401_UNAUTHORIZED)

        if is_user_locked(user):
            return locked_response()

        if not user.is_active:
            # Reached only with a correct password, so it reveals nothing to a guesser.
            return Response(
                {
                    "error": f"Distinguished {user.first_name}. Your account has not been verified.",
                    "action": "redirect_to_otp_verification",
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        cache.delete(_login_failure_key(email))
        if is_staff_account(user):
            challenge, resend_after, _sent = begin_staff_mfa_challenge(
                user=user,
                request=request,
                method="password",
            )
            response = Response({
                "mfa_required": True,
                "mfa_challenge_id": str(challenge.pk),
                "masked_email": mask_staff_email(user.email),
                "expires_in": max(0, int((challenge.expires_at - timezone.now()).total_seconds())),
                "resend_after": resend_after,
                "user": staff_identity_payload(user),
            }, status=status.HTTP_202_ACCEPTED)
            response["Cache-Control"] = "no-store, private"
            response["Pragma"] = "no-cache"
            return response

        customer_profile = getattr(user, "customer_profile", None)
        if customer_profile is not None:
            record_customer_network_observation(customer_profile, request, observation_type="LOGIN")

        update_last_login(None, user)
        tokens = issue_marketplace_tokens(user)
        user_payload = {
            "id": str(user.pk),
            "first_name": user.first_name,
            "last_name": user.last_name,
            "email": user.email,
            "is_staff": bool(user.is_staff),
            "is_superuser": bool(user.is_superuser),
            "role": "",
            "session_version": tokens["session_version"],
        }
        response = Response(
            {
                "message": f"Distinguished {user.first_name}, Account Login successful, see you at the other side",
                "access": tokens["access"],
                "refresh": tokens["refresh"],
                "user": user_payload,
                "redirect_to": "/dashboard/",
                "staff_password_change_required": False,  # nosec B105: boolean response flag, not a credential
                "staff_security_setup_required": False,
            },
            status=status.HTTP_200_OK,
        )
        response["Cache-Control"] = "no-store, private"
        response["Pragma"] = "no-cache"
        return response

class LogoutView(NoStoreAPIView):
    permission_classes = [IsAuthenticated]
    @extend_schema(
    tags=["Authentication"],
    request=None,
    responses={
        200: OpenApiResponse(description="Logout successful."),
        401: OpenApiResponse(description="Authentication required."),
    },)

    def post(self, request, *args, **kwargs):
        revoke_all_sessions(request.user)

        return Response(
            {
                "message": f"Distinguished {request.user.first_name}, you have been logged out successfully.",
                "action": "redirect_to_login",
                "redirect_to": "/login/",
            },
            status=status.HTTP_200_OK
        )


class ForgotPasswordView(NoStoreAPIView):
    permission_classes = [AnonymousOnly]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth_forgot_password"
    parser_classes = [JSONParser, FormParser, MultiPartParser]

    @extend_schema(
        request=ForgotPasswordSerializer,
        responses={
            200: OTPResponseSerializer,
            400: ErrorResponseSerializer,
            404: ErrorResponseSerializer,
            429: ErrorResponseSerializer,
            500: ErrorResponseSerializer,
        }
    )
    def post(self, request, *args, **kwargs):
        serializer = ForgotPasswordSerializer(data=request.data)

        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        email = serializer.validated_data["email"].lower()
        user = User.objects.filter(email__iexact=email).first()

        if not user or is_staff_account(user):
            return Response(
                {
                    "message": "If the account exists, a password reset code will be sent.",
                    "action": "enter_reset_otp",
                    "email": email,
                    "retry_after": AUTH_OTP_TTL_SECONDS,
                },
                status=status.HTTP_200_OK,
            )

        if is_user_locked(user):
            return Response(
                {
                    "message": "If the account exists, a password reset code will be sent.",
                    "action": "enter_reset_otp",
                    "email": email,
                    "retry_after": AUTH_OTP_TTL_SECONDS,
                },
                status=status.HTTP_200_OK,
            )

        cache_key = otp_cache_key("password_reset", email)
        cooldown_key = otp_cooldown_cache_key("password_reset", email)
        claimed, retry_after = _claim_otp_cooldown(cooldown_key)

        if not claimed:
            return Response(
                {
                    "error": "Please wait before requesting another password reset code.",
                    "retry_after": retry_after,
                    "action": "wait"
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS
            )

        otp_code = f"{secrets.randbelow(900000) + 100000:06d}"
        cache.set(cache_key, hash_otp_code("password_reset", email, otp_code), timeout=AUTH_OTP_TTL_SECONDS)
        cache.delete(_otp_attempts_key("password_reset_otp", email))

        _enqueue_email(send_password_reset_email, user.first_name, user.email, encrypt_email_code(otp_code))

        return Response(
            {
                "message": "If the account exists, a password reset code will be sent.",
                "action": "enter_reset_otp",
                "email": user.email,
                "retry_after": 300
            },
            status=status.HTTP_200_OK
        )


class VerifyResetOTPView(NoStoreAPIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth_verify_reset"

    @extend_schema(
        tags=["Authentication"],
        description=(
            "Verifies the OTP sent to the user's email "
            "and returns a short-lived password reset token."
        ),
        request=VerifyResetOTPSerializer,
        responses={
            200: VerifyResetOTPResponseSerializer,
            400: OpenApiResponse(
                description="Invalid or expired OTP."
            ),
        },
        examples=[
            OpenApiExample(
                "Verify OTP Request",
                request_only=True,
                value={
                    "email": "user@example.com",
                    "otp": "123456",
                },
            ),
            OpenApiExample(
                "OTP Verified",
                response_only=True,
                value={
                    "message": "OTP verified successfully.",
                    "action": "redirect_to_reset_password",
                    "reset_token": "temporary_secure_reset_token",  # nosec B105: nonfunctional OpenAPI example token
                    "expires_in": 600,
                },
            ),
        ],
    )
    def post(self, request):
        serializer = VerifyResetOTPSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        email = serializer.validated_data["email"].strip().lower()
        otp = serializer.validated_data["otp"]

        user = User.objects.filter(email__iexact=email).first()

        if user is None or is_staff_account(user):
            return Response(
                {"error": "Invalid or expired OTP."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not user.is_active:
            return Response(
                {"error": "Invalid or expired OTP."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # This key must match the key used by ForgotPasswordView.
        otp_key = otp_cache_key("password_reset", email)

        if _otp_attempt_is_exhausted("password_reset_otp", email):
            cache.delete(otp_key)
            return Response(
                {"error": "Too many invalid OTP attempts. Please request a new code."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        stored_otp = cache.get(otp_key)

        if not verify_otp_code("password_reset", email, otp, stored_otp):
            attempts = _record_failed_otp_attempt("password_reset_otp", email)
            if attempts >= AUTH_OTP_MAX_ATTEMPTS:
                cache.delete(otp_key)
                return Response(
                    {"error": "Too many invalid OTP attempts. Please request a new code."},
                    status=status.HTTP_429_TOO_MANY_REQUESTS,
                )
            return Response(
                {"error": "Invalid or expired OTP."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Generate a secure, single-use password reset token.
        reset_token = secrets.token_urlsafe(32)

        reset_token_key = reset_token_cache_key(reset_token)

        cache.set(
            reset_token_key,
            user.pk,
            timeout=600,
        )

        # Prevent the same OTP from being reused.
        cache.delete(otp_key)
        cache.delete(_otp_attempts_key("password_reset_otp", email))

        response = Response(
            {
                "message": "OTP verified successfully.",
                "action": "redirect_to_reset_password",
                "reset_token": reset_token,
                "expires_in": 600,
            },
            status=status.HTTP_200_OK,
        )
        response["Cache-Control"] = "no-store, private"
        response["Pragma"] = "no-cache"
        return response


class ResetPasswordView(NoStoreAPIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth_reset_password"

    @extend_schema(
        tags=["Authentication"],
        description=(
            "Resets the user's password using the temporary "
            "token issued after successful OTP verification."
        ),
        request=ResetPasswordSerializer,
        responses={
            200: ResetPasswordResponseSerializer,
            400: OpenApiResponse(
                description="Invalid token or password validation error."
            ),
        },
        examples=[
            OpenApiExample(
                "Reset Password Request",
                request_only=True,
                value={
                    "reset_token": "temporary_secure_reset_token",  # nosec B105: nonfunctional OpenAPI example token
                    "new_password": "NewStrongPassword123!",  # nosec B105: OpenAPI example only, never used as a credential
                    "confirm_password": "NewStrongPassword123!",  # nosec B105: OpenAPI example only, never used as a credential
                },
            ),
            OpenApiExample(
                "Password Reset Successful",
                response_only=True,
                value={
                    "message": "Password reset successfully.",
                    "action": "redirect_to_login",
                },
            ),
        ],
    )
    def post(self, request):
        serializer = ResetPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        reset_token = serializer.validated_data["reset_token"]
        new_password = serializer.validated_data["new_password"]

        reset_token_key = reset_token_cache_key(reset_token)

        user_id = cache.get(reset_token_key)

        if not user_id:
            return Response(
                {"error": "Invalid or expired reset token."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = User.objects.filter(pk=user_id).first()

        if user is None:
            cache.delete(reset_token_key)
            return Response(
                {"error": "Invalid or expired reset token."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if is_user_locked(user) or not user.is_active or is_staff_account(user):
            cache.delete(reset_token_key)
            return Response(
                {"error": "Invalid or expired reset token."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Apply Django's configured password validation.
        validate_password(new_password, user=user)

        user.set_password(new_password)

        # Invalidate existing JWTs if your custom authentication
        # checks the user's token_version field.
        if hasattr(user, "token_version"):
            user.token_version += 1
            user.save(update_fields=["password", "token_version"])
        else:
            user.save(update_fields=["password"])

        # A reset token can only be used once.
        cache.delete(reset_token_key)

        return Response(
            {
                "message": "Password reset successfully.",
                "action": "redirect_to_login",
            },
            status=status.HTTP_200_OK,
        )


class MarketplaceTokenRefreshView(NoStoreResponseMixin, TokenRefreshView):
    serializer_class = MarketplaceTokenRefreshSerializer


