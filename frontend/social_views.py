"""Thin browser bridge to the API's Google and Apple authorization-code endpoints.

This app does not exchange provider codes, validate identity tokens, create users, or persist
social identities. Those operations remain exclusively in artisan_service/social_auth/.
"""

import hmac
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from django.contrib import messages
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST

from .api import APIClient, APIError
from .social_forms import SocialProfileForm
from .views import redirect_after_login, safe_internal_next

PROVIDERS = {"google", "apple"}
STATE_TTL_SECONDS = 600
FLOW_SESSION_KEYS = (
    "social_auth_state",
    "social_auth_provider",
    "social_auth_intent",
    "social_auth_browser_binding",
    "social_auth_started_at",
    "social_auth_next",
    "social_auth_first_name",
    "social_auth_last_name",
)


def _auth_url(intent):
    return reverse("register" if intent == "register" else "login")


def _safe_next(request, target):
    return safe_internal_next(request, target) or reverse("dashboard")


def _clear_flow(request):
    for key in FLOW_SESSION_KEYS:
        request.session.pop(key, None)
    request.session.modified = True


def _no_store(response):
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "no-referrer"
    return response


def _trusted_authorization_url(provider, value, expected_state):
    try:
        parsed = urlsplit(str(value))
        returned_states = parse_qs(parsed.query).get("state", [])
    except ValueError:
        return False
    allowed_host = "accounts.google.com" if provider == "google" else "appleid.apple.com"
    return (
        parsed.scheme == "https"
        and parsed.hostname == allowed_host
        and not parsed.username
        and not parsed.password
        and not parsed.fragment
        and returned_states == [expected_state]
    )


def _flow_is_current(request, provider, returned_state):
    state = request.session.get("social_auth_state", "")
    created_at = request.session.get("social_auth_started_at", 0)
    try:
        age = time.time() - float(created_at)
    except (TypeError, ValueError):
        age = STATE_TTL_SECONDS + 1
    return (
        request.session.get("social_auth_provider") == provider
        and isinstance(state, str)
        and isinstance(returned_state, str)
        and bool(state)
        and bool(returned_state)
        and hmac.compare_digest(state, returned_state)
        and 0 <= age <= STATE_TTL_SECONDS
        and bool(request.session.get("social_auth_browser_binding"))
    )


def _registration_result(request, payload):
    email = str(payload.get("email") or "").strip()
    action = payload.get("action")
    if action == "collect_profile_details":
        registration_token = payload.get("registration_token")
        if not registration_token:
            messages.error(request, "The provider profile is incomplete. Please start registration again.")
            return redirect("register")
        request.session["social_registration_token"] = registration_token
        request.session["social_registration_email"] = email
        request.session["social_registration_first_name"] = str(payload.get("first_name") or "")
        request.session["social_registration_last_name"] = str(payload.get("last_name") or "")
        request.session.modified = True
        messages.info(request, payload.get("message", "Add the missing name details to finish registration."))
        return redirect("social-api-profile")

    if action == "redirect_to_otp_verification" and email:
        request.session["pending_email"] = email
        request.session.modified = True
        messages.success(request, payload.get("message", "Check your email for the verification code."))
        return redirect(f"{reverse('verify-otp')}?{urlencode({'email': email})}")

    messages.error(request, "The API returned an unexpected registration response. Please try again.")
    return redirect("register")


@require_POST
def social_auth_start(request, provider):
    """Ask the API to start; then redirect the browser to the provider URL it returns."""
    if provider not in PROVIDERS:
        messages.error(request, "That social sign-in option is not supported.")
        return redirect("register")
    if request.session.get("api_access"):
        messages.info(request, "You are already signed in.")
        return _no_store(redirect("dashboard"))

    intent = request.POST.get("intent", "register")
    if intent not in {"register", "login"}:
        messages.error(request, "Choose a valid sign-in action.")
        return redirect("register")
    role = request.POST.get("role", "") if intent == "register" else ""
    if intent == "register" and role not in {"customer", "artisan"}:
        messages.error(request, "Choose customer or artisan before continuing with a provider.")
        return redirect("register")

    binding = secrets.token_urlsafe(32)
    data = {
        "intent": intent,
        "browser_binding": binding,
    }
    if intent == "register":
        data.update(
            {
                "role": role,
                "first_name": request.POST.get("first_name", "")[:40],
                "last_name": request.POST.get("last_name", "")[:40],
            }
        )

    try:
        _, payload, _ = APIClient(request).post(f"/social-auth/{provider}/web/start/", data)
    except APIError as exc:
        _clear_flow(request)
        message = exc.payload.get("error") if isinstance(exc.payload, dict) else str(exc)
        messages.error(request, message or "The API could not start provider sign-in.")
        return _no_store(redirect(_auth_url(intent)))

    state = payload.get("state") if isinstance(payload, dict) else None
    authorization_url = payload.get("authorization_url") if isinstance(payload, dict) else None
    if (
        not isinstance(state, str)
        or len(state) < 20
        or not _trusted_authorization_url(provider, authorization_url, state)
    ):
        _clear_flow(request)
        messages.error(request, "The API returned an invalid provider authorization response.")
        return _no_store(redirect(_auth_url(intent)))

    request.session["social_auth_state"] = state
    request.session["social_auth_provider"] = provider
    request.session["social_auth_intent"] = intent
    request.session["social_auth_browser_binding"] = binding
    request.session["social_auth_started_at"] = time.time()
    request.session["social_auth_next"] = _safe_next(request, request.POST.get("next", "")) if intent == "login" else ""
    request.session["social_auth_first_name"] = data.get("first_name", "")
    request.session["social_auth_last_name"] = data.get("last_name", "")
    request.session.modified = True
    return _no_store(redirect(authorization_url))


@csrf_exempt
@require_http_methods(["GET", "POST"])
def social_auth_callback(request, provider):
    """Forward only the one-use authorization code to the API after browser/session checks."""
    if provider not in PROVIDERS:
        return _no_store(redirect("register"))
    if (provider == "google" and request.method != "GET") or (provider == "apple" and request.method != "POST"):
        _clear_flow(request)
        messages.error(request, "The provider returned a response in an unexpected format.")
        return _no_store(redirect("register"))

    params = request.POST if request.method == "POST" else request.GET
    state = params.get("state", "")
    intent = request.session.get("social_auth_intent", "register")
    if intent not in {"register", "login"}:
        intent = "register"
    if not _flow_is_current(request, provider, state):
        _clear_flow(request)
        messages.error(request, "The social sign-in expired or could not be verified. Please start again.")
        return _no_store(redirect(_auth_url(intent)))

    provider_error = params.get("error", "")
    if provider_error:
        _clear_flow(request)
        messages.info(request, "Social sign-in was cancelled. You can try again or use email registration.")
        return _no_store(redirect(_auth_url(intent)))

    code = params.get("code", "")
    if not code:
        _clear_flow(request)
        messages.error(request, "The identity provider did not return an authorization code.")
        return _no_store(redirect(_auth_url(intent)))

    binding = request.session.get("social_auth_browser_binding", "")
    apple_user = params.get("user", "") if provider == "apple" else ""
    data = {
        "state": state,
        "authorization_code": code,
        "browser_binding": binding,
    }
    if apple_user:
        data["user"] = apple_user
    next_path = request.session.get("social_auth_next", "")

    try:
        _, payload, _ = APIClient(request).post(f"/social-auth/{provider}/web/complete/", data)
    except APIError as exc:
        _clear_flow(request)
        if (
            exc.status_code == 403
            and isinstance(exc.payload, dict)
            and exc.payload.get("action") == "redirect_to_otp_verification"
            and exc.payload.get("email")
        ):
            email = str(exc.payload["email"])
            request.session["pending_email"] = email
            request.session.modified = True
            messages.info(request, str(exc))
            return _no_store(redirect(f"{reverse('verify-otp')}?{urlencode({'email': email})}"))
        message = exc.payload.get("error") if isinstance(exc.payload, dict) else str(exc)
        messages.error(request, message or "The API could not complete provider sign-in.")
        return _no_store(redirect(_auth_url(intent)))

    _clear_flow(request)
    if intent == "register":
        return _no_store(_registration_result(request, payload if isinstance(payload, dict) else {}))

    if isinstance(payload, dict) and payload.get("mfa_required"):
        from .views import _start_staff_mfa_pending

        _start_staff_mfa_pending(request, payload)
        safe_next = safe_internal_next(request, next_path)
        if safe_next:
            request.session["login_next"] = safe_next
            request.session.modified = True
        messages.info(request, "Enter the one-time code sent to your registered staff email.")
        return _no_store(redirect("staff-mfa"))

    from .views import _store_authenticated_api_session

    if not _store_authenticated_api_session(request, payload if isinstance(payload, dict) else {}):
        messages.error(request, "The API did not return a complete sign-in session.")
        return _no_store(redirect("login"))
    request.session.pop("pending_email", None)
    safe_next = safe_internal_next(request, next_path)
    if safe_next:
        request.session["login_next"] = safe_next
    request.session.modified = True
    messages.success(request, payload.get("message", "You are signed in."))
    return _no_store(redirect_after_login(request))


@require_http_methods(["GET", "POST"])
def social_auth_profile(request):
    """Collect missing names and send them to the API's profile-completion endpoint."""
    if request.session.get("api_access"):
        messages.info(request, "You are already signed in.")
        return _no_store(redirect("dashboard"))
    token = request.session.get("social_registration_token", "")
    email = request.session.get("social_registration_email", "")
    if not token:
        for key in ("social_registration_email", "social_registration_first_name", "social_registration_last_name"):
            request.session.pop(key, None)
        messages.error(request, "The social registration details expired. Please start again.")
        return _no_store(redirect("register"))

    form = SocialProfileForm(
        request.POST or None,
        initial={
            "first_name": request.session.get("social_registration_first_name", ""),
            "last_name": request.session.get("social_registration_last_name", ""),
        },
    )
    if request.method == "POST" and form.is_valid():
        try:
            _, payload, _ = APIClient(request).post(
                "/social-auth/complete-profile/",
                {
                    "registration_token": token,
                    "first_name": form.cleaned_data["first_name"],
                    "last_name": form.cleaned_data["last_name"],
                },
            )
        except APIError as exc:
            field_errors = {}
            if isinstance(exc.payload, dict):
                field_errors = exc.payload.get("fields") or {
                    key: value for key, value in exc.payload.items() if key in form.fields
                }
            if exc.status_code == 400 and field_errors:
                for field, errors in field_errors.items():
                    if field in form.fields:
                        for error in errors if isinstance(errors, list) else [errors]:
                            form.add_error(field, error)
            else:
                message = exc.payload.get("error") if isinstance(exc.payload, dict) else str(exc)
                messages.error(request, message or "The API could not finish the profile.")
            expired_profile = (
                isinstance(exc.payload, dict)
                and "expired" in str(exc.payload.get("error", "")).lower()
            )
            if exc.status_code in {404, 410} or expired_profile:
                for key in (
                    "social_registration_token",
                    "social_registration_email",
                    "social_registration_first_name",
                    "social_registration_last_name",
                ):
                    request.session.pop(key, None)
                return _no_store(redirect("register"))
        else:
            for key in (
                "social_registration_token",
                "social_registration_email",
                "social_registration_first_name",
                "social_registration_last_name",
            ):
                request.session.pop(key, None)
            email = payload.get("email", email)
            request.session["pending_email"] = email
            request.session.modified = True
            messages.success(request, payload.get("message", "Your registration has started. Verify your email to continue."))
            return _no_store(redirect(f"{reverse('verify-otp')}?{urlencode({'email': email})}"))

    response = render(request, "auth/social_profile.html", {"form": form, "social_email": email})
    return _no_store(response)
