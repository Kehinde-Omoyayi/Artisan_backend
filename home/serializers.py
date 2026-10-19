import re
from django.db import transaction
from django.contrib.auth import get_user_model
from django.conf import settings
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from django.contrib.auth.password_validation import validate_password
from rest_framework_simplejwt.exceptions import InvalidToken
from rest_framework_simplejwt.serializers import TokenRefreshSerializer

from .models import CustomerProfile, CustomUser
from .auth_tokens import validate_marketplace_token_claims
from .phone_numbers import normalize_phone

User = get_user_model()

class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(
        write_only=True,
        min_length=8,
        validators=[validate_password],
        style={"input_type": "password"},
    )
    role = serializers.ChoiceField(choices=["customer", "artisan"], write_only=True)

    class Meta:
        model = CustomUser
        fields = ["email", "first_name", "last_name", "password", "role"]

    def validate_email(self, value):
        if CustomUser.objects.filter(email__iexact=value).exists():
            raise ValidationError("A user with this email already exists.")
        return value.lower()

    def create(self, validated_data):
        role = validated_data.pop("role")
        password = validated_data.pop("password")

        with transaction.atomic():
            user = CustomUser.objects.create_user(password=password, is_active=False, **validated_data)
            if role == "customer":
                CustomerProfile.objects.create(user=user)
            else:
                from .models import ArtisanProfile
                ArtisanProfile.objects.create(user=user)
                # Artisans may also initiate customer-side service requests. Keep a
                # separate customer profile without changing the selected role.
                CustomerProfile.objects.create(user=user)
        return user

class VerifyOTPSerializer(serializers.Serializer):
    email = serializers.EmailField()
    otp = serializers.RegexField(r"^[0-9]{6}$", error_messages={"invalid": "Enter the 6-digit code."})

class RequestOTPSerializer(serializers.Serializer):
    email = serializers.EmailField()

class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)

class RegisterResponseSerializer(serializers.Serializer):
    message = serializers.CharField()
    action = serializers.CharField()
    email = serializers.EmailField()

class OTPResponseSerializer(serializers.Serializer):
    message = serializers.CharField()
    action = serializers.CharField()
    email = serializers.EmailField()

class ErrorResponseSerializer(serializers.Serializer):
    error = serializers.CharField()

class LoginUserSerializer(serializers.Serializer):
    id = serializers.CharField(required=False)
    first_name = serializers.CharField()
    last_name = serializers.CharField()
    email = serializers.EmailField()
    is_staff = serializers.BooleanField(required=False)
    is_superuser = serializers.BooleanField(required=False)
    role = serializers.CharField(required=False, allow_blank=True)
    session_version = serializers.IntegerField(required=False)

class LoginResponseSerializer(serializers.Serializer):
    message = serializers.CharField()
    access = serializers.CharField(required=False)
    refresh = serializers.CharField(required=False)
    user = LoginUserSerializer()
    redirect_to = serializers.CharField(required=False)
    mfa_required = serializers.BooleanField(required=False)
    mfa_challenge_id = serializers.UUIDField(required=False)
    masked_email = serializers.CharField(required=False)
    expires_in = serializers.IntegerField(required=False)
    resend_after = serializers.IntegerField(required=False)
    staff_password_change_required = serializers.BooleanField(required=False)
    staff_security_setup_required = serializers.BooleanField(required=False)

class ForgotPasswordSerializer(serializers.Serializer):
    email = serializers.EmailField()



class LogoutResponseSerializer(serializers.Serializer):
    message = serializers.CharField()
    action = serializers.CharField()


class VerifyResetOTPSerializer(serializers.Serializer):
    email = serializers.EmailField()
    otp = serializers.RegexField(r"^[0-9]{6}$", error_messages={"invalid": "Enter the 6-digit code."})


class VerifyResetOTPResponseSerializer(serializers.Serializer):
    message = serializers.CharField()
    action = serializers.CharField()
    reset_token = serializers.CharField()
    expires_in = serializers.IntegerField()


class ResetPasswordSerializer(serializers.Serializer):
    reset_token = serializers.CharField()
    new_password = serializers.CharField(
        write_only=True,
        min_length=8,
        validators=[validate_password],
    )
    confirm_password = serializers.CharField(
        write_only=True,
        min_length=8,
    )

    def validate(self, attrs):
        if attrs["new_password"] != attrs["confirm_password"]:
            raise serializers.ValidationError({
                "confirm_password": "Passwords do not match."  # nosec B105: validation message, not a credential
            })

        return attrs


class ResetPasswordResponseSerializer(serializers.Serializer):
    message = serializers.CharField()
    action = serializers.CharField()


class MarketplaceTokenRefreshSerializer(TokenRefreshSerializer):

    def validate(self, attrs):
        refresh = self.token_class(attrs["refresh"])
        user_id = refresh.get(settings.SIMPLE_JWT.get("USER_ID_CLAIM", "user_id"))
        token_version = refresh.get("token_version")

        user = User.objects.filter(pk=user_id).first()
        try:
            token_version = int(token_version)
        except (TypeError, ValueError):
            token_version = None

        if (
            user is None
            or token_version is None
            or token_version != user.token_version
            or not user.is_active
            or user.security_status == "LOCKED"
        ):
            raise InvalidToken("Session has been revoked or the account is unavailable.")

        validate_marketplace_token_claims(refresh, user)
        return super().validate(attrs)