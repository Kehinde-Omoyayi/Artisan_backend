"""Browser redirect/callback routes for Google and Sign in with Apple.

Apple returns its authorization result using a cross-site form_post, so only the
callback is CSRF-exempt. It validates a short-lived, one-use state held server-side.
"""

import secrets
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST

from .exceptions import SocialAuthError
from .forms import SocialProfileForm
from .oauth import (
    apple_user_names,
    authorization_url,
    consume_transaction,
    create_transaction,
    exchange_authorization_code,
    provider_config,
    STATE_TTL_SECONDS,
)
from .services import (
    begin_social_registration,
    complete_pending_profile,
    pending_profile_details,
    role_for_user,
    social_login,
)

BROWSER_BINDING_COOKIE = "artisan_social_auth_browser"


def _browser_binding(request):
    binding = request.COOKIES.get(BROWSER_BINDING_COOKIE, "")
    if not isinstance(binding, str) or not binding or len(binding) > 512:
        binding = secrets.token_urlsafe(32)
    return binding


def _set_browser_binding(response, request, binding):
    secure = bool(
        request.is_secure()
        or getattr(settings, "SESSION_COOKIE_SECURE", False)
        or getattr(settings, "ENVIRONMENT", "") == "production"
    )
    response.set_cookie(
        BROWSER_BINDING_COOKIE,
        binding,
        max_age=STATE_TTL_SECONDS,
        httponly=True,
        secure=secure,
        samesite="None" if secure else "Lax",
        path="/",
    )
    return response


def _return_to_auth(request, intent, *, level="error", message=None):
    route_name = "register" if intent == "register" else "login"
    if message:
        getattr(messages, level)(request, message)
    return redirect(reverse(route_name))


def _safe_next(request, target):
    if target and url_has_allowed_host_and_scheme(
        target,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return target
    return reverse("dashboard")


def _no_store(response):
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "no-referrer"
    return response


def _verify_otp_url(email):
    return f"{reverse('verify-otp')}?{urlencode({'email': email})}"


@require_POST
def social_web_start(request, provider):
    if request.session.get("api_access"):
        messages.info(request, "You are already signed in.")
        return _no_store(redirect(reverse("dashboard")))
    if provider not in ("google", "apple"):
        return _return_to_auth(request, "register", message="That social sign-in option is not supported.")
    intent = request.POST.get("intent", "register")
    if intent not in ("register", "login"):
        return _return_to_auth(request, "register", message="Choose a valid sign-in action.")
    role = request.POST.get("role", "") if intent == "register" else ""
    next_path = _safe_next(request, request.POST.get("next", "")) if intent == "login" else ""
    binding = _browser_binding(request)
    try:
        provider_config(provider, "web")
        transaction = create_transaction(
            provider=provider,
            platform="web",
            intent=intent,
            role=role,
            first_name=request.POST.get("first_name", ""),
            last_name=request.POST.get("last_name", ""),
            next_path=next_path,
            browser_binding=binding,
        )
        target = authorization_url(provider=provider, platform="web", transaction=transaction)
        return _set_browser_binding(_no_store(redirect(target)), request, binding)
    except SocialAuthError as exc:
        return _return_to_auth(request, intent, message=exc.message)


@csrf_exempt
@require_http_methods(["GET", "POST"])
def social_web_callback(request, provider):
    """Handle Google query callbacks and Apple's form_post response."""
    if provider not in ("google", "apple"):
        return _no_store(_return_to_auth(request, "register", message="That social sign-in option is not supported."))
    if (provider == "apple" and request.method != "POST") or (provider == "google" and request.method != "GET"):
        return _no_store(_return_to_auth(request, "register", message="The provider returned a response in an unexpected format."))
    params = request.POST if request.method == "POST" else request.GET
    state = params.get("state", "")
    error = params.get("error", "")
    intent = "register"
    try:
        transaction = consume_transaction(
            state=state,
            provider=provider,
            platform="web",
            browser_binding=request.COOKIES.get(BROWSER_BINDING_COOKIE, ""),
        )
        intent = transaction["intent"]
        if request.session.get("api_access"):
            messages.info(request, "You are already signed in.")
            return _no_store(redirect(reverse("dashboard")))
        if error:
            return _no_store(_return_to_auth(request, intent, level="info", message="Social sign-in was cancelled. You can try again or use email registration."))
        code = params.get("code", "")
        if not code:
            raise SocialAuthError("The identity provider did not return an authorization code.")
        identity = exchange_authorization_code(
            provider=provider,
            platform="web",
            code=code,
            transaction=transaction,
        )
        if provider == "apple":
            apple_first, apple_last = apple_user_names(params.get("user", ""))
            identity["first_name"] = identity.get("first_name") or apple_first
            identity["last_name"] = identity.get("last_name") or apple_last

        if intent == "register":
            result = begin_social_registration(
                provider=provider,
                identity=identity,
                role=transaction["role"],
                first_name=transaction.get("first_name", ""),
                last_name=transaction.get("last_name", ""),
            )
            payload = result["payload"]
            email = payload.get("email", identity.get("email", ""))
            request.session["pending_email"] = email
            if payload.get("action") == "collect_profile_details":
                request.session["social_registration_token"] = payload["registration_token"]
                request.session["social_registration_email"] = email
                messages.info(request, payload.get("message", "Add your name to finish registration."))
                return _no_store(redirect(reverse("social-auth-profile")))
            messages.success(request, payload.get("message", "Your account registration has started."))
            return _no_store(redirect(_verify_otp_url(email)))

        payload, user, role = social_login(provider=provider, identity=identity, request=request)
        request.session["api_access"] = payload["access"]
        request.session["api_refresh"] = payload["refresh"]
        request.session["api_user"] = payload["user"]
        request.session["api_role"] = role
        request.session.modified = True
        messages.success(request, payload["message"])
        return _no_store(redirect(_safe_next(request, transaction.get("next_path", ""))))
    except SocialAuthError as exc:
        if exc.action == "redirect_to_otp_verification" and exc.fields.get("email"):
            email = exc.fields["email"]
            request.session["pending_email"] = email
            messages.info(request, exc.message)
            return _no_store(redirect(_verify_otp_url(email)))
        return _no_store(_return_to_auth(request, intent, message=exc.message))


@require_http_methods(["GET", "POST"])
def social_web_profile(request):
    if request.session.get("api_access"):
        messages.info(request, "You are already signed in.")
        return _no_store(redirect(reverse("dashboard")))
    token = request.session.get("social_registration_token", "")
    pending = pending_profile_details(token)
    if not pending:
        request.session.pop("social_registration_token", None)
        request.session.pop("social_registration_email", None)
        messages.error(request, "The social registration details expired. Please start again.")
        return redirect(reverse("register"))

    form = SocialProfileForm(
        request.POST or None,
        initial={
            "first_name": pending["first_name"],
            "last_name": pending["last_name"],
        },
    )
    if request.method == "POST" and form.is_valid():
        try:
            payload = complete_pending_profile(
                registration_token=token,
                first_name=form.cleaned_data["first_name"],
                last_name=form.cleaned_data["last_name"],
            )
            email = payload["email"]
            request.session.pop("social_registration_token", None)
            request.session.pop("social_registration_email", None)
            request.session["pending_email"] = email
            messages.success(request, payload["message"])
            return _no_store(redirect(_verify_otp_url(email)))
        except SocialAuthError as exc:
            for field, errors in exc.fields.items():
                if field in form.fields:
                    for error in errors:
                        form.add_error(field, error)
            if not exc.fields:
                messages.error(request, exc.message)

    response = render(
        request,
        "social_auth/profile.html",
        {"form": form, "social_email": pending["email"]},
    )
    return _no_store(response)
