"""HTTP cache policy for responses containing authentication or recovery state."""

from rest_framework.views import APIView


class NoStoreResponseMixin:
    """Prevent browsers and shared proxies from caching credentials or identity-flow data."""

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "no-store, private"
        response["Pragma"] = "no-cache"
        response["Referrer-Policy"] = "no-referrer"
        return response


class NoStoreAPIView(NoStoreResponseMixin, APIView):
    """APIView base class for identity, OTP, MFA, and credential-recovery endpoints."""
