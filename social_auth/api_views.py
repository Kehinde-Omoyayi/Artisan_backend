"""Native and browser authorization-code endpoints; mount with social_auth.api_urls."""

from drf_spectacular.utils import OpenApiResponse, OpenApiTypes, extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from home.serializers import LoginResponseSerializer, RegisterResponseSerializer

from home.generic import AnonymousOnly
from home.security_http import NoStoreAPIView

from .exceptions import SocialAuthError
from .oauth import (
    authorization_url,
    code_challenge,
    consume_transaction,
    create_transaction,
    exchange_authorization_code,
    provider_config,
    apple_user_names,
)
from .serializers import (
    CompleteProfileSerializer,
    NativeCompleteSerializer,
    NativeStartSerializer,
    WebCompleteSerializer,
    WebStartSerializer,
)
from .services import begin_social_registration, complete_pending_profile, social_login


class NativeSocialStartView(NoStoreAPIView):
    permission_classes = [AnonymousOnly]
    throttle_classes = [AnonRateThrottle]

    @extend_schema(
        tags=["Social authentication"],
        request=NativeStartSerializer,
        responses={
            200: inline_serializer(
                name="NativeSocialStartResponse",
                fields={
                    "provider": serializers.CharField(),
                    "intent": serializers.CharField(),
                    "state": serializers.CharField(),
                    "nonce": serializers.CharField(),
                    "client_id": serializers.CharField(),
                    "expires_in": serializers.IntegerField(),
                    "code_challenge": serializers.CharField(required=False),
                    "code_challenge_method": serializers.CharField(required=False),
                    "authorization_url": serializers.URLField(required=False),
                },
            ),
            400: OpenApiResponse(description="The provider or social sign-in request is invalid."),
            503: OpenApiResponse(description="The identity provider is temporarily unavailable."),
        },
    )
    def post(self, request, provider):
        if provider not in ("google", "apple"):
            return Response({"error": "That social sign-in option is not supported."}, status=400)
        serializer = NativeStartSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({"error": "Choose a role and sign-in action to continue.", "fields": serializer.errors}, status=400)
        data = serializer.validated_data
        try:
            config = provider_config(provider, "native")
            transaction = create_transaction(
                provider=provider,
                platform="native",
                intent=data["intent"],
                role=data.get("role"),
            )
            result = {
                "provider": provider,
                "intent": data["intent"],
                "state": transaction["state"],
                "nonce": transaction["nonce"],
                "client_id": config["client_id"],
                "expires_in": 600,
            }
            if provider == "google":
                result["code_challenge"] = code_challenge(transaction["code_verifier"])
                result["code_challenge_method"] = "S256"
                result["authorization_url"] = authorization_url(
                    provider=provider,
                    platform="native",
                    transaction=transaction,
                )
            return Response(result, status=200)
        except SocialAuthError as exc:
            return Response(exc.as_payload(), status=exc.status_code)


class WebSocialStartView(NoStoreAPIView):
    """Create a server-held browser OAuth transaction for an API-consuming browser client."""

    permission_classes = [AnonymousOnly]
    throttle_classes = [AnonRateThrottle]

    @extend_schema(
        tags=["Social authentication"],
        request=WebStartSerializer,
        responses={
            200: inline_serializer(
                name="WebSocialStartResponse",
                fields={
                    "provider": serializers.CharField(),
                    "intent": serializers.CharField(),
                    "state": serializers.CharField(),
                    "authorization_url": serializers.URLField(),
                    "expires_in": serializers.IntegerField(),
                },
            ),
            400: OpenApiResponse(description="The provider or social sign-in request is invalid."),
            503: OpenApiResponse(description="The identity provider is temporarily unavailable."),
        },
    )
    def post(self, request, provider):
        if provider not in ("google", "apple"):
            return Response({"error": "That social sign-in option is not supported."}, status=400)
        serializer = WebStartSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({"error": "Choose a role and sign-in action to continue.", "fields": serializer.errors}, status=400)
        data = serializer.validated_data
        try:
            provider_config(provider, "web")
            transaction = create_transaction(
                provider=provider,
                platform="web",
                intent=data["intent"],
                role=data.get("role"),
                first_name=data.get("first_name", ""),
                last_name=data.get("last_name", ""),
                browser_binding=data["browser_binding"],
            )
            response = Response(
                {
                    "provider": provider,
                    "intent": data["intent"],
                    "state": transaction["state"],
                    "authorization_url": authorization_url(
                        provider=provider,
                        platform="web",
                        transaction=transaction,
                    ),
                    "expires_in": 600,
                },
                status=200,
            )
            response["Cache-Control"] = "no-store"
            response["Referrer-Policy"] = "no-referrer"
            return response
        except SocialAuthError as exc:
            return Response(exc.as_payload(), status=exc.status_code)


class WebSocialCompleteView(NoStoreAPIView):
    """Exchange a browser callback code; provider verification and account creation stay in this API."""

    permission_classes = [AnonymousOnly]
    throttle_classes = [AnonRateThrottle]

    @extend_schema(
        tags=["Social authentication"],
        request=WebCompleteSerializer,
        responses={
            200: LoginResponseSerializer,
            201: RegisterResponseSerializer,
            400: OpenApiResponse(description="The provider callback or identity is invalid."),
            403: OpenApiResponse(description="The provider account is not eligible to sign in."),
            409: OpenApiResponse(description="The provider identity conflicts with an existing account."),
        },
    )
    def post(self, request, provider):
        if provider not in ("google", "apple"):
            return Response({"error": "That social sign-in option is not supported."}, status=400)
        serializer = WebCompleteSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({"error": "The provider response is incomplete.", "fields": serializer.errors}, status=400)
        data = serializer.validated_data
        try:
            transaction = consume_transaction(
                state=data["state"],
                provider=provider,
                platform="web",
                browser_binding=data["browser_binding"],
            )
            identity = exchange_authorization_code(
                provider=provider,
                platform="web",
                code=data["authorization_code"],
                transaction=transaction,
            )
            if provider == "apple":
                apple_first, apple_last = apple_user_names(data.get("user", ""))
                identity["first_name"] = identity.get("first_name") or apple_first
                identity["last_name"] = identity.get("last_name") or apple_last

            if transaction["intent"] == "register":
                result = begin_social_registration(
                    provider=provider,
                    identity=identity,
                    role=transaction["role"],
                    first_name=transaction.get("first_name", ""),
                    last_name=transaction.get("last_name", ""),
                )
                response = Response(result["payload"], status=result["status"])
            else:
                payload, _user, _role = social_login(provider=provider, identity=identity, request=request)
                response = Response(payload, status=200)
            response["Cache-Control"] = "no-store"
            response["Referrer-Policy"] = "no-referrer"
            return response
        except SocialAuthError as exc:
            response = Response(exc.as_payload(), status=exc.status_code)
            response["Cache-Control"] = "no-store"
            response["Referrer-Policy"] = "no-referrer"
            return response


class NativeSocialCompleteView(NoStoreAPIView):
    permission_classes = [AnonymousOnly]
    throttle_classes = [AnonRateThrottle]

    @extend_schema(
        tags=["Social authentication"],
        request=NativeCompleteSerializer,
        responses={
            200: LoginResponseSerializer,
            201: RegisterResponseSerializer,
            400: OpenApiResponse(description="The provider callback or identity is invalid."),
            403: OpenApiResponse(description="The provider account is not eligible to sign in."),
            409: OpenApiResponse(description="The provider identity conflicts with an existing account."),
        },
    )
    def post(self, request, provider):
        if provider not in ("google", "apple"):
            return Response({"error": "That social sign-in option is not supported."}, status=400)
        serializer = NativeCompleteSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({"error": "The provider response is incomplete.", "fields": serializer.errors}, status=400)
        data = serializer.validated_data
        try:
            transaction = consume_transaction(
                state=data["state"],
                provider=provider,
                platform="native",
            )
            identity = exchange_authorization_code(
                provider=provider,
                platform="native",
                code=data["authorization_code"],
                transaction=transaction,
            )
            if transaction["intent"] == "register":
                result = begin_social_registration(
                    provider=provider,
                    identity=identity,
                    role=transaction["role"],
                    first_name=data.get("first_name", ""),
                    last_name=data.get("last_name", ""),
                )
                return Response(result["payload"], status=result["status"])

            payload, _user, _role = social_login(provider=provider, identity=identity, request=request)
            return Response(payload, status=200)
        except SocialAuthError as exc:
            return Response(exc.as_payload(), status=exc.status_code)


class NativeSocialCompleteProfileView(NoStoreAPIView):
    permission_classes = [AnonymousOnly]
    throttle_classes = [AnonRateThrottle]

    @extend_schema(
        tags=["Social authentication"],
        request=CompleteProfileSerializer,
        responses={
            201: OpenApiResponse(response=OpenApiTypes.OBJECT, description="Pending registration profile completed."),
            400: OpenApiResponse(description="Required profile details are invalid."),
            404: OpenApiResponse(description="The pending social registration is unavailable."),
        },
    )
    def post(self, request):
        serializer = CompleteProfileSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({"error": "Enter the missing name details.", "fields": serializer.errors}, status=400)
        try:
            payload = complete_pending_profile(**serializer.validated_data)
            return Response(payload, status=201)
        except SocialAuthError as exc:
            return Response(exc.as_payload(), status=exc.status_code)
