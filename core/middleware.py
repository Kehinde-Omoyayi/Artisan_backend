import hashlib
import logging
import time
import uuid
import ipaddress

from django.conf import settings
from django.http import HttpResponseRedirect, JsonResponse
from django.urls import reverse
from django.utils.deprecation import MiddlewareMixin

logger = logging.getLogger("marketplace.http")


def _hash(value): return hashlib.sha256(str(value or "").encode("utf-8", "ignore")).hexdigest() if value else ""


class RequestContextMiddleware(MiddlewareMixin):
    def process_request(self, request):
        raw = request.headers.get("X-Request-ID", "").strip()
        try: request.request_id = str(uuid.UUID(raw)) if raw else str(uuid.uuid4())
        except ValueError: request.request_id = str(uuid.uuid4())
        remote_ip = request.META.get("REMOTE_ADDR", "")
        trusted = False
        try:
            parsed_ip = ipaddress.ip_address(remote_ip)
            trusted = any(parsed_ip in ipaddress.ip_network(value, strict=False) for value in getattr(settings, "TRUSTED_PROXY_IPS", []) if value)
        except ValueError: trusted = False
        request.trusted_proxy = trusted
        request._request_started_at = time.perf_counter()
        if getattr(settings, "EXPOSE_DB_QUERY_METRICS", False):
            from django.db import connection
            from django.test.utils import CaptureQueriesContext
            request._query_context = CaptureQueriesContext(connection); request._query_context.__enter__()

    def process_response(self, request, response):
        request_id = getattr(request, "request_id", str(uuid.uuid4()))
        response["X-Request-ID"] = request_id
        query_context = getattr(request, "_query_context", None)
        if query_context is not None:
            try:
                query_context.__exit__(None, None, None); response["X-DB-Queries"] = str(len(query_context))
            except Exception:
                logger.exception("db_query_metric_capture_failed request_id=%s", request_id)
        started = getattr(request, "_request_started_at", None)
        if started is not None:
            elapsed_ms = (time.perf_counter() - started) * 1000
            response["X-Response-Time-ms"] = f"{elapsed_ms:.2f}"
            threshold = getattr(settings, "SLOW_REQUEST_THRESHOLD_MS", 200)
            if elapsed_ms > threshold: logger.warning("slow_request request_id=%s method=%s path=%s status=%s elapsed_ms=%.2f", request_id, request.method, request.path, response.status_code, elapsed_ms, extra={"request_id": request_id, "elapsed_ms": round(elapsed_ms, 2)})
        return response


class RequestBodyGuardMiddleware(MiddlewareMixin):
    def process_request(self, request):
        limit = getattr(settings, "JSON_BODY_LIMIT_BYTES", 256 * 1024)
        content_length = request.META.get("CONTENT_LENGTH")
        content_type = request.META.get("CONTENT_TYPE", "").split(";", 1)[0].strip().lower()
        if content_length and content_type == "application/json":
            try:
                if int(content_length) > limit: return JsonResponse({"detail": "Request payload is too large."}, status=413)
            except (TypeError, ValueError): return JsonResponse({"detail": "Invalid request length."}, status=400)


class SecurityObservabilityMiddleware(MiddlewareMixin):
    def process_response(self, request, response):
        response.setdefault("X-Content-Type-Options", "nosniff")
        response.setdefault("Referrer-Policy", getattr(settings, "SECURE_REFERRER_POLICY", "strict-origin-when-cross-origin"))
        if getattr(settings, "SECURE_CSP_ENABLED", False): response.setdefault("Content-Security-Policy", settings.SECURE_CSP)
        return response


class StaffAdminPasswordMiddleware(MiddlewareMixin):
    """Keep Django administration available while requiring expired staff passwords to change first."""

    def process_view(self, request, view_func, view_args, view_kwargs):
        user = getattr(request, "user", None)
        if not (
            request.path.startswith("/admin/")
            and user
            and user.is_authenticated
            and (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False))
        ):
            return None

        from home.password_policy import staff_password_change_required

        if not staff_password_change_required(user):
            return None

        allowed_paths = {"/admin/password_change/", "/admin/logout/", "/admin/mfa/"}
        if request.path in allowed_paths:
            return None
        return HttpResponseRedirect(reverse("admin:password_change"))


class StaffAdminMFAMiddleware(MiddlewareMixin):
    """Require recent staff MFA and registered recovery questions for Django admin work."""

    EXEMPT_PATHS = {
        "/admin/login/",
        "/admin/logout/",
        "/admin/mfa/",
        "/admin/jsi18n/",
    }

    def process_view(self, request, view_func, view_args, view_kwargs):
        if not request.path.startswith("/admin/") or request.path in self.EXEMPT_PATHS:
            return None
        user = getattr(request, "user", None)
        if not (
            user
            and getattr(user, "is_authenticated", False)
            and (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False))
        ):
            return None

        from home.password_policy import staff_security_questions_required

        if staff_security_questions_required(user):
            from django.contrib.auth import logout

            logout(request)
            return HttpResponseRedirect("/auth/login/?next=%2Fadmin%2F")

        from home.admin_mfa import admin_mfa_is_current, ensure_admin_mfa_challenge
        from rest_framework.exceptions import Throttled

        if admin_mfa_is_current(request.session, user):
            return None
        try:
            ensure_admin_mfa_challenge(request)
        except Throttled as exc:
            detail = getattr(exc, "detail", "Too many staff sign-in codes were requested. Wait before trying again.")
            return JsonResponse({"detail": str(detail)}, status=429)
        return HttpResponseRedirect(reverse("admin-staff-mfa"))

    def process_response(self, request, response):
        if request.path == "/admin" or request.path.startswith("/admin/"):
            response["Cache-Control"] = "no-store, private"
            response["Pragma"] = "no-cache"
            response["Referrer-Policy"] = "same-origin"
        return response
