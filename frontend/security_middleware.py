"""Security headers for the standalone browser project (kept separate from the API project)."""

from django.conf import settings


class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.setdefault("X-Content-Type-Options", "nosniff")
        response.setdefault("Referrer-Policy", settings.SECURE_REFERRER_POLICY)
        if getattr(settings, "SECURE_CSP_ENABLED", False):
            response.setdefault("Content-Security-Policy", settings.SECURE_CSP)
        return response
