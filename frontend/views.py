from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
from functools import wraps
import hashlib
import hmac
import math
import time
from zoneinfo import ZoneInfo
from urllib.parse import unquote, urlencode, urlparse
from uuid import UUID

from django.conf import settings
from django.contrib import messages
from django.http import Http404, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .api import (
    APIClient,
    APIError,
    extract_items,
    flatten_errors,
    payload_message,
    replace_path,
    new_idempotency_key,
)
from .forms import *
from .phone_fields import is_valid_phone

LANDING_IMAGE_ASSETS = {
    "carpentry": (
        "images/landing/trade-carpentry-640.webp",
        "images/landing/trade-carpentry-1440.webp",
    ),
    "electrical": (
        "images/landing/trade-electrical-640.webp",
        "images/landing/trade-electrical-1440.webp",
    ),
    "electronics": (
        "images/landing/trade-electronics-640.webp",
        "images/landing/trade-electronics-1440.webp",
    ),
    "tailoring": (
        "images/landing/trade-tailoring-640.webp",
        "images/landing/trade-tailoring-1440.webp",
    ),
    "painting": (
        "images/landing/trade-painting-640.webp",
        "images/landing/trade-painting-1440.webp",
    ),
    "plumbing": (
        "images/landing/trade-plumbing-640.webp",
        "images/landing/trade-plumbing-1440.webp",
    ),
}

# Preserve the existing editorial carousel and image cards. These six images are
# illustrative homepage themes, never substitutes for API-backed live listings.
LANDING_VISUAL_THEMES = (
    {
        "name": "Carpentry",
        "title": "Carpentry, in focus.",
        "description": "Timber and workshop craft shown as an illustrative theme, not a live service listing.",
        "image_640": LANDING_IMAGE_ASSETS["carpentry"][0],
        "image_1440": LANDING_IMAGE_ASSETS["carpentry"][1],
    },
    {
        "name": "Electrical",
        "title": "Electrical work, in focus.",
        "description": "An editorial image for electrical work; browse current service listings in the public catalogue.",
        "image_640": LANDING_IMAGE_ASSETS["electrical"][0],
        "image_1440": LANDING_IMAGE_ASSETS["electrical"][1],
    },
    {
        "name": "Electronics",
        "title": "Electronics, in focus.",
        "description": "A device-repair scene used for illustration, not a provider profile or live listing.",
        "image_640": LANDING_IMAGE_ASSETS["electronics"][0],
        "image_1440": LANDING_IMAGE_ASSETS["electronics"][1],
    },
    {
        "name": "Tailoring",
        "title": "Tailoring, in focus.",
        "description": "Sewing and garment work shown as a visual theme, not a promise of availability.",
        "image_640": LANDING_IMAGE_ASSETS["tailoring"][0],
        "image_1440": LANDING_IMAGE_ASSETS["tailoring"][1],
    },
    {
        "name": "Painting",
        "title": "Painting, in focus.",
        "description": "An illustrative paintwork image; current service listings are available in the public catalogue.",
        "image_640": LANDING_IMAGE_ASSETS["painting"][0],
        "image_1440": LANDING_IMAGE_ASSETS["painting"][1],
    },
    {
        "name": "Plumbing",
        "title": "Plumbing, in focus.",
        "description": "A home-repair scene used for illustration, not a provider profile or live listing.",
        "image_640": LANDING_IMAGE_ASSETS["plumbing"][0],
        "image_1440": LANDING_IMAGE_ASSETS["plumbing"][1],
    },
)

PENDING_ACTION_SESSION_KEY = "pending_protected_action"
PAYMENT_NOTICE_SESSION_KEY = "payment_notice_pending"
PENDING_REQUEST_FORM_SESSION_KEY = "pending_service_request_form_values"
PENDING_REQUEST_UPDATE_FORM_SESSION_KEY = "pending_service_request_update_form_values"
PROFILE_COMPLETION_NEXT_SESSION_KEY = "profile_completion_next"
PENDING_ACTION_TTL_SECONDS = 30 * 60
ACTION_PROFILE_REQUIRED_FIELDS = (
    "first_name",
    "last_name",
    "phone_number",
    "country",
    "state",
    "area",
)
ACTION_PROFILE_FIELD_LABELS = {
    "first_name": "First name",
    "last_name": "Last name",
    "phone_number": "Phone number",
    "country": "Country",
    "state": "State or region",
    "area": "Area or locality",
}
SERVICE_REQUEST_FORM_FIELDS = (
    "category_name",
    "service_name",
    "location_country_filter",
    "location_state_filter",
    "location_area_filter",
    "preferred_date",
    "urgency",
    "budget",
    "description",
)
CONVERSATION_REQUEST_FIELDS = (
    "service_id",
    "category_id",
    "description",
    "location_id",
    "country",
    "state",
    "area",
    "preferred_date",
    "preferred_time_start",
    "preferred_time_end",
    "preferred_time_note",
    "urgency",
    "budget",
    "budget_unknown",
    "additional_details",
)


def safe_internal_next(request, value):
    """Return a strictly local path for redirects; reject absolute and encoded host paths."""
    value = str(value or "").strip()
    if (
        not value.startswith("/")
        or value.startswith("//")
        or any(char in value for char in ("\\", "\r", "\n"))
    ):
        return ""
    try:
        parsed = urlparse(value)
        decoded = unquote(value)
    except ValueError:
        return ""
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
        return ""
    if decoded.startswith("//") or any(char in decoded for char in ("\\", "\r", "\n")):
        return ""
    if not url_has_allowed_host_and_scheme(
        value,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return ""
    return value


def _service_request_resume_url():
    return f"{reverse('service-requests')}?resume=form"


def _safe_service_request_form_values(data):
    return {
        field: str(data.get(field, "") or "")[:10000]
        for field in SERVICE_REQUEST_FORM_FIELDS
        if field in data
    }


def _store_pending_action(request, kind, **values):
    current = request.session.get(PENDING_ACTION_SESSION_KEY)
    same_action = (
        isinstance(current, dict)
        and current.get("kind") == kind
        and current.get("artisan_id") == values.get("artisan_id")
    )
    action = {"kind": kind, **values}
    action["created_at"] = current.get("created_at") if same_action else time.time()
    action["idempotency_key"] = (
        current.get("idempotency_key")
        if same_action and current.get("idempotency_key")
        else new_idempotency_key(f"navigation-{kind}")
    )
    request.session[PENDING_ACTION_SESSION_KEY] = action
    request.session.modified = True
    return action


def _pending_action_target(request, action):
    if not isinstance(action, dict):
        return reverse("home")
    if action.get("kind") == "service_request_form":
        return _service_request_resume_url()
    if action.get("kind") == "service_conversation":
        try:
            artisan_id = UUID(str(action.get("artisan_id", "")))
        except (TypeError, ValueError, AttributeError):
            return reverse("artisans")
        detail_path = reverse("artisan-detail", kwargs={"pk": artisan_id})
        return_to = safe_internal_next(request, action.get("return_to"))
        if return_to and urlparse(return_to).path == detail_path:
            return return_to
        return detail_path
    if action.get("kind") == "conversation_message":
        try:
            return reverse(
                "conversation-detail",
                kwargs={
                    "conversation_id": UUID(str(action.get("conversation_id", "")))
                },
            )
        except (TypeError, ValueError, AttributeError):
            return reverse("conversation-inbox")
    if action.get("kind") == "service_request_update":
        request_id = str(action.get("service_request_id") or "")
        if request_id.isdigit():
            return f"{reverse('service-request-detail', kwargs={'pk': request_id})}?resume=form"
        return reverse("service-requests")
    return reverse("home")


def _artisan_detail_return_target(request, artisan_id):
    detail_path = reverse("artisan-detail", kwargs={"pk": artisan_id})
    candidate = safe_internal_next(request, request.POST.get("next"))
    if candidate and urlparse(candidate).path == detail_path:
        return candidate
    return detail_path


def _profile_route_for_role(role):
    if role == "customer":
        return "/profile/customer/personal/profile/", "customer-profile"
    if role == "artisan":
        return "/profile/artisan/personal/profile/", "artisan-profile"
    return "", ""


def _action_profile_missing_fields(profile):
    profile = profile if isinstance(profile, dict) else {}
    missing = [
        field
        for field in ("first_name", "last_name", "country", "state", "area")
        if not str(profile.get(field) or "").strip()
    ]
    if not is_valid_phone(str(profile.get("phone_number") or "")):
        missing.insert(2, "phone_number")
    return missing


def _action_profile_is_complete(profile):
    return not _action_profile_missing_fields(profile)


def _action_profile_health_summary(profile):
    missing = _action_profile_missing_fields(profile)
    total = len(ACTION_PROFILE_FIELD_LABELS)
    completed = max(0, total - len(missing))
    return {
        "percent": int(round(completed * 100 / total)) if total else 100,
        "completed": completed,
        "total": total,
        "complete": not missing,
        "missing_labels": [ACTION_PROFILE_FIELD_LABELS[field] for field in missing],
    }


def _clear_pending_action(request, *, clear_form_values=False):
    request.session.pop(PENDING_ACTION_SESSION_KEY, None)
    request.session.pop(PROFILE_COMPLETION_NEXT_SESSION_KEY, None)
    if clear_form_values:
        request.session.pop(PENDING_REQUEST_FORM_SESSION_KEY, None)
    request.session.modified = True


def _login_redirect_for(request, destination):
    target = safe_internal_next(request, destination) or reverse("home")
    request.session["login_next"] = target
    request.session.modified = True
    return redirect(f"{reverse('login')}?{urlencode({'next': target})}")


def _profile_completion_redirect(request, role, destination, action, *, profile=None):
    profile_endpoint, profile_route = _profile_route_for_role(role)
    if not profile_endpoint:
        return redirect(destination)
    target = safe_internal_next(request, destination) or _pending_action_target(
        request, action
    )
    profile_url = reverse(profile_route)
    if target == profile_url or target.startswith(profile_url + "?"):
        target = _pending_action_target(request, action)
    request.session[PROFILE_COMPLETION_NEXT_SESSION_KEY] = target
    request.session.modified = True
    missing = [
        ACTION_PROFILE_FIELD_LABELS[field]
        for field in _action_profile_missing_fields(profile)
    ]
    messages.info(
        request,
        f"Complete these fields in your {role} profile before continuing: {', '.join(missing)}.",
    )
    return redirect(f"{profile_url}?{urlencode({'next': target})}")


def _ensure_action_profile_complete(request, action, *, role, destination):
    profile_endpoint, _profile_route = _profile_route_for_role(role)
    if not profile_endpoint:
        return None
    try:
        _, profile, _ = APIClient(request).get(profile_endpoint)
    except APIError as exc:
        handle_api_error(request, exc)
        if exc.status_code == 401:
            return _login_redirect_for(request, destination)
        return redirect(safe_internal_next(request, destination) or reverse("home"))
    if _action_profile_is_complete(profile):
        return None
    values = {
        key: value
        for key, value in action.items()
        if key not in {"kind", "created_at", "idempotency_key"}
    }
    _store_pending_action(request, action.get("kind", ""), **values)
    return _profile_completion_redirect(
        request, role, destination, action, profile=profile
    )


def _capture_anonymous_post(request, view_name, kwargs):
    if request.method != "POST":
        return safe_internal_next(request, request.get_full_path()) or reverse("home")
    if view_name == "start_conversation":
        try:
            artisan_id = UUID(str(kwargs.get("pk", "")))
        except (TypeError, ValueError, AttributeError):
            return reverse("artisans")
        detail_target = _artisan_detail_return_target(request, artisan_id)
        service_id = str(request.POST.get("service_id", "") or "").strip()
        action = {"artisan_id": str(artisan_id), "return_to": detail_target}
        if service_id.isdigit():
            action["service_id"] = service_id
        _store_pending_action(request, "service_conversation", **action)
        return detail_target
    if view_name == "service_requests":
        request.session[PENDING_REQUEST_FORM_SESSION_KEY] = (
            _safe_service_request_form_values(request.POST)
        )
        _store_pending_action(request, "service_request_form")
        request.session.modified = True
        return _service_request_resume_url()
    return safe_internal_next(request, request.get_full_path()) or reverse("home")


def _continue_pending_protected_action(request):
    action = request.session.get(PENDING_ACTION_SESSION_KEY)
    if not isinstance(action, dict):
        return None
    destination = _pending_action_target(request, action)
    try:
        created_at = float(action.get("created_at", 0))
    except (TypeError, ValueError):
        created_at = 0
    if created_at <= 0 or time.time() - created_at > PENDING_ACTION_TTL_SECONDS:
        _clear_pending_action(request, clear_form_values=True)
        messages.info(
            request,
            "That saved action expired. Please start again from the artisan or request page.",
        )
        return redirect(destination)

    kind = action.get("kind")
    role = current_role(request)
    if kind == "service_request_update":
        if role != "customer":
            _clear_pending_action(request, clear_form_values=True)
            messages.error(
                request, "Only customer accounts can update a service request."
            )
            return redirect(destination)
        completion = _ensure_action_profile_complete(
            request, action, role=role, destination=destination
        )
        if completion:
            return completion
        _clear_pending_action(request)
        messages.info(
            request,
            "Your profile is complete. Review your service request again and submit your changes.",
        )
        return redirect(destination)

    if kind == "conversation_message":
        if role not in {"customer", "artisan"}:
            _clear_pending_action(request)
            messages.error(request, "This account cannot send marketplace messages.")
            return redirect(destination)
        completion = _ensure_action_profile_complete(
            request, action, role=role, destination=destination
        )
        if completion:
            return completion
        _clear_pending_action(request)
        if action.get("had_attachments"):
            messages.info(
                request,
                "Your profile is complete. Reopen the conversation, reattach your files, and send your message.",
            )
            return redirect(destination)
        try:
            conversation_id = UUID(str(action.get("conversation_id", "")))
            APIClient(request).post(
                f"/conversations/{conversation_id}/messages/",
                {"body": str(action.get("body") or "")[:5000]},
                idempotency_key=action.get("idempotency_key")
                or new_idempotency_key("conversation-message"),
            )
        except (APIError, TypeError, ValueError) as exc:
            (
                handle_api_error(request, exc)
                if isinstance(exc, APIError)
                else messages.error(request, "The conversation could not be reopened.")
            )
            return redirect(destination)
        messages.success(request, "Your message was sent.")
        return redirect(destination)

    if kind == "service_request_form":
        if role != "customer":
            _clear_pending_action(request, clear_form_values=True)
            messages.error(
                request, "Service requests are available to customer accounts."
            )
            return redirect(reverse("service-requests"))
        completion = _ensure_action_profile_complete(
            request,
            action,
            role=role,
            destination=destination,
        )
        if completion:
            return completion
        request.session.pop(PENDING_ACTION_SESSION_KEY, None)
        request.session.pop(PROFILE_COMPLETION_NEXT_SESSION_KEY, None)
        request.session.modified = True
        messages.info(
            request,
            "Your request details were restored. Review them and submit when ready.",
        )
        return redirect(destination)

    if kind != "service_conversation":
        # Legacy direct-chat pending actions predate the request-first flow; they
        # cannot be replayed safely, so they expire like any stale saved action.
        _clear_pending_action(request, clear_form_values=True)
        messages.info(
            request,
            "That saved action is no longer available. Start from the artisan's request form instead.",
        )
        return redirect(reverse("home"))
    if role != "customer":
        _clear_pending_action(request)
        messages.error(
            request, "Only customer accounts can request an artisan service."
        )
        return redirect(destination)

    completion = _ensure_action_profile_complete(
        request,
        action,
        role=role,
        destination=destination,
    )
    if completion:
        return completion

    client = APIClient(request)
    try:
        payload = {"artisan_id": str(action["artisan_id"])}
        service_id = str(action.get("service_id") or "").strip()
        if service_id.isdigit():
            payload["service_id"] = service_id
        _, conversation, _ = client.post(
            "/conversations/start/",
            payload,
            idempotency_key=action.get("idempotency_key")
            or new_idempotency_key("conversation-start"),
        )
    except APIError as exc:
        _clear_pending_action(request)
        handle_api_error(request, exc)
        if exc.status_code == 401:
            return _login_redirect_for(request, destination)
        return redirect(destination)

    conversation_id = conversation.get("id") if isinstance(conversation, dict) else None
    if not conversation_id:
        _clear_pending_action(request)
        messages.error(
            request,
            "The conversation could not be opened. Please try again from the artisan profile.",
        )
        return redirect(destination)
    _clear_pending_action(request, clear_form_values=True)
    messages.success(request, "Your private conversation is ready.")
    return redirect("conversation-detail", conversation_id=conversation_id)


MANAGER_VALUES_REVEAL_SESSION_KEY = "manager_values_reveal_once"
MANAGER_VALUES_REVEAL_TTL_SECONDS = 120


def _consume_manager_values_reveal(request):
    payload = request.session.pop(MANAGER_VALUES_REVEAL_SESSION_KEY, None)
    request.session.modified = True
    if not isinstance(payload, dict):
        return ""
    try:
        expires_at = float(payload.get("expires_at") or 0)
    except (TypeError, ValueError):
        return ""
    if expires_at < timezone.now().timestamp() or payload.get("target") != request.path:
        return ""
    scope = str(payload.get("scope") or "")
    return scope if scope in {"manager", "promotion"} else ""


def _staff_security_gate_target(request):
    gate = request.session.get("staff_security_gate")
    if gate == "PASSWORD_CHANGE_REQUIRED":
        return reverse("staff-password-change")
    if gate == "STAFF_SECURITY_SETUP_REQUIRED":
        return reverse("staff-security-questions")
    return ""


def api_guard(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        gate_target = _staff_security_gate_target(request)
        if gate_target and request.path != gate_target:
            return redirect(gate_target)
        if not request.session.get("api_access"):
            target = _capture_anonymous_post(request, view_func.__name__, kwargs)
            messages.info(
                request, "Please sign in to continue. Your destination has been saved."
            )
            return _login_redirect_for(request, target)
        return view_func(request, *args, **kwargs)

    return wrapper


def _onboarding_user_key(request, user_info):
    """Create a stable, non-identifying browser-storage key for this signed-in user."""
    if not request.session.get("api_access"):
        return "guest"
    identity = ""
    if isinstance(user_info, dict):
        identity = (
            user_info.get("id")
            or user_info.get("email")
            or user_info.get("username")
            or ""
        )
    if not identity:
        identity = (
            f"session:{getattr(request.session, 'session_key', '') or 'signed-in'}"
        )
    message = f"artisan-market-onboarding-v1:{str(identity).strip().casefold()}"
    digest = hmac.new(
        str(settings.SECRET_KEY).encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return digest[:20]


def page(request, template, **context):
    gate_target = _staff_security_gate_target(request)
    if gate_target and request.path != gate_target:
        return redirect(gate_target)
    user_info = request.session.get("api_user", {})
    context.setdefault("user_info", user_info)
    nav_role = (
        context.get("nav_role")
        or context.get("role")
        or request.session.get("api_role")
    )
    if not nav_role and isinstance(user_info, dict):
        nav_role = user_info.get("role", "")
    normalized_role = str(nav_role or "").lower()
    authenticated = bool(request.session.get("api_access"))
    context.setdefault("nav_role", normalized_role)
    if context.get("nav_role") == "artisan":
        profile_payload = context.get("profile")
        if isinstance(profile_payload, dict) and "profile_photo" in profile_payload:
            user_info = dict(user_info) if isinstance(user_info, dict) else {}
            user_info["profile_photo"] = profile_payload.get("profile_photo") or ""
            request.session["api_user"] = user_info
            request.session.modified = True
            context["user_info"] = user_info
        context.setdefault(
            "nav_profile_photo",
            (
                (user_info.get("profile_photo") or "")
                if isinstance(user_info, dict)
                else ""
            ),
        )
    context.setdefault("authenticated", authenticated)
    capabilities = context.get("support_capabilities")
    manager_identity = bool(
        (isinstance(capabilities, dict) and capabilities.get("is_superuser"))
        or (isinstance(user_info, dict) and user_info.get("is_superuser"))
    )
    if normalized_role in {"artisan", "customer"}:
        # Manager chrome (privacy bar, analytics cards, staff navigation) belongs
        # to the staff workspace only. Whatever a stale flag or a stale API token
        # says, it is never painted onto an artisan's or a customer's page.
        manager_identity = False
    context.setdefault("platform_manager", manager_identity)
    context.setdefault("tour_role", normalized_role if authenticated else "guest")
    context.setdefault("tour_user_key", _onboarding_user_key(request, user_info))
    context.setdefault(
        "tour_auto_offer",
        bool(context.get("is_landing_page") and not context.get("platform_manager")),
    )
    context.setdefault(
        "payment_notice_auto_offer",
        bool(
            authenticated
            and request.session.get(PAYMENT_NOTICE_SESSION_KEY)
            and not gate_target
        ),
    )
    geography_templates = {
        "customer/profile.html",
        "artisan/profile.html",
        "customer/requests.html",
        "customer/request_detail.html",
        "artisan/services.html",
        "discovery/locations.html",
        "discovery/location_detail.html",
        "discovery/artisans.html",
    }
    context.setdefault("include_geography", template in geography_templates)
    context.setdefault(
        "include_phone_fields",
        template in {"customer/profile.html", "artisan/profile.html"},
    )
    context.setdefault("page_js_path", "js/pages/" + template.replace("/", "-"))
    context["page_js_path"] = context["page_js_path"].replace(".html", ".js")
    return render(request, template, context)


def api_data(request, method, path, *, data=None, files=None, params=None):
    client = APIClient(request)
    return (
        getattr(client, method.lower())(path, data=data, files=files)
        if method.upper() != "GET"
        else client.get(path, params=params)
    )


def handle_api_error(request, exc):
    if exc.status_code == 401:
        APIClient(request).clear_session()
    level = messages.WARNING if exc.status_code == 429 else messages.ERROR
    messages.add_message(request, level, exc.user_message)


def request_wants_json(request):
    return "application/json" in request.headers.get("Accept", "").lower()


def error_json_payload(exc):
    return {
        "message": exc.user_message,
        "detail": exc.user_message,
        "status": exc.status_code,
    }


def cleaned_payload(cleaned):
    payload = {}
    phone_unchanged = bool(cleaned.get("_phone_number_unchanged"))
    internal_fields = {
        "_phone_number_unchanged",
        "nearest_institution_source_id",
        "phone_country_calling_code",
        "location_country_filter",
        "location_state_filter",
        "location_area_filter",
    }
    for key, value in cleaned.items():
        if key in internal_fields or (key == "phone_number" and phone_unchanged):
            continue
        if value in ("", None):
            if key in (
                "phone_number",
                "budget",
                "location_id",
                "starting_price",
                "booking",
                "promotion_campaign",
                "conversation_id",
            ):
                continue
            payload[key] = value
            continue
        if hasattr(value, "isoformat"):
            payload[key] = value.isoformat()
        elif isinstance(value, UUID):
            payload[key] = str(value)
        elif isinstance(value, Decimal):
            payload[key] = str(value)
        elif key == "roles":
            payload[key] = list(value)
        else:
            payload[key] = value
    return payload


def _landing_home_context():
    """Shared static carousel data for public and signed-in home shells."""
    return {
        "visual_themes": LANDING_VISUAL_THEMES,
        "hero_slides": LANDING_VISUAL_THEMES,
        "initial_hero": LANDING_VISUAL_THEMES[0],
        "is_landing_page": True,
        "home_dashboard": True,
        "page_js_path": "js/pages/landing.js",
    }


WORKSPACE_ROLES = {"artisan", "customer", "staff"}


def _workspace_role(value):
    """Normalise a stored role; anything unrecognised means "ask again"."""
    role = str(value or "").strip().lower()
    return role if role in WORKSPACE_ROLES else ""


def _is_staff_identity(user_info):
    """Only a staff account may ever open a staff workspace."""
    if not isinstance(user_info, dict):
        return False
    return bool(user_info.get("is_staff") or user_info.get("is_superuser"))


def current_role(request):
    """The signed-in person's OWN workspace.

    The sign-in payload carries the real role, so a stored role wins -- with one
    guard: a "staff" tag is honoured only while the stored identity is still a
    staff account. A leftover tag from an earlier sign-in must never hand an
    artisan (or a customer) the previous, staff user's dashboard. When no role
    is stored, the API is asked which profile this account owns, and the
    fallback is the customer workspace -- never the staff one.

    Dual-role accounts keep their established workspace choice; service
    conversations still use the backend's participant-specific viewer_role.
    """
    user_info = request.session.get("api_user", {})
    staff_identity = _is_staff_identity(user_info)
    stored_roles = (
        _workspace_role(request.session.get("api_role")),
        _workspace_role(user_info.get("role") if isinstance(user_info, dict) else ""),
    )
    role = ""
    for candidate in stored_roles:
        if candidate in {"artisan", "customer"}:
            role = candidate
            break
        if candidate == "staff" and staff_identity:
            role = "staff"
            break
    if role:
        request.session["api_role"] = role
        request.session.modified = True
        return role
    client = APIClient(request)
    try:
        client.get("/profile/artisan/personal/profile/")
        role = "artisan"
    except APIError:
        try:
            client.get("/profile/customer/personal/profile/")
            role = "customer"
        except APIError:
            # Never guess "staff": an unknown account gets its own customer
            # workspace, because staff pages expose other people's data.
            role = "staff" if staff_identity else "customer"
    request.session["api_role"] = role
    request.session.modified = True
    return role


def redirect_after_login(request):
    pending_response = _continue_pending_protected_action(request)
    if pending_response is not None:
        return pending_response

    # The destination follows the signed-in person's own role, so a staff
    # capability can never pull an artisan or a customer into a staff page.
    role = current_role(request)
    capabilities = _support_capabilities(APIClient(request))
    staff_workspace = role == "staff" and (
        capabilities.get("is_superuser") or capabilities.get("is_support_staff")
    )
    if staff_workspace:
        default_target = reverse("dashboard")
    else:
        default_target = reverse("home") if role == "customer" else reverse("dashboard")
    candidate = (
        request.GET.get("next")
        or request.POST.get("next")
        or request.session.get("login_next")
    )
    target = safe_internal_next(request, candidate) or default_target
    request.session.pop("login_next", None)
    request.session.modified = True
    return redirect(target)


def _recent_notification_items(request, client, limit=5):
    try:
        _, payload, _ = client.get(
            "/notifications/", params={"limit": limit, "offset": 0}
        )
        items = extract_items(payload)
        total = (
            payload.get("count", len(items))
            if isinstance(payload, dict)
            else len(items)
        )
    except APIError as exc:
        handle_api_error(request, exc)
        return [], 0

    for item in items:
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        item["safe_url"] = safe_internal_url(data.get("action_url"))
        item["safe_action_label"] = (
            data.get("action_label", "") if item["safe_url"] else ""
        )
    return items, total


def _customer_request_count(request, client):
    try:
        _, payload, _ = client.get(
            "/services/requests/", params={"limit": 1, "offset": 0}
        )
        items = extract_items(payload)
        return (
            payload.get("count", len(items))
            if isinstance(payload, dict)
            else len(items)
        )
    except APIError as exc:
        handle_api_error(request, exc)
        return 0


def _artisan_request_preview(request, client, limit=2):
    try:
        _, payload, _ = client.get(
            "/services/artisan/requests/", params={"limit": limit, "offset": 0}
        )
        items = extract_items(payload)
        total = (
            payload.get("count", len(items))
            if isinstance(payload, dict)
            else len(items)
        )
        return items, total
    except APIError as exc:
        handle_api_error(request, exc)
        return [], 0


def _format_ngn_from_kobo(amount_kobo):
    try:
        amount = Decimal(str(amount_kobo or 0)) / Decimal("100")
    except (InvalidOperation, TypeError, ValueError):
        amount = Decimal("0.00")
    return f"{amount:,.2f}"


def _artisan_earnings_summary(request, client):
    try:
        _, payload, _ = client.get("/payments/artisan/earnings/summary/")
    except APIError as exc:
        handle_api_error(request, exc)
        return {"available": False}

    if not isinstance(payload, dict):
        return {"available": False}
    this_month = (
        payload.get("this_month")
        if isinstance(payload.get("this_month"), dict)
        else None
    )
    lifetime = (
        payload.get("lifetime") if isinstance(payload.get("lifetime"), dict) else None
    )
    if this_month is None or lifetime is None:
        return {"available": False}

    def safe_count(value):
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    return {
        "available": True,
        "currency": str(payload.get("currency") or "NGN"),
        "month_label": str(payload.get("month_label") or "This month"),
        "this_month_amount": _format_ngn_from_kobo(this_month.get("amount_kobo")),
        "this_month_payout_count": safe_count(this_month.get("payout_count")),
        "lifetime_amount": _format_ngn_from_kobo(lifetime.get("amount_kobo")),
        "lifetime_payout_count": safe_count(lifetime.get("payout_count")),
    }


def _is_payout_notification(item):
    event_family = str(item.get("event_key") or "").partition(":")[0]
    return event_family in {"payout_pending", "payout_successful", "payout_failed"}


def _support_capabilities(client):
    try:
        _, payload, _ = client.get("/support/cases/capabilities/")
    except APIError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _count_from_payload(payload):
    if isinstance(payload, dict):
        try:
            return max(0, int(payload.get("count", len(extract_items(payload))) or 0))
        except (TypeError, ValueError):
            return len(extract_items(payload))
    return len(extract_items(payload))


def _support_count(client, path, params=None):
    try:
        _, payload, _ = client.get(
            path, params={**(params or {}), "limit": 1, "offset": 0}
        )
    except APIError:
        return 0
    return _count_from_payload(payload)


def _support_dashboard_data(client, *, capabilities=None):
    capabilities = (
        capabilities if capabilities is not None else _support_capabilities(client)
    )
    data = {
        "support_capabilities": capabilities,
        "support_dashboard_ready": bool(capabilities.get("is_support_staff")),
        "support_cases": [],
        "support_refunds": [],
        "support_case_total": 0,
        "support_refund_total": 0,
        "open_case_count": 0,
        "review_case_count": 0,
        "urgent_case_count": 0,
        "dispute_case_count": 0,
        "requested_refund_count": 0,
        "approved_refund_count": 0,
        "processing_refund_count": 0,
        "processing_unknown_refund_count": 0,
        "promotion_applications": [],
        "promotion_application_count": 0,
        "case_status_metrics": [],
        "refund_status_metrics": [],
    }
    if not data["support_dashboard_ready"]:
        return data

    case_path = "/support/cases/"
    refund_path = "/support/cases/refunds/"
    try:
        _, case_payload, _ = client.get(case_path, params={"limit": 8, "offset": 0})
    except APIError:
        case_payload = []
    cases = extract_items(case_payload)
    data["support_cases"] = cases[:8]
    data["support_case_total"] = _count_from_payload(case_payload)

    data["open_case_count"] = _support_count(client, case_path, {"status": "OPEN"})
    data["review_case_count"] = _support_count(
        client, case_path, {"status": "UNDER_REVIEW"}
    )
    data["urgent_case_count"] = _support_count(
        client, case_path, {"priority": "URGENT"}
    )
    data["dispute_case_count"] = _support_count(
        client, case_path, {"case_type": "DISPUTE"}
    )

    case_status_rows = [
        ("Open", data["open_case_count"]),
        ("Under review", data["review_case_count"]),
        ("Resolved", _support_count(client, case_path, {"status": "RESOLVED"})),
        ("Closed", _support_count(client, case_path, {"status": "CLOSED"})),
    ]
    case_status_total = sum(count for _, count in case_status_rows) or 1
    data["case_status_metrics"] = [
        {
            "label": label,
            "count": count,
            "percent": round(count * 100 / case_status_total),
        }
        for label, count in case_status_rows
    ]

    if capabilities.get("can_view_refunds"):
        try:
            _, refund_payload, _ = client.get(
                refund_path, params={"limit": 6, "offset": 0}
            )
        except APIError:
            refund_payload = []
        data["support_refunds"] = extract_items(refund_payload)[:6]
        data["support_refund_total"] = _count_from_payload(refund_payload)
        data["requested_refund_count"] = _support_count(
            client, refund_path, {"status": "REQUESTED"}
        )
        data["approved_refund_count"] = _support_count(
            client, refund_path, {"status": "APPROVED"}
        )
        data["processing_refund_count"] = _support_count(
            client, refund_path, {"status": "PROCESSING"}
        )
        data["processing_unknown_refund_count"] = _support_count(
            client, refund_path, {"status": "PROCESSING_UNKNOWN"}
        )
        refund_status_rows = [
            ("Requested", data["requested_refund_count"]),
            ("Approved", data["approved_refund_count"]),
            ("Processing", data["processing_refund_count"]),
            ("Outcome unknown", data["processing_unknown_refund_count"]),
            ("Succeeded", _support_count(client, refund_path, {"status": "SUCCEEDED"})),
            (
                "Failed / rejected",
                _support_count(client, refund_path, {"status": "FAILED"}),
            ),
        ]
        refund_status_rows[-1] = (
            refund_status_rows[-1][0],
            refund_status_rows[-1][1]
            + _support_count(client, refund_path, {"status": "REJECTED"}),
        )
        refund_status_total = sum(count for _, count in refund_status_rows) or 1
        data["refund_status_metrics"] = [
            {
                "label": label,
                "count": count,
                "percent": round(count * 100 / refund_status_total),
            }
            for label, count in refund_status_rows
        ]

    if capabilities.get("can_review_promotions"):
        try:
            _, promotion_payload, _ = client.get(
                "/promotions/review/applications/",
                params={"status": "PENDING_REVIEW", "limit": 8, "offset": 0},
            )
            data["promotion_applications"] = extract_items(promotion_payload)[:8]
            data["promotion_application_count"] = _count_from_payload(promotion_payload)
        except APIError:
            data["promotion_applications"] = []
            data["promotion_application_count"] = 0
    return data


def _manager_analytics_for_template(payload):
    if not isinstance(payload, dict):
        return {}
    data = dict(payload)

    def parsed_datetime(value):
        if isinstance(value, str):
            return parse_datetime(value) or value
        return value

    for field in ("generated_at",):
        data[field] = parsed_datetime(data.get(field))
    snapshot = data.get("snapshot")
    if isinstance(snapshot, dict):
        data["snapshot"] = {
            **snapshot,
            "generated_at": parsed_datetime(snapshot.get("generated_at")),
            "scheduled_for": parsed_datetime(snapshot.get("scheduled_for")),
        }
    schedule = data.get("snapshot_schedule")
    if isinstance(schedule, dict):
        data["snapshot_schedule"] = {
            **schedule,
            **{
                field: parsed_datetime(schedule.get(field))
                for field in (
                    "next_run_at",
                    "last_run_at",
                    "last_success_at",
                    "last_error_at",
                    "updated_at",
                )
            },
            "history": [
                (
                    {**event, "created_at": parsed_datetime(event.get("created_at"))}
                    if isinstance(event, dict)
                    else event
                )
                for event in (
                    schedule.get("history")
                    if isinstance(schedule.get("history"), list)
                    else []
                )
            ],
        }
    promotions = data.get("promotions")
    if isinstance(promotions, dict) and isinstance(
        promotions.get("upcoming_expirations"), list
    ):
        promotions["upcoming_expirations"] = [
            (
                {**row, "expires_at": parsed_datetime(row.get("expires_at"))}
                if isinstance(row, dict)
                else row
            )
            for row in promotions["upcoming_expirations"]
        ]
    for collection, field in (
        ("recent_cases", "updated_at"),
        ("recent_requests", "created_at"),
        ("recent_bookings", "created_at"),
    ):
        rows = data.get(collection)
        if isinstance(rows, list):
            data[collection] = [
                (
                    {**row, field: parsed_datetime(row.get(field))}
                    if isinstance(row, dict)
                    else row
                )
                for row in rows
            ]
    return data


def _placement_profile_links(rows):
    for row in rows:
        if not isinstance(row, dict):
            continue
        token = str(row.get("profile_visit_token") or "").strip()
        profile_url = str(row.get("artisan_url") or "").strip()
        if token and profile_url:
            separator = "&" if "?" in profile_url else "?"
            row["artisan_url"] = (
                f"{profile_url}{separator}{urlencode({'promotion_visit': token})}"
            )
    return rows


def _public_homepage_promotions(request):
    try:
        _, payload, _ = APIClient(request).get("/promotions/homepage/")
        rows = payload if isinstance(payload, list) else extract_items(payload)
        return _placement_profile_links(rows)
    except APIError:
        # Anonymous public landing remains usable if the separately deployed API is unavailable.
        return []


def _public_market_picks(request):
    try:
        _, payload, _ = APIClient(request).get("/promotions/market-picks/")
        return payload if isinstance(payload, list) else extract_items(payload)
    except APIError:
        return []


def _public_promoted_discovery(request, filters):
    # No canonical service-area selection means no eligible geographic promotion.
    if not any(
        str(filters.get(key) or "").strip()
        for key in ("institution_id", "area_location_id", "area", "state")
    ):
        return []
    try:
        _, payload, _ = APIClient(request).get("/promotions/discovery/", params=filters)
        rows = payload.get("results", []) if isinstance(payload, dict) else []
        return _placement_profile_links(rows if isinstance(rows, list) else [])
    except APIError:
        return []


def home(request):
    if not request.session.get("api_access"):
        # The editorial homepage stays independent from catalogue data; only currently
        # eligible, server-verified homepage campaigns are added as a separate section.
        visual_themes = LANDING_VISUAL_THEMES
        return page(
            request,
            "landing.html",
            visual_themes=visual_themes,
            hero_slides=visual_themes,
            initial_hero=visual_themes[0],
            homepage_promotions=_public_homepage_promotions(request),
            market_picks=_public_market_picks(request),
            is_landing_page=True,
        )
    client = APIClient(request)
    # The workspace is chosen by the signed-in person's own role first. Staff
    # pages are only reachable for a staff identity: an artisan or a customer
    # must always land on their own dashboard, never on the one the previous
    # (staff) session was looking at.
    role = current_role(request)
    capabilities = _support_capabilities(client)
    is_manager = role == "staff" and bool(capabilities.get("is_superuser"))
    is_support_staff = role == "staff" and bool(capabilities.get("is_support_staff"))
    if not is_manager:
        request.session.pop(MANAGER_VALUES_REVEAL_SESSION_KEY, None)
        request.session.modified = True
    if is_manager:
        reveal_scope = _consume_manager_values_reveal(request)
        manager_values_revealed = reveal_scope == "manager"
        manager_analytics = {}
        if manager_values_revealed:
            try:
                _, manager_analytics, _ = client.get("/manager/overview/")
            except APIError as exc:
                handle_api_error(request, exc)
                manager_analytics = {}
        manager_analytics = _manager_analytics_for_template(manager_analytics)
        response = page(
            request,
            "manager/dashboard.html",
            role="staff",
            platform_manager=True,
            support_capabilities=capabilities,
            manager_values_revealed=manager_values_revealed,
            reveal_return_path=request.path,
            manager_analytics=manager_analytics,
            manager_snapshot_schedule=manager_analytics.get("snapshot_schedule", {}),
            manager_analytics_ready=bool(
                manager_values_revealed
                and isinstance(manager_analytics, dict)
                and isinstance(manager_analytics.get("kpis"), dict)
            ),
            **_landing_home_context(),
        )
        response["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
        response["Pragma"] = "no-cache"
        response["Expires"] = "0"
        return response

    if is_support_staff:
        return page(
            request,
            "support/dashboard.html",
            role="staff",
            platform_manager=False,
            **_support_dashboard_data(client, capabilities=capabilities),
            **_landing_home_context(),
        )

    if role == "customer":
        return customer_profile(request, home_dashboard=True)

    if role == "staff":
        return page(
            request,
            "support/dashboard.html",
            role="staff",
            platform_manager=False,
            **_support_dashboard_data(client, capabilities=capabilities),
            **_landing_home_context(),
        )

    data = {
        "role": role,
        "profile": None,
        "bookings": [],
        "requests": [],
        "request_total": 0,
        "notifications": [],
        "notification_total": 0,
        "services": [],
        "cases": [],
        "profile_health": None,
    }
    if role == "artisan":
        # Independent loads: a booking/catalogue failure cannot hide profile health.
        try:
            _, profile_payload, _ = client.get("/profile/artisan/personal/profile/")
            if isinstance(profile_payload, dict) and profile_payload:
                data["profile"] = profile_payload
                data["profile_health"] = _action_profile_health_summary(profile_payload)
        except APIError as exc:
            handle_api_error(request, exc)
        for key, endpoint in [
            ("bookings", "/booking/"),
            ("services", "/services/get_artisan_services/"),
            ("requests", "/services/artisan/requests/"),
        ]:
            try:
                _, payload, _ = client.get(endpoint, params={"limit": 30, "offset": 0})
                items = extract_items(payload)
                data[key] = items[:30]
                if key == "requests":
                    data["request_total"] = _count_from_payload(payload)
                if key == "bookings":
                    data["booking_total"] = _count_from_payload(payload)
            except APIError as exc:
                handle_api_error(request, exc)
        data["notifications"], data["notification_total"] = _recent_notification_items(
            request, client, limit=30
        )
    return page(request, "dashboard.html", **_landing_home_context(), **data)


@api_guard
def support_operations_dashboard(request):
    """Keep the support team workspace directly available beside manager analytics."""
    client = APIClient(request)
    capabilities = _support_capabilities(client)
    return page(
        request,
        "support/dashboard.html",
        role="staff",
        platform_manager=bool(capabilities.get("is_superuser")),
        **_support_dashboard_data(client, capabilities=capabilities),
    )


# ---------------- Authentication ----------------

STAFF_MFA_SESSION_KEYS = (
    "staff_mfa_challenge_id",
    "staff_mfa_masked_email",
    "staff_mfa_resend_after",
)


def _clear_staff_mfa_pending(request):
    for key in STAFF_MFA_SESSION_KEYS:
        request.session.pop(key, None)


def _start_staff_mfa_pending(request, payload):
    APIClient(request).clear_session()
    _clear_pending_action(request, clear_form_values=True)
    request.session.pop(PROFILE_COMPLETION_NEXT_SESSION_KEY, None)
    request.session.cycle_key()
    request.session["staff_mfa_challenge_id"] = str(
        payload.get("mfa_challenge_id") or ""
    )
    request.session["staff_mfa_masked_email"] = str(payload.get("masked_email") or "")
    request.session["staff_mfa_resend_after"] = int(payload.get("resend_after") or 0)
    request.session.modified = True


def _store_authenticated_api_session(request, payload):
    access = payload.get("access") if isinstance(payload, dict) else None
    refresh = payload.get("refresh") if isinstance(payload, dict) else None
    if not access or not refresh:
        return False
    client = APIClient(request)
    client.clear_session()
    request.session.cycle_key()
    request.session["api_access"] = access
    request.session["api_refresh"] = refresh
    request.session["api_user"] = payload.get("user", {})
    request.session["api_role"] = (payload.get("user") or {}).get("role") or ""
    request.session[PAYMENT_NOTICE_SESSION_KEY] = True
    _clear_staff_mfa_pending(request)
    for key in ("reset_token", "reset_email", "staff_reset_token", "staff_reset_email"):
        request.session.pop(key, None)
    if payload.get("staff_password_change_required"):
        request.session["api_role"] = "staff"
        request.session["staff_security_gate"] = "PASSWORD_CHANGE_REQUIRED"
    elif payload.get("staff_security_setup_required"):
        request.session["api_role"] = "staff"
        request.session["staff_security_gate"] = "STAFF_SECURITY_SETUP_REQUIRED"
    request.session.pop(MANAGER_VALUES_REVEAL_SESSION_KEY, None)
    request.session.modified = True
    return True


def _staff_security_gate_redirect(request):
    gate = request.session.get("staff_security_gate")
    if gate == "PASSWORD_CHANGE_REQUIRED":
        return redirect("staff-password-change")
    if gate == "STAFF_SECURITY_SETUP_REQUIRED":
        return redirect("staff-security-questions")
    return None


def staff_mfa(request):
    challenge_id = request.session.get("staff_mfa_challenge_id")
    if not challenge_id:
        messages.info(
            request, "Start signing in again to receive a staff verification code."
        )
        return redirect("login")
    form = StaffMFAVerifyForm(request.POST or None)
    action = (
        request.POST.get("action", "verify") if request.method == "POST" else "verify"
    )
    if request.method == "POST" and action == "resend":
        try:
            _, payload, _ = APIClient(request).post(
                "/staff-mfa/resend/", {"challenge_id": challenge_id}
            )
            request.session["staff_mfa_challenge_id"] = str(
                payload.get("challenge_id") or challenge_id
            )
            request.session["staff_mfa_masked_email"] = str(
                payload.get("masked_email")
                or request.session.get("staff_mfa_masked_email", "")
            )
            request.session["staff_mfa_resend_after"] = int(
                payload.get("resend_after") or 0
            )
            request.session.modified = True
            messages.success(
                request,
                payload.get("message")
                or "Check the registered staff email for a code.",
            )
        except APIError as exc:
            if exc.status_code in {400, 403, 404}:
                _clear_staff_mfa_pending(request)
                messages.error(
                    request,
                    "The staff sign-in challenge expired. Please sign in again.",
                )
                return redirect("login")
            handle_api_error(request, exc)
        response = page(request, "auth/staff_mfa.html", form=form)
        response["Cache-Control"] = "no-store, private"
        response["Pragma"] = "no-cache"
        return response
    if request.method == "POST" and action == "verify" and form.is_valid():
        try:
            _, payload, _ = APIClient(request).post(
                "/staff-mfa/verify/",
                {"challenge_id": challenge_id, "code": form.cleaned_data["code"]},
            )
            if not _store_authenticated_api_session(request, payload):
                raise APIError(
                    502, {"error": "The API did not complete staff sign-in."}
                )
            messages.success(
                request, payload.get("message") or "Staff sign-in verified."
            )
            gate_redirect = _staff_security_gate_redirect(request)
            if gate_redirect:
                return gate_redirect
            return redirect_after_login(request)
        except APIError as exc:
            if exc.status_code in {400, 403, 404}:
                messages.error(
                    request,
                    "That code is invalid or expired. Check it and try again, or request a new code.",
                )
            else:
                handle_api_error(request, exc)
    response = page(request, "auth/staff_mfa.html", form=form)
    response["Cache-Control"] = "no-store, private"
    response["Pragma"] = "no-cache"
    return response


def login(request):
    if request.method == "GET":
        _clear_staff_mfa_pending(request)
    next_target = (
        safe_internal_next(request, request.GET.get("next"))
        or safe_internal_next(request, request.POST.get("next"))
        or safe_internal_next(request, request.session.get("login_next"))
    )
    if next_target:
        request.session["login_next"] = next_target
        request.session.modified = True
    else:
        request.session.pop("login_next", None)
    form = LoginForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            _, payload, _ = APIClient(request).post(
                "/login/", cleaned_payload(form.cleaned_data)
            )
            if payload.get("mfa_required"):
                _start_staff_mfa_pending(request, payload)
                messages.info(
                    request,
                    "Enter the one-time code sent to your registered staff email.",
                )
                return redirect("staff-mfa")
            if not _store_authenticated_api_session(request, payload):
                raise APIError(
                    502, {"error": "The API did not return a complete sign-in session."}
                )
            gate_redirect = _staff_security_gate_redirect(request)
            if gate_redirect:
                return gate_redirect
            messages.success(request, "Welcome back. You are signed in.")
            return redirect_after_login(request)
        except APIError as exc:
            if (
                exc.status_code == 403
                and isinstance(exc.payload, dict)
                and exc.payload.get("action") == "redirect_to_otp_verification"
            ):
                email = form.cleaned_data["email"]
                messages.info(request, "Your account needs email verification first.")
                return redirect(f'{reverse("verify-otp")}?email={email}')
            handle_api_error(request, exc)
    return page(request, "auth/login.html", form=form, next_target=next_target or "")


def register(request):
    next_target = (
        safe_internal_next(request, request.GET.get("next"))
        or safe_internal_next(request, request.POST.get("next"))
        or safe_internal_next(request, request.session.get("login_next"))
    )
    if next_target:
        request.session["login_next"] = next_target
        request.session.modified = True
    form = RegisterForm(request.POST or None)
    valid_roles = {value for value, _label in form.fields["role"].choices}
    selected_role = form["role"].value()
    register_step = (
        "details"
        if request.method == "POST" and selected_role in valid_roles
        else "role"
    )
    if request.method == "POST" and form.is_valid():
        try:
            _, payload, _ = APIClient(request).post(
                "/register/", cleaned_payload(form.cleaned_data)
            )
            email = payload.get("email") or form.cleaned_data["email"]
            request.session["pending_email"] = email
            messages.success(
                request,
                payload_message(payload)
                or "Account created. Check your email for the verification code.",
            )
            return redirect(f'{reverse("verify-otp")}?email={email}')
        except APIError as exc:
            handle_api_error(request, exc)
    return page(
        request,
        "auth/register.html",
        form=form,
        register_step=register_step,
        next_target=next_target or "",
    )


def verify_otp(request):
    form = OTPForm(
        request.POST or None,
        initial={
            "email": request.GET.get("email")
            or request.session.get("pending_email", "")
        },
    )
    if request.method == "POST" and form.is_valid():
        try:
            _, payload, _ = APIClient(request).post(
                "/verify-otp/", cleaned_payload(form.cleaned_data)
            )
            request.session.pop("pending_email", None)
            messages.success(
                request, payload_message(payload) or "Email verified successfully."
            )
            return redirect(f'{reverse("login")}?verified=1')
        except APIError as exc:
            handle_api_error(request, exc)
    return page(request, "auth/verify_otp.html", form=form)


def request_otp(request):
    form = EmailForm(
        request.POST or None, initial={"email": request.GET.get("email", "")}
    )
    if request.method == "POST" and form.is_valid():
        try:
            _, payload, _ = APIClient(request).post(
                "/request-otp/", cleaned_payload(form.cleaned_data)
            )
            email = payload.get("email") or form.cleaned_data["email"]
            request.session["pending_email"] = email
            messages.success(
                request, payload_message(payload) or "A new code has been requested."
            )
            return redirect(f'{reverse("verify-otp")}?email={email}')
        except APIError as exc:
            handle_api_error(request, exc)
    return page(request, "auth/request_otp.html", form=form)


def _staff_security_question_choices(request):
    try:
        _, payload, _ = APIClient(request).get("/staff-security-question-options/")
    except APIError as exc:
        handle_api_error(request, exc)
        return []
    rows = payload.get("questions", []) if isinstance(payload, dict) else []
    return [
        (str(item["id"]), str(item["prompt"]))
        for item in rows
        if isinstance(item, dict) and item.get("id") and item.get("prompt")
    ]


def _staff_recovery_questions(cleaned):
    return [
        {
            "question_id": cleaned[f"question_{index}"],
            "answer": cleaned[f"answer_{index}"],
        }
        for index in range(1, 4)
    ]


def staff_security_setup(request):
    choices = _staff_security_question_choices(request)
    form = StaffSecuritySetupForm(
        request.POST or None,
        question_choices=choices,
        initial={"email": request.session.get("staff_security_email", "")},
    )
    if request.method == "POST" and form.is_valid():
        cleaned = form.cleaned_data
        payload = {
            "email": cleaned["email"],
            "otp": cleaned["otp"],
            "questions": _staff_recovery_questions(cleaned),
        }
        try:
            _, result, _ = APIClient(request).post(
                "/staff-password-resets/security-setup/complete/", payload
            )
            request.session.pop("staff_security_email", None)
            request.session.pop("staff_security_gate", None)
            request.session.modified = True
            messages.success(
                request,
                payload_message(result)
                or "Security questions registered. Sign in to continue.",
            )
            return redirect(
                "dashboard" if request.session.get("api_access") else "login"
            )
        except APIError as exc:
            handle_api_error(request, exc)
    return page(
        request,
        "auth/staff_security_setup.html",
        form=form,
        role="staff",
        page_title="Register staff security questions",
    )


def staff_password_reset(request):
    choices = _staff_security_question_choices(request)
    form = StaffRecoveryVerifyForm(
        request.POST or None,
        question_choices=choices,
        initial={"email": request.session.get("staff_reset_email", "")},
    )
    if request.method == "POST" and form.is_valid():
        cleaned = form.cleaned_data
        payload = {
            "email": cleaned["email"],
            "otp": cleaned["otp"],
            "questions": _staff_recovery_questions(cleaned),
        }
        try:
            _, result, _ = APIClient(request).post(
                "/staff-password-resets/verify/", payload
            )
            request.session["staff_reset_token"] = result.get("reset_token")
            request.session["staff_reset_email"] = cleaned["email"]
            request.session.modified = True
            messages.success(request, "Identity verified. Choose a new staff password.")
            return redirect("staff-password-reset-complete")
        except APIError as exc:
            handle_api_error(request, exc)
    return page(
        request,
        "auth/staff_password_reset.html",
        form=form,
        role="staff",
    )


def staff_password_reset_complete(request):
    reset_token = request.session.get("staff_reset_token")
    if not reset_token:
        return redirect("staff-password-reset")
    form = StaffResetCompleteForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        payload = cleaned_payload(form.cleaned_data)
        payload["reset_token"] = reset_token
        try:
            _, result, _ = APIClient(request).post(
                "/staff-password-resets/complete/", payload
            )
            APIClient(request).clear_session()
            messages.success(
                request,
                payload_message(result)
                or "Staff password changed. Sign in with the new password.",
            )
            return redirect("login")
        except APIError as exc:
            handle_api_error(request, exc)
    return page(
        request,
        "auth/staff_password_reset_complete.html",
        form=form,
        role="staff",
    )


@api_guard
def staff_password_change(request):
    form = StaffPasswordChangeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        payload = {
            "current_password": form.cleaned_data["current_password"],
            "new_password": form.cleaned_data["new_password"],
            "confirm_password": form.cleaned_data["confirm_password"],
        }
        try:
            _, result, _ = APIClient(request).post("/change-password/", payload)
            APIClient(request).clear_session()
            messages.success(
                request,
                payload_message(result)
                or "Password changed. Sign in with your new password.",
            )
            return redirect("login")
        except APIError as exc:
            handle_api_error(request, exc)
    return page(request, "auth/staff_password_change.html", form=form, role="staff")


@api_guard
def staff_security_questions(request):
    client = APIClient(request)
    choices = _staff_security_question_choices(request)
    registered_questions = []
    registered_labels = []
    registered = False
    try:
        _, profile, _ = client.get("/staff-security-questions/")
        registered = (
            bool(profile.get("registered")) if isinstance(profile, dict) else False
        )
        current_questions = (
            profile.get("questions", []) if isinstance(profile, dict) else []
        )
        registered_questions = [
            str(item.get("id"))
            for item in current_questions
            if isinstance(item, dict) and item.get("id")
        ]
        registered_labels = [
            str(item.get("prompt") or "")
            for item in current_questions
            if isinstance(item, dict)
        ]
    except APIError as exc:
        handle_api_error(request, exc)
    initial = {
        f"current_question_{index}": question_id
        for index, question_id in enumerate(registered_questions, start=1)
    }
    form = StaffSecurityQuestionUpdateForm(
        request.POST or None,
        question_choices=choices,
        registered_questions=registered_questions if registered else None,
        initial=initial,
    )
    if request.method == "POST" and form.is_valid():
        cleaned = form.cleaned_data
        payload = {
            "current_password": cleaned["current_password"],
            "questions": _staff_recovery_questions(cleaned),
        }
        if registered:
            payload["current_answers"] = [
                {
                    "question_id": cleaned[f"current_question_{index}"],
                    "answer": cleaned[f"current_answer_{index}"],
                }
                for index in range(1, 4)
            ]
        try:
            _, result, _ = client.put("/staff-security-questions/", payload)
            if (
                request.session.get("staff_security_gate")
                == "STAFF_SECURITY_SETUP_REQUIRED"
            ):
                request.session.pop("staff_security_gate", None)
            request.session.modified = True
            messages.success(
                request, payload_message(result) or "Security questions saved."
            )
            return redirect("dashboard")
        except APIError as exc:
            handle_api_error(request, exc)
    return page(
        request,
        "auth/staff_security_questions.html",
        form=form,
        role="staff",
        registered=registered,
        registered_labels=registered_labels,
    )


def forgot_password(request):
    form = EmailForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            _, payload, _ = APIClient(request).post(
                "/forgot-password/", cleaned_payload(form.cleaned_data)
            )
            email = payload.get("email") or form.cleaned_data["email"]
            request.session["reset_email"] = email
            messages.success(
                request,
                payload_message(payload) or "Check your email for the reset code.",
            )
            return redirect("verify-reset-otp")
        except APIError as exc:
            handle_api_error(request, exc)
    return page(request, "auth/forgot_password.html", form=form)


def verify_reset_otp(request):
    form = OTPForm(
        request.POST or None,
        initial={
            "email": request.GET.get("email") or request.session.get("reset_email", "")
        },
    )
    if request.method == "POST" and form.is_valid():
        try:
            _, payload, _ = APIClient(request).post(
                "/verify-reset-otp/", cleaned_payload(form.cleaned_data)
            )
            request.session["reset_email"] = form.cleaned_data["email"]
            request.session["reset_token"] = payload.get("reset_token")
            request.session.modified = True
            messages.success(request, "Reset code verified. Choose a new password.")
            return redirect("reset-password")
        except APIError as exc:
            handle_api_error(request, exc)
    return page(request, "auth/verify_reset_otp.html", form=form)


def reset_password(request):
    token = request.session.get("reset_token")
    if not token:
        return redirect("forgot-password")
    form = ResetForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            reset_payload = cleaned_payload(form.cleaned_data)
            reset_payload["reset_token"] = token
            _, payload, _ = APIClient(request).post("/reset-password/", reset_payload)
            request.session.pop("reset_token", None)
            request.session.pop("reset_email", None)
            messages.success(
                request,
                payload_message(payload)
                or "Password changed successfully. You can now sign in.",
            )
            return redirect("login")
        except APIError as exc:
            handle_api_error(request, exc)
    return page(request, "auth/reset_password.html", form=form)


@require_POST
def logout(request):
    _clear_pending_action(request, clear_form_values=True)
    request.session.pop("login_next", None)
    request.session.pop("api_role", None)
    APIClient(request).logout()
    request.session.flush()
    messages.success(request, "You have been signed out.")
    return redirect("home")


@require_POST
def acknowledge_payment_notice(request):
    """Keep the post-login payment notice blocking until it is explicitly acknowledged."""
    request.session.pop(PAYMENT_NOTICE_SESSION_KEY, None)
    request.session.modified = True
    destination = safe_internal_next(request, request.POST.get("next")) or reverse(
        "home"
    )
    return redirect(destination)


# ---------------- Profiles / verification ----------------


@api_guard
def customer_profile(request, *, home_dashboard=False):
    from .geography import world_geography

    client = APIClient(request)
    try:
        _, profile, _ = client.get("/profile/customer/personal/profile/")
    except APIError as exc:
        handle_api_error(request, exc)
        if not home_dashboard:
            return redirect("dashboard")
        # This page *is* the dashboard target, so bouncing it back to
        # /dashboard/ would loop forever. Show the workspace shell instead.
        profile = {}

    completion_requested = bool(
        request.session.get(PENDING_ACTION_SESSION_KEY)
        or request.session.get(PROFILE_COMPLETION_NEXT_SESSION_KEY)
    )
    profile_completion_required = (
        completion_requested and not _action_profile_is_complete(profile)
    )
    next_target = (
        safe_internal_next(request, request.GET.get("next"))
        or safe_internal_next(
            request, request.session.get(PROFILE_COMPLETION_NEXT_SESSION_KEY)
        )
        or ""
    )
    if request.method == "POST":
        form = CustomerProfileForm(request.POST, request.FILES, initial=profile)
    else:
        form = CustomerProfileForm(initial=profile)
    if profile_completion_required:
        for field in (
            "first_name",
            "last_name",
            "phone_number",
            "country",
            "state",
            "area",
        ):
            form.fields[field].required = True
        form.fields["phone_number"].help_text = (
            "A valid phone number and personal location are required to continue."
        )

    if request.method == "POST" and form.is_valid():
        try:
            data = {
                key: value
                for key, value in cleaned_payload(form.cleaned_data).items()
                if key != "profile_photo"
            }
            if not form.cleaned_data.get("nearest_institution"):
                data["nearest_institution"] = ""
            files = {}
            if request.FILES.get("profile_photo"):
                uploaded = request.FILES["profile_photo"]
                files["profile_photo"] = (
                    uploaded.name,
                    uploaded.file,
                    uploaded.content_type or "application/octet-stream",
                )
            if files and data.get("nearest_institution") is None:
                data["nearest_institution"] = (
                    ""  # Multipart uses blank to clear the nullable relation.
                )
            _, profile, _ = client.patch(
                "/profile/customer/personal/profile/", data=data, files=files or None
            )
            messages.success(request, "Profile updated successfully.")
            pending_response = _continue_pending_protected_action(request)
            if pending_response is not None:
                return pending_response
            next_target = (
                safe_internal_next(request, request.POST.get("next"))
                or safe_internal_next(request, request.GET.get("next"))
                or safe_internal_next(
                    request, request.session.get(PROFILE_COMPLETION_NEXT_SESSION_KEY)
                )
            )
            request.session.pop(PROFILE_COMPLETION_NEXT_SESSION_KEY, None)
            if next_target and next_target != reverse("customer-profile"):
                return redirect(next_target)
            return redirect("home" if home_dashboard else "customer-profile")
        except APIError as exc:
            handle_api_error(request, exc)

    request_total = _customer_request_count(request, client)
    notifications, notification_total = _recent_notification_items(
        request, client, limit=5
    )
    return page(
        request,
        "customer/profile.html",
        profile=profile,
        profile_picker_catalogue=world_geography(),
        profile_picker_today=timezone.localdate().isoformat(),
        form=form,
        role="customer",
        nav_role=current_role(request),
        request_total=request_total,
        notifications=notifications,
        notification_total=notification_total,
        profile_completion_required=profile_completion_required,
        missing_profile_fields=[
            ACTION_PROFILE_FIELD_LABELS[field]
            for field in _action_profile_missing_fields(profile)
        ],
        next_target=next_target,
        **(_landing_home_context() if home_dashboard else {"page_js_path": ""}),
    )


@api_guard
def artisan_profile(request):
    from .geography import world_geography

    client = APIClient(request)
    try:
        _, profile, _ = client.get("/profile/artisan/personal/profile/")
    except APIError as exc:
        handle_api_error(request, exc)
        return redirect("dashboard")

    completion_requested = bool(
        request.session.get(PENDING_ACTION_SESSION_KEY)
        or request.session.get(PROFILE_COMPLETION_NEXT_SESSION_KEY)
    )
    profile_completion_required = (
        completion_requested and not _action_profile_is_complete(profile)
    )
    next_target = (
        safe_internal_next(request, request.GET.get("next"))
        or safe_internal_next(
            request, request.session.get(PROFILE_COMPLETION_NEXT_SESSION_KEY)
        )
        or ""
    )
    if request.method == "POST":
        form = ArtisanProfileForm(request.POST, request.FILES, initial=profile)
    else:
        form = ArtisanProfileForm(initial=profile)
    if profile_completion_required:
        for field in (
            "first_name",
            "last_name",
            "phone_number",
            "country",
            "state",
            "area",
        ):
            form.fields[field].required = True
        form.fields["phone_number"].help_text = (
            "A valid phone number and personal location are required to continue."
        )

    if request.method == "POST" and form.is_valid():
        data = {
            k: v
            for k, v in cleaned_payload(form.cleaned_data).items()
            if k != "profile_photo"
        }
        files = {}
        if request.FILES.get("profile_photo"):
            f = request.FILES["profile_photo"]
            files["profile_photo"] = (f.name, f.file, f.content_type)
        if not form.cleaned_data.get("nearest_institution"):
            data["nearest_institution"] = ""
        try:
            _, profile, _ = client.patch(
                "/profile/artisan/personal/profile/", data=data, files=files or None
            )
            messages.success(request, "Artisan profile updated successfully.")
            pending_response = _continue_pending_protected_action(request)
            if pending_response is not None:
                return pending_response
            next_target = (
                safe_internal_next(request, request.POST.get("next"))
                or safe_internal_next(request, request.GET.get("next"))
                or safe_internal_next(
                    request, request.session.get(PROFILE_COMPLETION_NEXT_SESSION_KEY)
                )
            )
            request.session.pop(PROFILE_COMPLETION_NEXT_SESSION_KEY, None)
            if next_target and next_target != reverse("artisan-profile"):
                return redirect(next_target)
            return redirect("artisan-profile")
        except APIError as exc:
            handle_api_error(request, exc)

    requests, request_total = _artisan_request_preview(request, client, limit=2)
    income_summary = _artisan_earnings_summary(request, client)
    recent_notifications, notification_total = _recent_notification_items(
        request, client, limit=50
    )
    payout_notifications = [
        item for item in recent_notifications if _is_payout_notification(item)
    ][:2]
    notification_previews = [
        item for item in recent_notifications if not _is_payout_notification(item)
    ][:2]
    notifications = recent_notifications[:5]
    response = page(
        request,
        "artisan/profile.html",
        profile=profile,
        profile_picker_catalogue=world_geography(),
        profile_picker_today=timezone.localdate().isoformat(),
        form=form,
        role="artisan",
        profile_health=_action_profile_health_summary(profile),
        requests=requests,
        request_total=request_total,
        income_summary=income_summary,
        payout_notifications=payout_notifications,
        notification_previews=notification_previews,
        notifications=notifications,
        notification_total=notification_total,
        profile_completion_required=profile_completion_required,
        missing_profile_fields=[
            ACTION_PROFILE_FIELD_LABELS[field]
            for field in _action_profile_missing_fields(profile)
        ],
        next_target=next_target,
        page_js_path="",
    )
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    response["Pragma"] = "no-cache"
    response["Expires"] = "0"
    return response


def _promotion_application_catalog(client):
    _, eligibility, _ = client.get("/promotions/eligibility/")
    _, institutions_payload, _ = client.get("/services/institutions/")
    _, locations_payload, _ = client.get(
        "/services/locations/", params={"limit": 1000, "offset": 0}
    )
    _, offerings_payload, _ = client.get(
        "/services/get_artisan_services/", params={"limit": 1000, "offset": 0}
    )
    _, applications_payload, _ = client.get(
        "/promotions/applications/", params={"limit": 100, "offset": 0}
    )
    return {
        "eligibility": (
            eligibility
            if isinstance(eligibility, dict)
            else {"eligible": False, "requirements": []}
        ),
        "institutions": (
            institutions_payload
            if isinstance(institutions_payload, list)
            else extract_items(institutions_payload)
        ),
        "locations": extract_items(locations_payload),
        "offerings": extract_items(offerings_payload),
        "applications": extract_items(applications_payload),
    }


@api_guard
def promotion_applications(request):
    client = APIClient(request)
    try:
        catalog = _promotion_application_catalog(client)
    except APIError as exc:
        handle_api_error(request, exc)
        return redirect("artisan-profile")

    application_id = str(
        request.GET.get("application") or request.POST.get("application_id") or ""
    ).strip()
    selected_application = None
    if application_id:
        try:
            _, selected_application, _ = client.get(
                replace_path("/promotions/applications/{pk}/", pk=application_id)
            )
        except APIError as exc:
            handle_api_error(request, exc)
            return redirect("promotion-applications")

    initial = {}
    if isinstance(selected_application, dict):
        initial = {
            "promotion_type": selected_application.get("promotion_type", ""),
            "institution_id": selected_application.get("institution_id") or "",
            "area_location_id": selected_application.get("area_location_id") or "",
            "state_country": selected_application.get("state_country") or "",
            "state_name": selected_application.get("state_name") or "",
            "service_ids": [
                str(value) for value in selected_application.get("service_ids", [])
            ],
            "promotional_description": selected_application.get(
                "promotional_description"
            )
            or "",
        }

    can_edit = (
        not selected_application
        or selected_application.get("status") == "CHANGES_REQUIRED"
    )
    form = PromotionApplicationForm(
        request.POST or None,
        initial=initial if request.method != "POST" else None,
        institutions=catalog["institutions"],
        locations=catalog["locations"],
        offerings=catalog["offerings"],
    )

    if (
        request.method == "POST"
        and selected_application
        and request.POST.get("action") == "withdraw"
    ):
        try:
            _, withdrawn, _ = client.post(
                replace_path(
                    "/promotions/applications/{pk}/withdraw/",
                    pk=selected_application["id"],
                ),
                data={},
            )
            messages.success(
                request,
                payload_message(withdrawn)
                or "Your promotion application was withdrawn.",
            )
            return redirect(
                f"{reverse('promotion-applications')}?application={selected_application['id']}"
            )
        except APIError as exc:
            handle_api_error(request, exc)

    if (
        request.method == "POST"
        and can_edit
        and request.POST.get("action") != "withdraw"
        and form.is_valid()
    ):
        uploads = request.FILES.getlist("evidence_images")
        if len(uploads) > 5:
            form.add_error(
                None, "You can upload up to five work samples per submission."
            )
        for upload in uploads:
            if upload.size > 5 * 1024 * 1024:
                form.add_error(None, "Each work sample must be 5 MB or smaller.")
                break
        if not form.errors:
            promotion_type = form.cleaned_data["promotion_type"]
            data = {
                "promotion_type": promotion_type,
                "service_ids": json.dumps(
                    [int(value) for value in form.cleaned_data["service_ids"]]
                ),
                "promotional_description": form.cleaned_data["promotional_description"],
            }
            if promotion_type == "INSTITUTION":
                data["institution_id"] = form.cleaned_data["institution_id"]
            elif promotion_type == "AREA":
                data["area_location_id"] = form.cleaned_data["area_location_id"]
            elif promotion_type == "STATE":
                data["state_country"] = form.cleaned_data["state_country"]
                data["state_name"] = form.cleaned_data["state_name"]
            files = [
                (
                    "evidence_images",
                    (
                        upload.name,
                        upload.file,
                        upload.content_type or "application/octet-stream",
                    ),
                )
                for upload in uploads
            ]
            try:
                if selected_application:
                    _, saved, _ = client.patch(
                        replace_path(
                            "/promotions/applications/{pk}/",
                            pk=selected_application["id"],
                        ),
                        data=data,
                        files=files or None,
                    )
                    verb = "resubmitted"
                else:
                    _, saved, _ = client.post(
                        "/promotions/applications/", data=data, files=files or None
                    )
                    verb = "submitted"
                messages.success(
                    request,
                    payload_message(saved)
                    or f"Your promotion application was {verb} for Customer Support review.",
                )
                return redirect(
                    f"{reverse('promotion-applications')}?application={saved['id']}"
                )
            except APIError as exc:
                handle_api_error(request, exc)

    applications = catalog["applications"]
    if selected_application and not any(
        item.get("id") == selected_application.get("id") for item in applications
    ):
        applications = [selected_application] + applications
    return page(
        request,
        "artisan/promotions.html",
        role="artisan",
        form=form,
        eligibility=catalog["eligibility"],
        applications=applications,
        selected_application=selected_application,
        can_edit=can_edit,
        promotion_state_pairs=[
            {"country": country, "state": state}
            for country, state in sorted(
                form.state_country_pairs,
                key=lambda pair: (pair[0].casefold(), pair[1].casefold()),
            )
        ],
        application_status_labels={
            "PENDING_REVIEW": "Pending Review",
            "CHANGES_REQUIRED": "Changes Required",
            "APPROVED": "Approved",
            "REJECTED": "Rejected",
            "WITHDRAWN": "Withdrawn",
        },
        page_js_path="js/pages/promotion-applications.js",
    )


@api_guard
def promotion_evidence_proxy(request, application_id, evidence_id):
    client = APIClient(request)
    path = replace_path(
        "/promotions/applications/{application_id}/evidence/{evidence_id}/",
        application_id=application_id,
        evidence_id=evidence_id,
    )
    try:
        content, content_type, _headers = client.get_file(path)
    except APIError as exc:
        handle_api_error(request, exc)
        return HttpResponse(
            exc.user_message,
            status=exc.status_code,
            content_type="text/plain; charset=utf-8",
        )
    response = HttpResponse(content, content_type=content_type)
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@api_guard
def verification(request):
    client = APIClient(request)
    try:
        _, status_data, _ = client.get("/services/verification/")
    except APIError as exc:
        handle_api_error(request, exc)
        if request_wants_json(request):
            error_payload = error_json_payload(exc)
            safe_error = {
                key: error_payload[key]
                for key in ("status", "message", "detail")
                if key in error_payload
            }
            return JsonResponse(
                safe_error or {"detail": str(exc)}, status=exc.status_code
            )
        status_data = {}

    if request_wants_json(request):
        # The same UI route supports the page's authenticated status polling. Keep
        # this response limited to fields needed for the flow; provider/reference
        # metadata returned by the API is never sent to the browser here.
        safe_status = {
            key: status_data[key]
            for key in ("status", "message", "method", "identity_name_available")
            if isinstance(status_data, dict) and key in status_data
        }
        return JsonResponse(safe_status)

    status = str(
        status_data.get("status", "") if isinstance(status_data, dict) else ""
    ).upper()
    identity_name_available = (
        bool(status_data.get("identity_name_available"))
        if isinstance(status_data, dict)
        else False
    )
    can_retry = status in ("UNVERIFIED", "REJECTED", "SERVICE_ERROR") or (
        status == "VERIFIED" and not identity_name_available
    )
    return page(
        request,
        "artisan/verification.html",
        verification=status_data,
        verification_can_retry=can_retry,
    )


@api_guard
def nin_verification(request):
    form = VerificationNINForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            _, payload, _ = APIClient(request).post(
                "/services/verification/nin/", cleaned_payload(form.cleaned_data)
            )
            messages.success(
                request,
                payload_message(payload) or "NIN verification request submitted.",
            )
            return redirect("verification")
        except APIError as exc:
            handle_api_error(request, exc)
    return page(
        request,
        "artisan/verification_action.html",
        form=form,
        title="NIN verification",
        intro="Submit your NIN with explicit consent. The API remains responsible for verification.",
    )


def _bvn_verification_page(request, *, bvn_form, otp_form, flow_stage, flow_message=""):
    otp_form.fields["otp"].label = "Six-digit email verification code"
    otp_form.fields["otp"].help_text = "Enter the six-digit code sent to your email."
    otp_form.fields["otp"].widget.attrs.update(
        {
            "autocomplete": "one-time-code",
            "inputmode": "numeric",
            "maxlength": "6",
        }
    )
    bvn_form.fields["bvn"].widget.attrs.update(
        {
            "inputmode": "numeric",
            "maxlength": "11",
        }
    )
    return page(
        request,
        "artisan/bvn_verification.html",
        bvn_form=bvn_form,
        otp_form=otp_form,
        flow_stage=flow_stage,
        flow_message=flow_message,
        page_js_path="js/pages/artisan-bvn_verification.js",
    )


@api_guard
def bvn_initiate(request):
    flow_message = ""
    flow_stage = request.session.get("bvn_flow_stage", "bvn")
    if flow_stage not in (
        "bvn",
        "initiating",
        "requesting_otp",
        "otp",
        "checking_result",
    ):
        flow_stage = "bvn"

    if request.method == "POST" and "otp" in request.POST:
        bvn_form = VerificationBVNForm()
        otp_form = OTPOnlyForm(request.POST)
        flow_stage = "otp"
        if otp_form.is_valid():
            try:
                _, payload, _ = APIClient(request).post(
                    "/services/verification/bvn/details/",
                    cleaned_payload(otp_form.cleaned_data),
                )
                request.session["bvn_flow_stage"] = "checking_result"
                request.session.pop("bvn_methods", None)
                flow_stage = "checking_result"
                otp_form = OTPOnlyForm()
                flow_message = (
                    payload_message(payload)
                    or "Your verification code has been submitted. Checking the result."
                )
            except APIError as exc:
                handle_api_error(request, exc)
                otp_form = OTPOnlyForm()
                flow_message = exc.user_message
    elif request.method == "POST":
        bvn_form = VerificationBVNForm(request.POST)
        otp_form = OTPOnlyForm()
        flow_stage = "bvn"
        if bvn_form.is_valid():
            try:
                _, payload, _ = APIClient(request).post(
                    "/services/verification/bvn/initiate/",
                    cleaned_payload(bvn_form.cleaned_data),
                )
                request.session["bvn_flow_stage"] = "initiating"
                request.session.pop("bvn_methods", None)
                flow_stage = "initiating"
                bvn_form = VerificationBVNForm()
                flow_message = (
                    payload_message(payload)
                    or "Your BVN verification request has been submitted."
                )
            except APIError as exc:
                handle_api_error(request, exc)
                flow_message = exc.user_message
    else:
        bvn_form = VerificationBVNForm()
        otp_form = OTPOnlyForm()

    return _bvn_verification_page(
        request,
        bvn_form=bvn_form,
        otp_form=otp_form,
        flow_stage=flow_stage,
        flow_message=flow_message,
    )


@api_guard
def bvn_method(request):
    methods = request.session.get("bvn_methods", [])
    form = VerificationMethodForm(request.POST or None)
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    if request.method == "POST" and form.is_valid():
        try:
            api_status, payload, _ = APIClient(request).post(
                "/services/verification/bvn/method/",
                cleaned_payload(form.cleaned_data),
            )
            request.session["bvn_flow_stage"] = "requesting_otp"
            if is_ajax:
                return JsonResponse(payload, status=api_status)
            messages.success(
                request,
                payload_message(payload) or "Verification code request submitted.",
            )
            return redirect("bvn-initiate")
        except APIError as exc:
            if is_ajax:
                return JsonResponse(error_json_payload(exc), status=exc.status_code)
            handle_api_error(request, exc)
    return page(request, "artisan/bvn_method.html", form=form, methods=methods)


@api_guard
def bvn_details(request):
    form = OTPOnlyForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            _, payload, _ = APIClient(request).post(
                "/services/verification/bvn/details/",
                cleaned_payload(form.cleaned_data),
            )
            request.session.pop("bvn_methods", None)
            messages.success(
                request, payload_message(payload) or "BVN verification completed."
            )
            return redirect("verification")
        except APIError as exc:
            handle_api_error(request, exc)
    return page(
        request,
        "artisan/verification_action.html",
        form=form,
        title="Complete BVN verification",
        intro="Enter the verification code supplied by the selected provider flow.",
    )


# ---------------- Discovery ----------------


def categories(request):
    try:
        _, payload, _ = APIClient(request).get("/services/categories/")
    except APIError as exc:
        handle_api_error(request, exc)
        payload = []
    return page(
        request, "discovery/categories.html", items=extract_items(payload), raw=payload
    )


def services(request):
    requested_category = " ".join(
        str(request.GET.get("category_name", "") or "").split()
    )[:120]
    client = APIClient(request)
    category_names = []
    category_keys = set()
    try:
        _, category_payload, _ = client.get("/services/categories/")
        for item in extract_items(category_payload):
            if not isinstance(item, dict):
                continue
            name = " ".join(str(item.get("name") or "").split())
            key = name.casefold()
            if name and key not in category_keys:
                category_names.append(name)
                category_keys.add(key)
    except APIError as exc:
        handle_api_error(request, exc)

    selected_category = next(
        (
            name
            for name in category_names
            if name.casefold() == requested_category.casefold()
        ),
        requested_category,
    )
    params = {"category_name": selected_category} if selected_category else {}
    try:
        _, payload, _ = client.get("/services/service_cat/", params=params or None)
    except APIError as exc:
        handle_api_error(request, exc)
        payload = []
    items = extract_items(payload)

    for item in items:
        if not isinstance(item, dict):
            continue
        name = " ".join(str(item.get("category_name") or "").split())
        key = name.casefold()
        if name and key not in category_keys:
            category_names.append(name)
            category_keys.add(key)
    if selected_category and selected_category.casefold() not in category_keys:
        category_names.append(selected_category)
        category_keys.add(selected_category.casefold())

    category_options = [
        {"name": name, "selected": name.casefold() == selected_category.casefold()}
        for name in sorted(category_names, key=str.casefold)
    ]
    return page(
        request,
        "discovery/services.html",
        items=items,
        params=params,
        categories=category_options,
        selected_category=selected_category,
    )


def _discovery_url(values):
    query = urlencode(
        [(key, value) for key, value in values.items() if value not in (None, "")]
    )
    return reverse("artisans") + (f"?{query}" if query else "")


def _discovery_categories(category_items, service_items, filters):
    """Build an A–Z category sidebar from the existing category and service catalogues."""
    category_names = {}
    services_by_category = {}

    for item in category_items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if name:
            category_names.setdefault(name.casefold(), name)

    # The service catalogue is also API data. Use its category values if the category
    # list response is temporarily incomplete, and group only declared services.
    for item in service_items:
        if not isinstance(item, dict):
            continue
        category_name = str(item.get("category_name") or "").strip()
        service_name = str(item.get("name") or "").strip()
        if not category_name:
            continue
        category_key = category_name.casefold()
        category_names.setdefault(category_key, category_name)
        if service_name:
            services_by_category.setdefault(category_key, {}).setdefault(
                service_name.casefold(), service_name
            )

    selected_category = filters.get("category_name", "").casefold()
    selected_service = filters.get("service_name", "").casefold()
    categories = []

    for category_key, category_name in sorted(
        category_names.items(), key=lambda pair: pair[1].casefold()
    ):
        all_params = dict(filters)
        all_params["category_name"] = category_name
        all_params.pop("service_name", None)
        service_links = []
        service_names = sorted(
            services_by_category.get(category_key, {}).values(), key=str.casefold
        )
        for service_name in service_names:
            service_params = dict(filters)
            service_params["category_name"] = category_name
            is_active = (
                service_name.casefold() == selected_service
                and category_name.casefold() == selected_category
            )
            if is_active:
                service_params.pop("service_name", None)
            else:
                service_params["service_name"] = service_name
            service_links.append(
                {
                    "name": service_name,
                    "url": _discovery_url(service_params),
                    "is_active": is_active,
                }
            )

        categories.append(
            {
                "name": category_name,
                "services": service_links,
                "all_url": _discovery_url(all_params),
                "is_active": category_name.casefold() == selected_category,
                "is_open": category_name.casefold() == selected_category
                or (selected_service and category_name.casefold() == selected_category),
            }
        )

    return categories


def _discovery_location_catalogue(items):
    """Keep only real marketplace location rows for the discovery filter options."""
    records = []
    seen = set()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        record = {
            key: " ".join(str(item.get(key, "") or "").split())
            for key in ("country", "state", "area")
        }
        if not all(record.values()):
            continue
        identity = tuple(record[key].casefold() for key in ("country", "state", "area"))
        if identity in seen:
            continue
        seen.add(identity)
        record["id"] = str(item.get("id") or "")
        records.append(record)
    return sorted(
        records,
        key=lambda item: (
            item["country"].casefold(),
            item["state"].casefold(),
            item["area"].casefold(),
        ),
    )


def _discovery_location_choices(locations, country="", state=""):
    def unique_values(items, field):
        values = {}
        for item in items:
            value = item.get(field, "")
            if value:
                values.setdefault(value.casefold(), value)
        return sorted(values.values(), key=str.casefold)

    countries = unique_values(locations, "country")
    country_rows = [
        item
        for item in locations
        if not country or item["country"].casefold() == country.casefold()
    ]
    states = unique_values(country_rows, "state")
    state_rows = [
        item
        for item in country_rows
        if not state or item["state"].casefold() == state.casefold()
    ]
    areas = unique_values(state_rows, "area")
    return countries, states, areas


def artisans(request):
    """Public, paginated directory. The API performs all artisan filtering."""

    def query_value(name, max_length=160):
        value = " ".join(str(request.GET.get(name, "") or "").split())
        return value[:max_length]

    filters = {
        name: query_value(name, max_length)
        for name, max_length in (
            ("q", 160),
            ("area", 150),
            ("state", 100),
            ("country", 100),
            ("category_name", 100),
            ("service_name", 150),
            ("institution_id", 64),
            ("available_anywhere", 10),
        )
    }
    try:
        offset = max(0, int(query_value("offset", 10) or "0"))
    except (TypeError, ValueError):
        offset = 0
    page_size = 20
    client = APIClient(request)

    try:
        _, category_payload, _ = client.get("/services/categories/")
        category_items = extract_items(category_payload)
    except APIError as exc:
        handle_api_error(request, exc)
        category_items = []

    try:
        _, service_payload, _ = client.get("/services/service_cat/")
        service_items = extract_items(service_payload)
    except APIError as exc:
        handle_api_error(request, exc)
        service_items = []

    from .geography import world_geography
    from .profile_catalogue import catalogue_page, institution_record

    geography = world_geography()
    location_items = []
    selected_institution = institution_record(filters["institution_id"]) if filters["institution_id"] else None
    invalid_institution = False
    if selected_institution:
        # Source keys are frontend-only; the API still matches service locality.
        for key in ("country", "state", "area"):
            filters[key] = selected_institution[key]
    elif filters["institution_id"]:
        # Preserve old UUID links for the legacy backend; reject unknown source keys.
        from uuid import UUID

        try:
            UUID(filters["institution_id"])
        except (ValueError, TypeError):
            invalid_institution = True
            messages.error(
                request, "Choose an institution from the current search results."
            )
    # Institutions are directly reduced when any geography filter is present (country/state/area)
    effective_institution_filters = {
        k: filters[k] for k in ("country", "state", "area") if filters.get(k)
    }
    institutions = catalogue_page(effective_institution_filters)["results"]
    if selected_institution:
        selected = {
            **selected_institution,
            "id": selected_institution["source_id"],
            "abbreviation": next(
                iter(selected_institution.get("abbreviations") or []), ""
            ),
            "campus": "",
        }
        if not any(row["id"] == selected["id"] for row in institutions):
            institutions = [selected] + institutions

    api_filters = {key: value for key, value in filters.items() if value}
    if selected_institution:
        api_filters.pop("institution_id", None)
    directory_unavailable = False
    try:
        if invalid_institution:
            payload = {"count": 0, "results": []}
        else:
            _, payload, _ = client.get(
                "/profile/artisan/details/",
                params={**api_filters, "limit": page_size, "offset": offset},
            )
        items = extract_items(payload)
        total_count = (
            max(0, int(payload.get("count", len(items))))
            if isinstance(payload, dict)
            else len(items)
        )
    except (APIError, TypeError, ValueError) as exc:
        if isinstance(exc, APIError):
            handle_api_error(request, exc)
        items, total_count = [], 0
        # Never present a failed lookup as "no artisans matched": the two look
        # identical to a visitor and hide real outages and rate limits.
        directory_unavailable = True

    promoted_items = (
        [] if invalid_institution else _public_promoted_discovery(request, api_filters)
    )
    categories = _discovery_categories(category_items, service_items, filters)
    country_options = sorted(
        set(geography) | ({filters["country"]} if filters["country"] else set()),
        key=str.casefold,
    )
    state_options = sorted(
        set(geography.get(filters["country"], {}))
        | ({filters["state"]} if filters["state"] else set()),
        key=str.casefold,
    )
    area_options = sorted(
        set(geography.get(filters["country"], {}).get(filters["state"], []))
        | ({filters["area"]} if filters["area"] else set()),
        key=str.casefold,
    )
    has_active_filters = any(filters.values())
    clear_service_params = dict(filters)
    clear_service_params.pop("service_name", None)
    first_result = offset + 1 if items else 0
    last_result = min(offset + len(items), total_count) if items else 0
    previous_url = (
        _discovery_url({**filters, "offset": max(0, offset - page_size)})
        if offset
        else ""
    )
    next_url = (
        _discovery_url({**filters, "offset": offset + page_size})
        if offset + len(items) < total_count
        else ""
    )

    return page(
        request,
        "discovery/artisans.html",
        items=items,
        promoted_items=promoted_items,
        current_page=offset // page_size + 1,
        page_size=page_size,
        total_count=total_count,
        first_result=first_result,
        last_result=last_result,
        previous_url=previous_url,
        next_url=next_url,
        params=filters,
        categories=categories,
        institutions=institutions,
        locations=location_items,
        discovery_geography=geography,
        country_options=country_options,
        state_options=state_options,
        area_options=area_options,
        has_active_filters=has_active_filters,
        clear_service_url=_discovery_url(clear_service_params),
        directory_unavailable=directory_unavailable,
    )


def artisan_detail(request, pk):
    try:
        _, item, _ = APIClient(request).get(
            replace_path("/profile/artisan/details/{pk}/", pk=pk)
        )
    except APIError as exc:
        handle_api_error(request, exc)
        return redirect("artisans")
    role = current_role(request) if request.session.get("api_access") else ""
    promotion_visit_token = " ".join(
        str(request.GET.get("promotion_visit") or "").split()
    )[:4000]
    return page(
        request,
        "discovery/artisan_detail.html",
        artisan=item,
        role=role,
        promotion_visit_token=promotion_visit_token,
    )


@api_guard
def start_conversation(request, pk):
    if request.method != "POST":
        return redirect("artisan-detail", pk=pk)
    detail_target = _artisan_detail_return_target(request, pk)
    role = current_role(request)
    action = {
        "kind": "service_conversation",
        "artisan_id": str(pk),
        "return_to": detail_target,
    }
    service_id = str(request.POST.get("service_id", "") or "").strip()
    if service_id.isdigit():
        action["service_id"] = service_id
    if role in {"customer", "artisan"}:
        completion = _ensure_action_profile_complete(
            request,
            action,
            role="customer",
            destination=detail_target,
        )
        if completion:
            return completion
    payload = {"artisan_id": str(pk)}
    if service_id.isdigit():
        payload["service_id"] = service_id
    try:
        _, conversation, _ = APIClient(request).post(
            "/conversations/start/",
            payload,
            idempotency_key=new_idempotency_key("conversation-start"),
        )
        return redirect("conversation-detail", conversation_id=conversation["id"])
    except APIError as exc:
        handle_api_error(request, exc)
        return redirect("artisan-detail", pk=pk)


@api_guard
def start_category_conversation(request, category_id):
    if request.method != "POST":
        return redirect("categories")
    payload = {"category_id": category_id}
    if str(request.POST.get("service_id", "")).isdigit():
        payload["service_id"] = request.POST["service_id"]
    try:
        _, room, _ = APIClient(request).post(
            "/conversations/start/",
            payload,
            idempotency_key=new_idempotency_key("category-chat"),
        )
        return redirect("conversation-detail", conversation_id=room["id"])
    except APIError as exc:
        handle_api_error(request, exc)
        return redirect("categories")


@api_guard
def conversation_inbox(request):
    try:
        _, payload, _ = APIClient(request).get(
            "/conversations/inbox/", params={k: v for k, v in request.GET.items() if v}
        )
    except APIError as exc:
        handle_api_error(request, exc)
        payload = []
    conversations = extract_items(payload)
    return page(
        request,
        "conversations/inbox.html",
        conversations=conversations,
        customer_conversations=[
            item for item in conversations if item.get("viewer_role") == "customer"
        ],
        artisan_conversations=[
            item for item in conversations if item.get("viewer_role") == "artisan"
        ],
        direct_conversations=[
            item for item in conversations if item.get("viewer_role") == "direct"
        ],
        role=current_role(request),
    )


def _private_conversation_messages(payload, conversation_id):
    for message in extract_items(payload):
        for item in message.get("attachments", []):
            if item.get("url") and item.get("id"):
                item["url"] = reverse(
                    "conversation-media-proxy",
                    kwargs={
                        "conversation_id": conversation_id,
                        "attachment_id": item["id"],
                    },
                )
    return payload


_MEDIA_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "application/pdf": ".pdf",
    "audio/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "video/webm": ".webm",
    "video/mp4": ".mp4",
}


def _conversation_media_disposition(mime):
    """Photos and recordings preview inline; documents download with a real name."""
    mime = str(mime or "").split(";")[0].strip().lower()
    extension = _MEDIA_EXTENSIONS.get(mime, "")
    if mime.startswith(("image/", "audio/", "video/")):
        return f'inline; filename="service-media{extension}"'
    return f'attachment; filename="document{extension or ".bin"}"'


@api_guard
@require_GET
def conversation_media_proxy(request, conversation_id, attachment_id):
    try:
        body, mime, _ = APIClient(request).get_file(
            f"/conversations/{conversation_id}/media/{attachment_id}/"
        )
    except APIError as exc:
        response = HttpResponse(
            "Media is not available.", status=exc.status_code, content_type="text/plain"
        )
    else:
        from .byte_ranges import bounds

        try:
            part = bounds(request.headers.get("Range", ""), len(body))
        except ValueError:
            response = HttpResponse(status=416)
            response["Content-Range"] = f"bytes */{len(body)}"
        else:
            if part:
                start, end = part
                response = HttpResponse(
                    body[start : end + 1], status=206, content_type=mime
                )
                response["Content-Range"] = f"bytes {start}-{end}/{len(body)}"
            else:
                response = HttpResponse(body, content_type=mime)
        response["Accept-Ranges"] = "bytes"
        response["Content-Disposition"] = _conversation_media_disposition(mime)
    response["Cache-Control"] = "private, no-store, max-age=0"
    response["X-Content-Type-Options"] = "nosniff"
    response["Referrer-Policy"] = "no-referrer"
    response["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return response


@api_guard
def conversation_messages(request, conversation_id):
    try:
        _, payload, headers = APIClient(request).get(
            f"/conversations/{conversation_id}/messages/",
            params={"limit": request.GET.get("limit", "60")},
        )
        from django.http import JsonResponse

        response = JsonResponse(
            _private_conversation_messages(payload, conversation_id), status=200
        )
        response["Cache-Control"] = "no-store"
        return response
    except APIError as exc:
        return JsonResponse({"message": exc.user_message}, status=exc.status_code)


def _conversation_local_datetime(value):
    """Render ISO API times as device-local input defaults, with Lagos as the no-JS fallback."""
    if not value:
        return ""
    try:
        parsed = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        )
    except (TypeError, ValueError):
        return ""
    lagos = ZoneInfo("Africa/Lagos")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=lagos)
    return parsed.astimezone(lagos).strftime("%Y-%m-%dT%H:%M")


def _offer_materials_payload(raw):
    """Parse the offer form's JSON materials list; unknown shapes fail closed to []."""
    import json as _json

    if not raw:
        return []
    try:
        parsed = _json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    items = []
    for entry in parsed[:50]:
        if not isinstance(entry, dict):
            continue
        item = {
            "name": str(entry.get("name", ""))[:200],
            "quantity": str(entry.get("quantity", ""))[:32],
            "unit": str(entry.get("unit", ""))[:40],
            "unit_price": str(entry.get("unit_price", ""))[:32],
        }
        if item["name"]:
            items.append(item)
    return items


def _offer_payload_from_post(post):
    """The itemised pricing payload — exactly the fields the form collects."""
    return {
        "labour_amount": post.get("labour_amount", ""),
        "materials_amount": post.get("materials_amount", "0") or "0",
        "total_amount": post.get("total_amount", ""),
        "materials": _offer_materials_payload(post.get("materials_json", "")),
        "transport_amount": post.get("transport_amount", "0") or "0",
        "additional_amount": post.get("additional_amount", "0") or "0",
        "additional_charge_details": post.get("additional_charge_details", ""),
        "materials_included": post.get("materials_included", "") == "true",
        "scheduled_start": _offer_datetime_payload(post.get("scheduled_start")),
        "duration_value": (post.get("duration_value", "") or "").strip(),
        "duration_unit": (post.get("duration_unit", "") or "").strip().upper(),
    }


OFFER_FIELD_LABELS = {
    "labour_amount": "Labour (₦)",
    "materials_amount": "Materials subtotal",
    "materials": "Items",
    "total_amount": "Payable total",
    "transport_amount": "Transport (₦)",
    "additional_amount": "Additional charges (₦)",
    "additional_charge_details": "Additional charge description",
    "scheduled_start": "Scheduled start",
    "duration_value": "Duration value",
    "duration_unit": "Duration unit",
    "parent_offer_id": "Quote being revised",
    "materials_included": "Materials included",
    "non_field_errors": "Offer",
}


def _api_error_items(exc):
    """Flatten a DRF error body into (field, message) pairs."""
    payload = getattr(exc, "detail", None) or getattr(exc, "payload", None)
    if isinstance(payload, dict):
        details = payload.get("details", payload)
        if isinstance(details, dict):
            pairs = []
            for field, messages in details.items():
                if isinstance(messages, (list, tuple)):
                    for message in messages:
                        pairs.append((str(field), str(message)))
                else:
                    pairs.append((str(field), str(messages)))
            return pairs
        if isinstance(details, (list, tuple)):
            return [("non_field_errors", str(message)) for message in details]
    return [("non_field_errors", str(payload or "The offer could not be sent."))]


def _offer_error_context(exc):
    pairs = _api_error_items(exc)
    messages = [
        f"{OFFER_FIELD_LABELS.get(field, field.replace('_', ' ').title())}: {message}"
        if field != "non_field_errors"
        else message
        for field, message in pairs
    ]
    return messages, {field: message for field, message in pairs}


def _offer_draft_context(post):
    """Everything the artisan typed, so a failed send never blanks the form."""
    import json as _json

    return {
        "labour_amount": post.get("labour_amount", ""),
        "materials_json": post.get("materials_json", "[]") or "[]",
        "materials_amount": post.get("materials_amount", "0") or "0",
        "materials_included": post.get("materials_included", "") == "true",
        "transport_amount": post.get("transport_amount", "0") or "0",
        "additional_amount": post.get("additional_amount", "0") or "0",
        "additional_charge_details": post.get("additional_charge_details", ""),
        "scheduled_start": post.get("scheduled_start", ""),
        "duration_value": post.get("duration_value", ""),
        "duration_unit": post.get("duration_unit", "DAYS") or "DAYS",
        "total_amount": post.get("total_amount", ""),
        "materials": _offer_materials_payload(post.get("materials_json", "")),
    }


def _offer_datetime_payload(value):
    """Treat datetime-local fallbacks as Lagos time; preserve explicit browser offsets/UTC values."""
    if not value:
        return value
    try:
        parsed = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        )
    except (TypeError, ValueError):
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Africa/Lagos"))
    return parsed.isoformat()


@require_GET
@require_GET
def service_worker(request):
    worker_path = settings.BASE_DIR / "static" / "js" / "service-worker.js"
    try:
        worker_contents = worker_path.read_text(encoding="utf-8")
    except OSError:
        return HttpResponse("Service worker unavailable.", status=404)
    response = HttpResponse(
        worker_contents, content_type="application/javascript; charset=utf-8"
    )
    response["Service-Worker-Allowed"] = "/"
    response["Cache-Control"] = "no-cache"
    return response


def _conversation_submit_payload(post_data):
    """Build the single explicit request submission payload."""
    payload = {}
    nullable_fields = {
        "service_id",
        "category_id",
        "location_id",
        "preferred_date",
        "preferred_time_start",
        "preferred_time_end",
        "budget",
    }
    for field in CONVERSATION_REQUEST_FIELDS:
        if field not in post_data:
            continue
        value = post_data.get(field, "")
        if field == "budget_unknown":
            payload[field] = str(value).strip().casefold() in {"true", "1", "yes", "on"}
        elif field in nullable_fields and value in ("", None):
            payload[field] = None
        else:
            payload[field] = value
    payload["submit_request"] = True
    return payload


def _conversation_request_error_payload(exc):
    allowed_fields = set(CONVERSATION_REQUEST_FIELDS) | {"request", "submit_request"}
    field_errors = {}
    if isinstance(exc.payload, dict):
        for field in allowed_fields:
            value = exc.payload.get(field)
            if isinstance(value, str) and value.strip():
                field_errors[field] = value.strip()[:500]
            elif isinstance(value, (list, tuple)):
                messages_for_field = [
                    str(item).strip()[:500]
                    for item in value
                    if isinstance(item, str) and str(item).strip()
                ]
                if messages_for_field:
                    field_errors[field] = " ".join(messages_for_field)
    return {
        **error_json_payload(exc),
        "field_errors": field_errors,
    }


PHASE_STEP_DEFINITIONS = (
    ("REQUEST", "Request & Chat", "Describe the work and agree the request details."),
    ("OFFER", "Offer & Negotiation", "Review scope, price, and the agreed schedule."),
    ("PAY", "Booking & Escrow", "The original booking payment is verified and held in escrow."),
    ("PROGRESS", "Service in Progress", "Share updates and photos in this conversation."),
    ("ADD_ONS", "Separate add-on orders", "Optional extra work is quoted and funded separately."),
    ("COMPLETE", "Completion & Review", "The customer confirms completion and leaves a review."),
    ("DISPUTE", "Support (if needed)", "Dispute handling keeps relevant escrow funds frozen."),
)

_PHASE_STATE_INDEX = {
    "OPEN": 0,
    "NEGOTIATING": 1,
    "AWAITING_PAYMENT": 2,
    "BOOKING": 2,
    "ACCEPTED": 2,
    "ACTIVE": 3,
    "IN_PROGRESS": 3,
    "COMPLETION_REQUESTED": 5,
    "COMPLETED": 6,
    "DISPUTED": 6,
    # A cancelled or declined booking stopped at the booking step; the track
    # keeps its progress instead of snapping back to step one.
    "CLOSED": 2,
    "CANCELLED": 2,
    "REJECTED": 2,
}


def _fallback_phase_steps(conversation):
    """Rebuild the project track when an older API payload omits `journey`."""
    state = str(conversation.get("state") or "OPEN").upper()
    current_index = _PHASE_STATE_INDEX.get(state, 0)
    steps = []
    for index, (key, title, description) in enumerate(PHASE_STEP_DEFINITIONS):
        steps.append(
            {
                "step": index + 1,
                "key": key,
                "title": title,
                "description": description,
                "complete": index < current_index,
                "current": index == current_index,
                "optional": key in {"ADD_ONS", "DISPUTE"},
            }
        )
    return steps


def _request_still_being_written(conversation):
    """True while the customer has not yet sent the request to the artisan.

    The API reports a submitted request under `service_request` and keeps the
    lifecycle status of anything else under `request_reference`; a request that
    was never written has neither. All three states mean the same thing to the
    customer: the chat is open, but the message box stays shut until they press
    "Request service".
    """
    if not isinstance(conversation, dict):
        return False
    if conversation.get("viewer_role") != "customer":
        return False
    if conversation.get("booking"):
        return False
    if conversation.get("service_request"):
        return False
    reference = conversation.get("request_reference")
    status = ""
    if isinstance(reference, dict):
        status = str(reference.get("status") or "").upper()
    return status in {"", "DRAFT"}


def _conversation_request_summary(conversation):
    """Read-only brief from authoritative server data; never a form."""
    source = conversation.get("service_request") or conversation.get("request_draft") or {}
    if not isinstance(source, dict) or not source:
        return {}
    return {
        "service": (
            source.get("service")
            or source.get("category")
            or source.get("service_name")
            or source.get("category_name")
            or ""
        ),
        "location": source.get("location") or source.get("location_label") or "",
        "preferred_date": source.get("preferred_date") or "",
        "budget": source.get("budget") or "",
        "description": source.get("description") or "",
    }


@api_guard
def conversation_detail(request, conversation_id):
    client = APIClient(request)
    offer_errors = []
    offer_field_errors = {}
    offer_draft = {}
    offer_form = ""
    try:
        if request.method == "POST":
            action = request.POST.get("action", "message")
            # The key travels with the form, so retrying a failed send replays the
            # same operation instead of creating a second offer or booking.
            key = (
                request.POST.get("idempotency_key", "").strip()
                or new_idempotency_key(f"conversation-{action}")
            )
            if action == "request_submit":
                # The chat opens on the artisan, but nothing can be said or
                # offered until the customer has described the job and pressed
                # Request service. The API builds the request from these fields
                # and validates completeness.
                payload = {
                    field: request.POST.get(field, "").strip()
                    for field in CONVERSATION_REQUEST_FIELDS
                    if field in request.POST
                }
                payload["submit_request"] = True
                try:
                    client.patch(
                        f"/conversations/{conversation_id}/request/",
                        payload,
                        idempotency_key=key,
                    )
                except APIError as exc:
                    handle_api_error(request, exc)
                    return redirect(
                        "conversation-detail", conversation_id=conversation_id
                    )
                messages.success(
                    request,
                    "Request sent. The artisan can now send you an itemised offer.",
                )
                return redirect(
                    "conversation-detail", conversation_id=conversation_id
                )
            if action == "request_close":
                # Explicit workflow action: close an unbooked request. No drafts.
                client.patch(
                    f"/conversations/{conversation_id}/request/",
                    {"close_request": True, "submit_request": False},
                    idempotency_key=key,
                )
                messages.success(
                    request,
                    "Request closed. Recordings are retained for seven days unless a preservation hold applies.",
                )
                return redirect(
                    "conversation-detail", conversation_id=conversation_id
                )
            if action == "message":
                body = request.POST.get("body", "").strip()
                files = [
                    ("attachments", (item.name, item.file, item.content_type))
                    for item in request.FILES.getlist("attachments")
                ]
                _, conversation_context, _ = client.get(
                    f"/conversations/{conversation_id}/"
                )
                role = conversation_context.get("viewer_role")
                if role not in {"customer", "artisan"}:
                    role = current_role(request)
                destination = reverse(
                    "conversation-detail", kwargs={"conversation_id": conversation_id}
                )
                completion_action = {
                    "kind": "conversation_message",
                    "conversation_id": str(conversation_id),
                    "body": body[:5000],
                    "had_attachments": bool(files),
                }
                completion = _ensure_action_profile_complete(
                    request,
                    completion_action,
                    role=role,
                    destination=destination,
                )
                if completion:
                    return completion
                client.post(
                    f"/conversations/{conversation_id}/messages/",
                    {"body": body},
                    files=files or None,
                    idempotency_key=key,
                )
            elif action == "offer":
                payload = _offer_payload_from_post(request.POST)
                try:
                    client.post(
                        f"/conversations/{conversation_id}/offers/",
                        payload,
                        idempotency_key=key,
                    )
                except APIError as exc:
                    if exc.status_code not in (400, 409):
                        raise
                    # Keep the artisan on the page with every entry intact.
                    offer_errors, offer_field_errors = _offer_error_context(exc)
                    offer_draft = _offer_draft_context(request.POST)
                    offer_form = "new"
                else:
                    messages.success(request, "Offer sent to the other participant.")
            elif action == "accept":
                client.post(
                    f"/conversations/{conversation_id}/offers/{request.POST.get('offer_id')}/accept/",
                    {},
                    idempotency_key=key,
                )
                messages.success(
                    request,
                    "Offer accepted. Your booking and payment step are now available.",
                )
            elif action in {"decline", "revision"}:
                note = request.POST.get("note", "").strip()[:5000]
                client.post(
                    f"/conversations/{conversation_id}/offers/{request.POST.get('offer_id')}/decline/",
                    {"request_revision": action == "revision"},
                    idempotency_key=key,
                )
                if action == "revision" and note:
                    # The customer explains in their own words what should change;
                    # it lands in the chat so the artisan can edit the quote and
                    # resend it as a new revision.
                    client.post(
                        f"/conversations/{conversation_id}/messages/",
                        {"body": note},
                        idempotency_key=new_idempotency_key("revision-note"),
                    )
                messages.success(
                    request,
                    (
                        "Revision requested. The artisan will edit the quote and send it back."
                        if action == "revision"
                        else "Offer declined. No new price was accepted."
                    ),
                )
            elif action == "counter":
                counter_payload = _offer_payload_from_post(request.POST)
                counter_payload["parent_offer_id"] = request.POST.get(
                    "parent_offer_id", ""
                )
                try:
                    client.post(
                        f"/conversations/{conversation_id}/offers/counter/",
                        counter_payload,
                        idempotency_key=key,
                    )
                except APIError as exc:
                    if exc.status_code not in (400, 409):
                        raise
                    offer_errors, offer_field_errors = _offer_error_context(exc)
                    offer_draft = _offer_draft_context(request.POST)
                    offer_form = "counter"
                else:
                    messages.success(request, "Counter offer sent.")
            elif action == "offer_resend":
                offer_id = request.POST.get("offer_id", "").strip()
                if not offer_id:
                    raise APIError(400, {"detail": "The agreement is missing."})
                client.post(
                    f"/conversations/{conversation_id}/offers/{offer_id}/resend/",
                    {"scheduled_start": request.POST.get("scheduled_start", "").strip() or None},
                    idempotency_key=key,
                )
                messages.success(
                    request,
                    "Agreement resent. The customer can accept it again.",
                )
            elif action == "offer_renotify":
                offer_id = request.POST.get("offer_id", "")
                if not offer_id:
                    raise APIError(400, {"detail": "The offer is missing."})
                client.post(
                    f"/conversations/{conversation_id}/offers/{offer_id}/renotify/",
                    {},
                    idempotency_key=key,
                )
                messages.success(
                    request, "The other participant has been notified about this offer."
                )
            elif action == "additional_order_propose":
                _, current, _ = client.get(f"/conversations/{conversation_id}/")
                booking_id = (current.get("booking") or {}).get("id")
                if not booking_id:
                    raise APIError(
                        409,
                        {"detail": "There is no active booking for this conversation."},
                    )
                payload = {
                    "amount": request.POST.get("amount", "").strip(),
                    "description": request.POST.get("description", "").strip(),
                }
                client.post(
                    f"/booking/{booking_id}/additional-orders/",
                    payload,
                    idempotency_key=key,
                )
                messages.success(
                    request,
                    "Separate add-on order sent. The original booking price and scope are unchanged.",
                )
            elif action == "additional_order_respond":
                _, current, _ = client.get(f"/conversations/{conversation_id}/")
                booking_id = (current.get("booking") or {}).get("id")
                order_id = request.POST.get("order_id", "")
                offer_id = request.POST.get("offer_id", "")
                if not booking_id or not order_id or not offer_id:
                    raise APIError(
                        400, {"detail": "The add-on order proposal is missing."}
                    )
                response_action = request.POST.get("response_action", "")
                payload = {"action": response_action}
                if response_action == "counter":
                    payload["amount"] = request.POST.get("amount", "").strip()
                    payload["description"] = request.POST.get("description", "").strip()
                client.post(
                    f"/booking/{booking_id}/additional-orders/{order_id}/offers/{offer_id}/respond/",
                    payload,
                    idempotency_key=key,
                )
                messages.success(
                    request,
                    {
                        "accept": "Separate add-on order approved. Payment must be confirmed through escrow before service completion.",
                        "decline": "Separate add-on order declined. No extra payment was collected.",
                        "counter": "Counter-offer sent on the separate add-on order.",
                    }.get(response_action, "Add-on order updated."),
                )
            elif action == "additional_order_pay":
                order_id = request.POST.get("order_id", "")
                if not order_id:
                    raise APIError(400, {"detail": "Add-on order is missing."})
                _, payment_payload, _ = client.post(
                    f"/payments/additional-orders/{order_id}/initialize/",
                    {},
                    idempotency_key=key,
                )
                payment_id = (
                    payment_payload.get("payment_id")
                    if isinstance(payment_payload, dict)
                    else None
                )
                if payment_id:
                    messages.info(
                        request,
                        "Add-on payment is being prepared through the provider. It is not paid until server-side verification confirms escrow.",
                    )
                    return redirect("payment-detail", payment_id=payment_id)
            elif action in {"pay", "pay_status"}:
                payment_id = request.POST.get("payment_id", "").strip()
                if action == "pay" or not payment_id:
                    _, payment_payload, _ = client.post(
                        f"/conversations/{conversation_id}/payment/initialize/",
                        {},
                        idempotency_key=key,
                    )
                    payment_id = (
                        str(payment_payload.get("payment_id") or "")
                        if isinstance(payment_payload, dict)
                        else ""
                    )
                else:
                    payment_payload = {}
                _remember_payment_return(request, conversation_id)
                # The escrow payment is completed here, in the chat: straight to
                # the provider checkout, never through a booking or payment page.
                checkout_url = (
                    _checkout_url_for(client, payment_id, payment_payload)
                    if payment_id
                    else ""
                )
                if not checkout_url and payment_id and action == "pay_status":
                    # The stored link expired, or predates the card/bank-transfer
                    # rule, so the API hides it. Ask for a fresh checkout instead
                    # of leaving the payer on a dead provider page.
                    _, renewed, _ = client.post(
                        f"/conversations/{conversation_id}/payment/initialize/",
                        {},
                        idempotency_key=f"{key}-renew",
                    )
                    checkout_url = _checkout_url_for(client, payment_id, renewed)
                if checkout_url:
                    return redirect(checkout_url)
                # No usable link: say why, in the provider's own words, instead of
                # leaving the payer on a spinner with nothing to act on.
                reason = ""
                if payment_id:
                    try:
                        _, snapshot, _ = client.get(
                            replace_path("/payments/{payment_id}/", payment_id=payment_id)
                        )
                        reason = str((snapshot or {}).get("failure_reason") or "").strip()
                    except APIError:
                        reason = ""
                if reason:
                    messages.info(
                        request,
                        f"The payment provider refused this checkout: {reason} "
                        "Nothing has been charged. Fix that, then tap “Check again”.",
                    )
                else:
                    messages.info(
                        request,
                        "Preparing the secure payment link. Tap “Check again” in a moment — nothing has been charged.",
                    )
            elif action in {"pay_transfer", "pay_transfer_check"}:
                payment_id = request.POST.get("payment_id", "").strip()
                if action == "pay_transfer" and not payment_id:
                    _, payment_payload, _ = client.post(
                        f"/conversations/{conversation_id}/payment/initialize/",
                        {},
                        idempotency_key=f"{key}-transfer",
                    )
                    payment_id = str(
                        (payment_payload or {}).get("payment_id") or ""
                    ) if isinstance(payment_payload, dict) else ""
                if not payment_id:
                    messages.info(
                        request,
                        "The payment is not ready yet. Nothing has been charged — try again in a moment.",
                    )
                    return redirect(
                        "conversation-detail", conversation_id=conversation_id
                    )
                try:
                    if action == "pay_transfer":
                        # Ask the provider for a dedicated account and show it here.
                        # "renew" means the last account closed: the provider will
                        # not reuse that reference, so the API retires it and mints
                        # a new one against a fresh reference.
                        renew = str(request.POST.get("renew") or "").strip() in {"1", "true"}
                        if renew:
                            messages.info(
                                request,
                                "Getting a fresh transfer account. Nothing has been charged.",
                            )
                        client.post(
                            replace_path(
                                "/payments/{payment_id}/bank-transfer/",
                                payment_id=payment_id,
                            ),
                            {"renew": renew},
                            idempotency_key=f"{key}-account{'-renew' if renew else ''}",
                        )
                    else:
                        client.post(
                            replace_path(
                                "/payments/{payment_id}/bank-transfer/verify/",
                                payment_id=payment_id,
                            ),
                            {},
                            idempotency_key=f"{key}-confirm",
                        )
                        messages.info(
                            request,
                            "Checking that transfer with your bank now. This chat updates by itself — nothing more to tap.",
                        )
                except APIError as exc:
                    handle_api_error(request, exc)
                    return redirect(
                        "conversation-detail", conversation_id=conversation_id
                    )
                _remember_payment_return(request, conversation_id)
                return redirect(
                    reverse(
                        "conversation-detail",
                        kwargs={"conversation_id": conversation_id},
                    )
                    + f"?transfer={payment_id}"
                )
            elif action in {
                "start_booking",
                "complete",
                "confirm_completion",
                "cancel",
            }:
                _, current, _ = client.get(f"/conversations/{conversation_id}/")
                booking = current.get("booking") or {}
                booking_id = booking.get("id")
                if not booking_id:
                    raise APIError(
                        409,
                        {"detail": "There is no active booking for this conversation."},
                    )
                booking_path = {
                    "start_booking": f"/booking/{booking_id}/start/",
                    "complete": f"/booking/{booking_id}/complete/",
                    "confirm_completion": f"/booking/{booking_id}/confirm-completion/",
                    "cancel": f"/booking/{booking_id}/cancel/",
                }[action]
                payload = {}
                if action == "complete":
                    payload["note"] = request.POST.get("note", "")
                elif action == "cancel":
                    payload = {
                        "reason": request.POST.get("reason", "").strip()
                        or "Customer requested cancellation.",
                        "reason_code": request.POST.get("reason_code", "OTHER"),
                    }
                client.post(booking_path, payload, idempotency_key=key)
                messages.success(request, "The booking has been updated.")
            elif action == "dispute":
                reason = (
                    request.POST.get("reason", "").strip()
                    or "Please review this service issue."
                )
                _, disputed_conversation, _ = client.post(
                    f"/conversations/{conversation_id}/dispute/",
                    {"reason": reason},
                    idempotency_key=key,
                )
                dispute_case = (
                    disputed_conversation.get("dispute_case")
                    if isinstance(disputed_conversation, dict)
                    else None
                )
                case_id = (
                    dispute_case.get("id") if isinstance(dispute_case, dict) else None
                )
                if case_id:
                    try:
                        client.post(
                            replace_path("/support/cases/{pk}/messages/", pk=case_id),
                            {"message": reason},
                            idempotency_key=request.POST.get("support_message_key")
                            or new_idempotency_key("conversation-dispute-message"),
                        )
                    except APIError as exc:
                        handle_api_error(request, exc)
                        messages.warning(
                            request,
                            "The dispute was created, but the message could not be confirmed. You can send it from the support case composer.",
                        )
                        return redirect("support-case-detail", pk=case_id)
                    messages.success(
                        request, "Your dispute message was sent to customer support."
                    )
                    response = redirect("support-case-detail", pk=case_id)
                    response["Location"] = f"{response['Location']}#latest-message"
                    return response
                messages.success(request, "Your dispute has been submitted to support.")
            elif action == "read":
                payload = {}
                if request.POST.get("last_message_id"):
                    payload["last_message_id"] = request.POST["last_message_id"]
                _, read_state, _ = client.post(
                    f"/conversations/{conversation_id}/read/", payload
                )
                if request_wants_json(request):
                    return JsonResponse(read_state)
            if offer_errors:
                messages.error(
                    request,
                    "The offer was not sent. Nothing was booked and no payment was taken.",
                )
            else:
                return redirect(
                    "conversation-detail", conversation_id=conversation_id
                )
        _, conversation, _ = client.get(f"/conversations/{conversation_id}/")
        journey = conversation.get("journey")
        if not isinstance(journey, list) or not journey:
            journey = _fallback_phase_steps(conversation)
            conversation = dict(conversation)
            conversation["journey"] = journey
        current_phase = next(
            (step for step in journey if isinstance(step, dict) and step.get("current")),
            None,
        ) or {
            "step": 1,
            "title": str(conversation.get("state") or "Request & Chat").replace("_", " ").title(),
            "description": "Describe the work and agree the request details.",
        }
        active_offer = conversation.get("active_offer")
        if isinstance(active_offer, dict):
            active_offer = dict(active_offer)
            for field in ("scheduled_start", "scheduled_end", "expires_at"):
                active_offer[f"{field}_local"] = _conversation_local_datetime(
                    active_offer.get(field)
                )
            conversation["active_offer"] = active_offer
        _, messages_payload, _ = client.get(
            f"/conversations/{conversation_id}/messages/", params={"limit": "60"}
        )
        offer_history = []
        if conversation.get("conversation_type") == "SERVICE":
            _, offers_payload, _ = client.get(
                f"/conversations/{conversation_id}/offers/", params={"limit": "8"}
            )
            offer_history = extract_items(offers_payload)
        active_offer_json = conversation.get("active_offer") or {}

        # The chat owns the escrow payment: read the live payment so the strip can
        # offer the provider checkout directly, without leaving this page.
        payment_checkout_url = ""
        payment_id = ""
        bank_transfer = {}
        request_is_draft = _request_still_being_written(conversation)
        payment_state = conversation.get("payment") or {}
        payment_paid = (
            str(payment_state.get("status") or "").upper() in PAID_PAYMENT_STATUSES
        )
        if isinstance(payment_state, dict) and payment_state.get("id"):
            payment_id = str(payment_state["id"])
            if str(payment_state.get("status") or "").upper() == "PAYMENT_PENDING":
                live_payment = _payment_snapshot(client, payment_id)
                payment_checkout_url = _live_checkout_url(live_payment)
                # The provider hands us a dedicated account number; the payer
                # copies it from this page instead of opening the hosted page.
                if isinstance(live_payment.get("bank_transfer"), dict):
                    bank_transfer = live_payment["bank_transfer"]

        # After a cancellation the artisan can put the same agreement back in
        # front of the customer, either as-is or edited first.
        resend_offer_id = ""
        viewer_is_artisan = conversation.get("viewer_role") == "artisan"
        booking_status = str((conversation.get("booking") or {}).get("status") or "")
        if viewer_is_artisan and booking_status == "CANCELLED":
            accepted = [
                item
                for item in offer_history
                if isinstance(item, dict) and item.get("status") == "ACCEPTED"
            ]
            if accepted:
                resend_offer_id = str(accepted[0].get("id") or "")
        requested_resend = (request.GET.get("resend") or "").strip()
        if requested_resend and viewer_is_artisan:
            source = next(
                (
                    item
                    for item in offer_history
                    if isinstance(item, dict) and str(item.get("id")) == requested_resend
                ),
                None,
            )
            if source:
                offer_draft = {
                    "labour_amount": str(source.get("labour_amount") or ""),
                    "materials_json": json.dumps(
                        [
                            {
                                "name": row.get("name") or "",
                                "quantity": str(row.get("quantity") or ""),
                                "unit": row.get("unit") or "",
                                "unit_price": str(row.get("unit_price") or ""),
                            }
                            for row in (source.get("materials") or [])
                        ]
                    ),
                    "materials_amount": str(source.get("materials_amount") or "0"),
                    "total_amount": str(source.get("total_amount") or ""),
                    "scheduled_start": _conversation_local_datetime(
                        source.get("scheduled_start")
                    ),
                    "duration_value": str(source.get("duration_value") or ""),
                    "duration_unit": source.get("duration_unit") or "DAYS",
                    "transport_amount": str(source.get("transport_amount") or "0"),
                    "additional_amount": str(source.get("additional_amount") or "0"),
                    "additional_charge_details": source.get(
                        "additional_charge_details"
                    )
                    or "",
                    "materials_included": bool(source.get("materials_included")),
                }
                offer_form = "new"

        counter_prefill = {
            "labour_amount": str(active_offer_json.get("labour_amount") or ""),
            "materials": active_offer_json.get("materials") or [],
            "transport_amount": str(active_offer_json.get("transport_amount") or "0"),
            "additional_amount": str(active_offer_json.get("additional_amount") or "0"),
            "additional_charge_details": active_offer_json.get(
                "additional_charge_details"
            )
            or "",
            "scheduled_start": active_offer_json.get("scheduled_start_local") or "",
            "duration_value": str(active_offer_json.get("duration_value") or ""),
            "duration_unit": active_offer_json.get("duration_unit") or "DAYS",
            "materials_included": bool(active_offer_json.get("materials_included")),
        }
        response = page(
            request,
            "conversations/detail.html",
            conversation=conversation,
            conversation_messages=extract_items(
                _private_conversation_messages(messages_payload, conversation_id)
            ),
            offer_history=offer_history,
            role=conversation.get("viewer_role"),
            nav_role=current_role(request),
            dispute_message_key=new_idempotency_key("conversation-dispute-message"),
            current_phase=current_phase,
            request_brief=_conversation_request_summary(conversation),
            can_edit_request=(
                conversation.get("viewer_role") == "customer"
                and conversation.get("conversation_type") == "SERVICE"
                and not conversation.get("booking")
            ),
            offer_errors=offer_errors if request.method == "POST" else [],
            offer_field_errors=offer_field_errors,
            offer_draft=offer_draft,
            offer_form=offer_form,
            offer_action_key=new_idempotency_key("conversation-offer"),
            resend_offer_id=resend_offer_id,
            resend_mode=bool(request.GET.get("resend")),
            payment_id=payment_id,
            payment_checkout_url=payment_checkout_url,
            bank_transfer=bank_transfer,
            payment_paid=payment_paid,
            request_is_draft=request_is_draft,
            payment_status_url=(
                reverse("payment-status", kwargs={"payment_id": payment_id})
                if payment_id
                else ""
            ),
            offer_counter_key=new_idempotency_key("conversation-counter"),
            counter_prefill=json.dumps(counter_prefill).replace("'", "&#x27;"),
            duration_unit_options=[
                ("HOURS", "Hours"),
                ("DAYS", "Days"),
                ("WEEKS", "Weeks"),
            ],
        )
        if offer_errors:
            response.status_code = 400
        return response
    except APIError as exc:
        handle_api_error(request, exc)
        if request.method == "POST" and exc.status_code not in (401, 403, 404):
            return redirect("conversation-detail", conversation_id=conversation_id)
        return redirect("conversation-inbox")


@api_guard
def locations(request):
    client = APIClient(request)
    params = {k: v for k, v in request.GET.items() if v and k != "create"}
    form = LocationForm(request.POST or None)
    if (
        request.method == "POST"
        and form.is_valid()
        and request.POST.get("_create") == "1"
    ):
        try:
            _, location, _ = client.post(
                "/services/locations/", cleaned_payload(form.cleaned_data)
            )
            messages.success(request, "Location created.")
            return redirect("location-detail", pk=location["id"])
        except APIError as exc:
            handle_api_error(request, exc)
    try:
        _, payload, _ = client.get("/services/locations/", params=params or None)
    except APIError as exc:
        handle_api_error(request, exc)
        payload = []
    return page(
        request,
        "discovery/locations.html",
        locations=extract_items(payload),
        params=params,
        form=form,
    )


@api_guard
def location_detail(request, pk):
    client = APIClient(request)
    try:
        _, location, _ = client.get(replace_path("/services/locations/{pk}/", pk=pk))
    except APIError as exc:
        handle_api_error(request, exc)
        return redirect("locations")
    form = LocationForm(request.POST or None, initial=location)
    if request.method == "POST" and request.POST.get("_delete") == "1":
        try:
            client.delete(replace_path("/services/locations/{pk}/", pk=pk))
            messages.success(request, "Location removed.")
        except APIError as exc:
            handle_api_error(request, exc)
        return redirect("locations")
    if request.method == "POST" and form.is_valid():
        try:
            _, location, _ = client.patch(
                replace_path("/services/locations/{pk}/", pk=pk),
                cleaned_payload(form.cleaned_data),
            )
            messages.success(request, "Location updated.")
            return redirect("location-detail", pk=pk)
        except APIError as exc:
            handle_api_error(request, exc)
    return page(request, "discovery/location_detail.html", location=location, form=form)


# ---------------- Service requests ----------------

SERVICE_REQUEST_PAGE_SIZE = 12
ARTISAN_REQUEST_FILTERS = (
    "service_name",
    "location_country",
    "location_area",
    "urgency",
    "q",
)
RESPONDER_FILTERS = ("q", "state", "area", "min_rating", "ordering")


def _request_form_catalog(request, client):
    return _artisan_service_catalog(request, client)


def _request_geography(locations):
    from .geography import world_geography

    # Existing canonical marketplace locations remain usable alongside worldwide data.
    data = {country: dict(states) for country, states in world_geography().items()}
    for location in locations:
        country, state, area = (
            location.get(key, "") for key in ("country", "state", "area")
        )
        if country and state and area:
            states = data.setdefault(country, {})
            states[state] = sorted(
                set(states.get(state, [])) | {area}, key=str.casefold
            )
    return data


def _request_picker_catalogue(form):
    """Catalogue data for the request pickers (categories, services, locations)."""
    services = []
    for category, names in form.fields["service_name"].choices:
        if not category:
            continue
        for name, label in names:
            services.append({"name": str(label), "category_name": str(category)})
    categories = [
        {"name": str(value)}
        for value, label in form.fields["category_name"].choices
        if value
    ]
    from .profile_catalogue import all_institutions

    return {
        "categories": categories,
        "services": services,
        "locations": form.location_records,
        "geography": _request_geography(form.location_records),
        # Optional "closest institution" picker: same catalogue, ordering and
        # filter semantics as the artisan service form (Nigeria first,
        # alphabetical, reduced by any chosen geography).
        "institutions": [
            {
                "id": str(row.get("source_id") or ""),
                "name": str(row.get("name") or ""),
                "abbreviation": next(iter(row.get("abbreviations") or []), ""),
                "country": str(row.get("country") or ""),
                "state": str(row.get("state") or ""),
                "area": str(row.get("area") or ""),
            }
            for row in all_institutions()
            if isinstance(row, dict) and row.get("source_id")
        ],
        # The browser clock can sit in another time zone; the marketplace's
        # "today" is what the API validates against, so ship it with the data.
        "today": timezone.localdate().isoformat(),
    }


def _service_request_form(request, catalog, *, data=None, initial=None):
    return ServiceRequestForm(
        data,
        initial=initial,
        categories=catalog.get("categories", []),
        services=catalog.get("services", []),
        locations=catalog.get("locations", []),
    )


def _service_request_payload(cleaned, locations):
    location_id = str(cleaned.get("location_id") or "")
    location = next(
        (
            item
            for item in locations
            if isinstance(item, dict) and str(item.get("id") or "") == location_id
        ),
        None,
    )
    if not location:
        from .geography import canonical_location

        location = canonical_location(
            cleaned.get("location_country_filter"),
            cleaned.get("location_state_filter"),
            cleaned.get("location_area_filter"),
        )
    if not location:
        return None
    payload = cleaned_payload(
        {
            key: value
            for key, value in cleaned.items()
            if key not in {"location_id", "budget"}
        }
    )
    if "budget" in cleaned:
        payload["budget"] = (
            str(cleaned["budget"]) if cleaned["budget"] is not None else None
        )
    payload["location"] = {
        "country": str(location.get("country") or "").strip(),
        "state": str(location.get("state") or "").strip(),
        "area": str(location.get("area") or "").strip(),
    }
    return payload


def _query_url(path, params):
    query = urlencode(
        {key: value for key, value in params.items() if value not in (None, "")}
    )
    return f"{path}?{query}" if query else path


def _safe_page_offset(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _withdraw_service_request(request, client, pk):
    """Delete a request from the customer workspace without losing the record."""
    try:
        _, payload, _ = client.delete(replace_path("/services/requests/{pk}/", pk=pk))
    except APIError as exc:
        handle_api_error(request, exc)
        return False
    label = "Request #%s" % pk
    messages.success(
        request,
        f"{label} was removed from your requests. It is kept on record for your history.",
    )
    return True


def _request_action(request):
    """Resolve which button submitted a request form."""
    action = str(request.POST.get("action", "") or "").strip()
    if action:
        return action
    if request.POST.get("_delete"):
        return "delete"
    if request.POST.get("_edit"):
        return "edit"
    return ""


@api_guard
def service_requests(request):
    client = APIClient(request)
    if request.method == "POST" and _request_action(request) == "delete":
        request_id = str(request.POST.get("request_id", "") or "").strip()
        if not request_id.isdigit():
            messages.error(request, "That request could not be found.")
        else:
            _withdraw_service_request(request, client, request_id)
        return redirect("service-requests")
    if request.method == "POST":
        request.session[PENDING_REQUEST_FORM_SESSION_KEY] = (
            _safe_service_request_form_values(request.POST)
        )
        request.session.modified = True
        role = current_role(request)
        if role == "customer":
            action = {"kind": "service_request_form"}
            completion = _ensure_action_profile_complete(
                request,
                action,
                role=role,
                destination=_service_request_resume_url(),
            )
            if completion:
                return completion

    catalog = _request_form_catalog(request, client)
    if request.method == "POST":
        form = _service_request_form(request, catalog, data=request.POST)
        if form.is_valid():
            payload = _service_request_payload(
                form.cleaned_data, catalog.get("locations", [])
            )
            if payload is None:
                form.add_error(
                    "location_area_filter",
                    "That location is no longer in the marketplace catalogue. Please choose it again.",
                )
            else:
                try:
                    _, item, _ = client.post(
                        "/services/requests/",
                        payload,
                        idempotency_key=new_idempotency_key("service-request-create"),
                    )
                    _clear_pending_action(request, clear_form_values=True)
                    messages.success(request, "Your service request was created.")
                    return redirect("service-request-detail", pk=item["id"])
                except APIError as exc:
                    handle_api_error(request, exc)
    else:
        form_values = (
            request.session.get(PENDING_REQUEST_FORM_SESSION_KEY)
            if request.GET.get("resume") == "form"
            else None
        )
        if isinstance(form_values, dict):
            form = _service_request_form(
                request, catalog, initial=_safe_service_request_form_values(form_values)
            )
            messages.info(
                request,
                "Your request details were restored. Review the catalogue selections and submit when ready.",
            )
        else:
            form = _service_request_form(request, catalog)

    offset = _safe_page_offset(request.GET.get("offset"))
    try:
        _, payload, _ = client.get(
            "/services/requests/",
            params={"limit": SERVICE_REQUEST_PAGE_SIZE, "offset": offset},
        )
        items = extract_items(payload)
        total = _count_from_payload(payload)
    except APIError as exc:
        handle_api_error(request, exc)
        items, total = [], 0
    base_url = reverse("service-requests")
    return page(
        request,
        "customer/requests.html",
        requests=items,
        form=form,
        request_catalogue=_request_picker_catalogue(form),
        catalog_ready=(len(form.fields["service_name"].choices) > 1),
        request_total=total,
        next_url=(
            _query_url(base_url, {"offset": offset + SERVICE_REQUEST_PAGE_SIZE})
            if offset + SERVICE_REQUEST_PAGE_SIZE < total
            else ""
        ),
        previous_url=(
            _query_url(base_url, {"offset": max(0, offset - SERVICE_REQUEST_PAGE_SIZE)})
            if offset
            else ""
        ),
    )


def _load_service_request_responses(request, client, pk):
    filters = {
        key: str(request.GET.get(key, "") or "").strip() for key in RESPONDER_FILTERS
    }
    filters["ordering"] = filters.get("ordering") or "newest"
    offset = _safe_page_offset(request.GET.get("response_offset"))
    params = {key: value for key, value in filters.items() if value}
    params.update({"limit": SERVICE_REQUEST_PAGE_SIZE, "offset": offset})
    try:
        _, payload, _ = client.get(
            replace_path("/services/requests/{pk}/responses/", pk=pk),
            params=params,
        )
        items = extract_items(payload)
        total = _count_from_payload(payload)
    except APIError as exc:
        # The marketplace API does not expose responses for every request (yet);
        # a missing or forbidden collection simply means "nobody to show" and
        # must not replace the request page with a global error notice.
        if exc.status_code not in {403, 404}:
            handle_api_error(request, exc)
        items, total = [], 0
    path = reverse("service-request-responders", kwargs={"pk": pk})
    pager_base = {**filters}
    previous_url = (
        _query_url(
            path,
            {
                **pager_base,
                "response_offset": max(0, offset - SERVICE_REQUEST_PAGE_SIZE),
            },
        )
        if offset
        else ""
    )
    next_url = (
        _query_url(
            path, {**pager_base, "response_offset": offset + SERVICE_REQUEST_PAGE_SIZE}
        )
        if offset + SERVICE_REQUEST_PAGE_SIZE < total
        else ""
    )
    return items, total, filters, previous_url, next_url


@api_guard
def service_request_detail(request, pk, focus_responders=False):
    client = APIClient(request)
    path = replace_path("/services/requests/{pk}/", pk=pk)
    try:
        _, item, _ = client.get(path)
    except APIError as exc:
        handle_api_error(request, exc)
        return redirect("service-requests")

    if item.get("status", "OPEN") != "OPEN":
        messages.info(
            request,
            "This request is no longer active. Its reference is retained in the marketplace records.",
        )
        return redirect("service-requests")

    catalog = _request_form_catalog(request, client)
    location_id = _location_id_for_service(
        item.get("location"), catalog.get("locations", [])
    )
    item_location = item.get("location") or {}
    initial = {
        **item,
        "location_id": location_id,
        "location_country_filter": item_location.get("country") or "",
        "location_state_filter": item_location.get("state") or "",
        "location_area_filter": item_location.get("area") or "",
        "preferred_date": item.get("preferred_date") or "",
    }
    if request.method == "GET" and request.GET.get("resume") == "form":
        saved_form_values = request.session.get(PENDING_REQUEST_UPDATE_FORM_SESSION_KEY)
        if isinstance(saved_form_values, dict):
            initial.update(saved_form_values)
    action = request.POST.get("action", "update") if request.method == "POST" else ""
    if request.method == "POST" and _request_action(request) == "delete":
        _withdraw_service_request(request, client, pk)
        return redirect("service-requests")
    if request.method == "POST" and action == "start_conversation":
        artisan_id = str(request.POST.get("artisan_id") or "").strip()
        if not artisan_id:
            messages.error(
                request, "Choose an artisan response before starting a conversation."
            )
        else:
            completion_action = {
                "kind": "service_conversation",
                "artisan_id": artisan_id,
                "return_to": reverse("service-request-detail", kwargs={"pk": pk}),
            }
            completion = _ensure_action_profile_complete(
                request,
                completion_action,
                role="customer",
                destination=reverse("service-request-detail", kwargs={"pk": pk}),
            )
            if completion:
                return completion
            try:
                _, conversation, _ = client.post(
                    "/conversations/start/",
                    {"artisan_id": artisan_id, "service_request_id": int(pk)},
                    idempotency_key=new_idempotency_key("request-conversation"),
                )
                messages.success(
                    request, "Your conversation with the selected artisan is ready."
                )
                return redirect(
                    "conversation-detail", conversation_id=conversation["id"]
                )
            except (APIError, ValueError) as exc:
                if isinstance(exc, APIError):
                    handle_api_error(request, exc)
                else:
                    messages.error(
                        request,
                        "The selected artisan response could not be verified. Please try again.",
                    )
        form = _service_request_form(request, catalog, initial=initial)
    elif request.method == "POST":
        role = current_role(request)
        if role == "customer":
            request.session[PENDING_REQUEST_UPDATE_FORM_SESSION_KEY] = (
                _safe_service_request_form_values(request.POST)
            )
            request.session.modified = True
            completion = _ensure_action_profile_complete(
                request,
                {"kind": "service_request_update", "service_request_id": str(pk)},
                role=role,
                destination=f"{reverse('service-request-detail', kwargs={'pk': pk})}?resume=form",
            )
            if completion:
                return completion
        form = _service_request_form(
            request, catalog, data=request.POST, initial=initial
        )
        if form.is_valid():
            payload = _service_request_payload(
                form.cleaned_data, catalog.get("locations", [])
            )
            if payload is None:
                form.add_error(
                    "location_area_filter",
                    "That location is no longer in the marketplace catalogue. Please choose it again.",
                )
            else:
                try:
                    _, item, _ = client.patch(path, payload)
                    request.session.pop(PENDING_REQUEST_UPDATE_FORM_SESSION_KEY, None)
                    messages.success(request, "Service request updated.")
                    return redirect("service-request-detail", pk=pk)
                except APIError as exc:
                    handle_api_error(request, exc)
    else:
        form = _service_request_form(request, catalog, initial=initial)

    (
        responders,
        responder_total,
        responder_filters,
        responder_previous_url,
        responder_next_url,
    ) = _load_service_request_responses(request, client, pk)
    return page(
        request,
        "customer/request_detail.html",
        item=item,
        form=form,
        request_catalogue=_request_picker_catalogue(form),
        responders=responders,
        responder_total=responder_total,
        responder_filters=responder_filters,
        responder_previous_url=responder_previous_url,
        responder_next_url=responder_next_url,
        focus_responders=focus_responders,
    )


def service_request_responders(request, pk):
    return service_request_detail(request, pk, focus_responders=True)


@api_guard
def artisan_requests(request):
    client = APIClient(request)
    filters = {
        key: str(request.GET.get(key, "") or "").strip()
        for key in ARTISAN_REQUEST_FILTERS
    }
    if request.method == "GET" and not request.GET:
        filters["location_country"] = "Nigeria"
    offset = _safe_page_offset(request.GET.get("offset"))
    list_path = "/services/artisan/requests/"

    if request.method == "POST":
        action = str(request.POST.get("action") or "").strip()
        raw_id = str(request.POST.get("request_id") or "").strip()
        if not raw_id.isdigit():
            messages.error(request, "The service request reference is invalid.")
        elif action == "dismiss":
            try:
                client.post(f"/services/artisan/requests/{raw_id}/dismiss/", {})
                messages.success(
                    request,
                    "Request hidden from your matches only. Other eligible artisans can still respond.",
                )
            except APIError as exc:
                handle_api_error(request, exc)
        elif action == "respond":
            try:
                _, room, _ = client.post(
                    "/conversations/start/",
                    {"request_id": int(raw_id)},
                    idempotency_key=new_idempotency_key("request-room"),
                )
                return redirect("conversation-detail", conversation_id=room["id"])
            except APIError as exc:
                handle_api_error(request, exc)
        elif action == "withdraw":
            try:
                client.delete(f"/services/requests/{raw_id}/responses/")
                messages.success(request, "Your response has been withdrawn.")
            except APIError as exc:
                handle_api_error(request, exc)
        else:
            messages.error(request, "That request action is not supported.")
        return_to = str(request.POST.get("return_to") or "")
        if return_to and url_has_allowed_host_and_scheme(
            return_to,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        ):
            return redirect(return_to)
        return redirect("artisan-requests")

    params = {key: value for key, value in filters.items() if value}
    params.update({"limit": SERVICE_REQUEST_PAGE_SIZE, "offset": offset})
    try:
        _, payload, _ = client.get(list_path, params=params)
        items = extract_items(payload)
        total = _count_from_payload(payload)
    except APIError as exc:
        handle_api_error(request, exc)
        items, total = [], 0
    for item in items:
        age = item.get("age_seconds")
        if age is None and item.get("created_at"):
            try:
                posted = datetime.fromisoformat(
                    str(item["created_at"]).replace("Z", "+00:00")
                )
                if timezone.is_naive(posted):
                    posted = timezone.make_aware(posted)
                age = (timezone.now() - posted).total_seconds()
            except (TypeError, ValueError):
                age = None
        try:
            age = max(0, int(age))
        except (ValueError, TypeError, OverflowError):
            age = None
        item["age_seconds"] = age if age is not None else ""
        item["request_age_label"] = _matched_request_age_label(age)
    base_url = reverse("artisan-requests")
    filters["filters_applied"] = "1"
    previous_url = (
        _query_url(
            base_url, {**filters, "offset": max(0, offset - SERVICE_REQUEST_PAGE_SIZE)}
        )
        if offset
        else ""
    )
    next_url = (
        _query_url(base_url, {**filters, "offset": offset + SERVICE_REQUEST_PAGE_SIZE})
        if offset + SERVICE_REQUEST_PAGE_SIZE < total
        else ""
    )
    return page(
        request,
        "artisan/requests.html",
        filter_catalogue=_matched_request_picker_catalogue(request, client),
        role="artisan",
        requests=items,
        request_total=total,
        filters=filters,
        has_active_filters=any(filters.get(key) for key in ARTISAN_REQUEST_FILTERS),
        previous_url=previous_url,
        next_url=next_url,
        return_to=request.get_full_path(),
        page_js_path="",
    )


def institution_catalogue(request):
    """Search frontend-owned JSON; location values are optional narrowing filters."""
    if request.method != "GET":
        return JsonResponse({"detail": "Use GET."}, status=405)
    from .profile_catalogue import catalogue_page

    return JsonResponse(catalogue_page(request.GET))


def _matched_request_age_label(seconds):
    if seconds is None:
        return "Time unavailable"
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, seconds = divmod(rest, 60)
    return (
        (f"{days}d " if days else "")
        + (f"{hours}h " if days or hours else "")
        + (f"{minutes}m " if days or hours or minutes else "")
        + f"{seconds}s"
    )


def _matched_request_picker_catalogue(request, client):
    from .geography import world_geography

    result = {"geography": world_geography()}
    try:
        _, payload, _ = client.get("/services/service_cat/")
        result["services"] = extract_items(payload)
    except APIError as exc:
        handle_api_error(request, exc)
        result["services"] = []
    return result


# ---------------- Artisan services ----------------


def _catalog_endpoint_items(request, client, endpoint):
    items = []
    offset = 0
    limit = 100
    for _page_number in range(100):
        try:
            _, payload, _ = client.get(
                endpoint, params={"limit": limit, "offset": offset}
            )
        except APIError as exc:
            handle_api_error(request, exc)
            return items
        page_items = extract_items(payload)
        items.extend(page_items)
        if not isinstance(payload, dict) or not page_items:
            break
        total = _count_from_payload(payload)
        if offset + len(page_items) >= total or len(page_items) < limit:
            break
        offset += len(page_items)
    return items


def _artisan_service_catalog(request, client, *, include_institutions=False):
    """Catalogue data shared by the service and request forms.

    Institutions are only needed by the "My services" picker, so they are fetched
    on request; other callers keep their existing API footprint.
    """
    endpoints = [
        ("categories", "/services/categories/"),
        ("services", "/services/service_cat/"),
        ("locations", "/services/locations/"),
    ]
    if include_institutions:
        endpoints.append(("institutions", "/services/institutions/"))
    catalog = {"institutions": []}
    for key, endpoint in endpoints:
        catalog[key] = _catalog_endpoint_items(request, client, endpoint)
    return catalog


ANYWHERE_LOCATION_VALUE = "__anywhere__"
MAX_ARTISAN_SERVICES = 3


GEOGRAPHIC_COVERAGE_VALUE = "__geo__"


def _location_id_for_service(item_location, locations):
    """Map a saved location onto the backend marketplace catalogue (request forms)."""
    if item_location is None:
        return ANYWHERE_LOCATION_VALUE
    if not isinstance(item_location, dict):
        return str(item_location or "")
    target = tuple(
        str(item_location.get(key, "") or "").strip().casefold()
        for key in ("country", "state", "area")
    )
    if not any(target):
        return ANYWHERE_LOCATION_VALUE
    for location in locations:
        if not isinstance(location, dict) or not location.get("id"):
            continue
        candidate = tuple(
            str(location.get(key, "") or "").strip().casefold()
            for key in ("country", "state", "area")
        )
        if candidate == target:
            return str(location["id"])
    return ""


def _artisan_service_form_initial(item):
    initial = {
        "category_name": item.get("category_name", ""),
        "service_name": item.get("service_name", ""),
        "starting_price": item.get("starting_price", ""),
    }
    location = item.get("location")
    if isinstance(location, dict) and any(
        str(location.get(key) or "").strip() for key in ("country", "state", "area")
    ):
        # Existing localized offering: prefill the geographic pickers from the
        # saved location names.
        initial["location_id"] = GEOGRAPHIC_COVERAGE_VALUE
        for key in ("country", "state", "area"):
            initial[f"location_{key}_filter"] = str(location.get(key) or "")
    else:
        initial["location_id"] = ANYWHERE_LOCATION_VALUE
    return initial


def _artisan_service_payload(cleaned_data):
    resolved_location = cleaned_data.get("resolved_location")
    payload = {
        # The API resolves service_name and derives category_name from that service.
        # A null location is the existing general-offering/Anywhere contract; a
        # catalogue {country, state, area} triple is resolved (and registered on
        # first use) by the API.
        "service_name": cleaned_data["service_name"],
        "location": resolved_location,
    }
    starting_price = cleaned_data.get("starting_price")
    if starting_price is not None:
        # Decimal is not JSON serialisable; the API accepts a decimal string.
        payload["starting_price"] = f"{starting_price:.2f}"
    return payload


@api_guard
def artisan_services(request):
    client = APIClient(request)
    catalog = _artisan_service_catalog(request, client)
    try:
        _, payload, _ = client.get("/services/get_artisan_services/")
        items = extract_items(payload)
    except APIError as exc:
        handle_api_error(request, exc)
        items = []

    service_count = len(items)
    at_limit = service_count >= MAX_ARTISAN_SERVICES

    if request.method == "POST" and request.POST.get("_delete") == "1":
        offering_id = request.POST.get("offering_id", "").strip()
        if not offering_id.isdigit():
            messages.error(
                request,
                "The selected service could not be identified. Refresh the page and try again.",
            )
        else:
            path = replace_path("/services/get_artisan_services/{pk}/", pk=offering_id)
            try:
                client.delete(path)
                messages.success(request, "Service removed from your artisan profile.")
            except APIError as exc:
                handle_api_error(request, exc)
        return redirect("artisan-services")

    editing_id = (
        request.POST.get("offering_id", "")
        if request.method == "POST"
        else request.GET.get("edit", "")
    ).strip()
    editing_item = None
    if editing_id and not editing_id.isdigit():
        messages.error(
            request,
            "The selected service could not be identified. Choose it from your service list.",
        )
        if request.method == "POST":
            return redirect("artisan-services")
        editing_id = ""

    if editing_id:
        path = replace_path("/services/get_artisan_services/{pk}/", pk=editing_id)
        try:
            _, editing_item, _ = client.get(path)
        except APIError as exc:
            handle_api_error(request, exc)
            if request.method == "POST":
                return redirect("artisan-services")
            editing_id = ""

    initial = (
        _artisan_service_form_initial(editing_item) if editing_item else None
    )
    form = ArtisanServiceForm(
        request.POST or None,
        initial=initial,
        categories=catalog["categories"],
        services=catalog["services"],
    )

    if request.method == "POST" and form.is_valid():
        try:
            service_payload = _artisan_service_payload(form.cleaned_data)
            if editing_id:
                path = replace_path(
                    "/services/get_artisan_services/{pk}/", pk=editing_id
                )
                client.patch(path, service_payload)
                messages.success(request, "Service details updated.")
            else:
                client.post("/services/get_artisan_services/", service_payload)
                messages.success(request, "Service added to your artisan profile.")
            return redirect("artisan-services")
        except APIError as exc:
            handle_api_error(request, exc)
        except ValueError as exc:
            form.add_error("location_id", str(exc))
    elif request.method == "POST":
        messages.error(
            request, "Please correct the highlighted fields before saving this service."
        )

    from .geography import world_geography
    from .profile_catalogue import all_institutions

    return page(
        request,
        "artisan/services.html",
        items=items,
        form=form,
        editing_item=editing_item,
        service_catalog=catalog["services"],
        # Same data source as the public discovery filters: the worldwide
        # geographic catalogue and the institution catalogue (alphabetical,
        # Nigeria first when no geography is chosen).
        geography_records=world_geography(),
        institutions=[
            {
                "id": str(row.get("source_id") or ""),
                "name": str(row.get("name") or ""),
                "abbreviation": next(
                    iter(row.get("abbreviations") or []), ""
                ),
                "country": str(row.get("country") or ""),
                "state": str(row.get("state") or ""),
                "area": str(row.get("area") or ""),
            }
            for row in all_institutions()
            if isinstance(row, dict) and row.get("source_id")
        ],
        anywhere_value=ANYWHERE_LOCATION_VALUE,
        geographic_value=GEOGRAPHIC_COVERAGE_VALUE,
        service_count=service_count,
        max_services=MAX_ARTISAN_SERVICES,
        at_limit=at_limit,
    )


@api_guard
def artisan_service_detail(request, pk):
    # Retain the bookmarked route, but keep service management on one page.
    return redirect(f"{reverse('artisan-services')}?edit={pk}#service-offer-form")


# ---------------- Admin artisan activation ----------------


@api_guard
def artisan_activate(request, pk):
    if request.method != "POST":
        return page(request, "artisan/activation.html", pk=pk)
    try:
        _, payload, _ = APIClient(request).post(
            replace_path("/services/artisan/profile/{pk}/activate/", pk=pk),
            {"activate": request.POST.get("activate") == "1"},
        )
        messages.success(
            request, payload_message(payload) or "Artisan activation updated."
        )
    except APIError as exc:
        handle_api_error(request, exc)
    # Keep manager navigation within the restricted staff workspace.
    return redirect("home")


# ---------------- Bookings ----------------


@api_guard
def bookings(request):
    client = APIClient(request)
    if request.method == "POST":
        messages.info(
            request,
            "Bookings are created after scope, price, and schedule are agreed in a service conversation.",
        )
        return redirect("conversation-inbox")
    try:
        _, payload, _ = client.get("/booking/")
        items = extract_items(payload)
    except APIError as exc:
        handle_api_error(request, exc)
        items = []
    for item in items:
        item["actions"] = _booking_actions(item)
        item["can_cancel"] = any(
            action.get("code") == "CANCEL_BOOKING" for action in item["actions"]
        )
    return page(
        request, "bookings/list.html", bookings=items, role=current_role(request)
    )


BOOKING_ACTION_ROUTES = {
    "ACCEPT_BOOKING": "booking-accept",
    "REJECT_BOOKING": "booking-reject",
    "CANCEL_BOOKING": "booking-cancel",
    "START_WORK": "booking-start",
    "REQUEST_COMPLETION": "booking-complete",
    "CONFIRM_COMPLETION": "booking-confirm",
    "DISPUTE_BOOKING": "booking-dispute",
    "VIEW_HISTORY": "booking-history",
}


def _booking_actions(booking):
    """Turn the server's available_actions into links the template can render.

    The list comes from the API, which is also what enforces it, so a rendered
    button and an allowed action can never disagree.
    """
    actions = []
    for item in booking.get("available_actions") or []:
        if not isinstance(item, dict):
            continue
        entry = dict(item)
        code = entry.get("code")
        route = BOOKING_ACTION_ROUTES.get(code)
        if route:
            entry["url"] = reverse(route, kwargs={"pk": booking.get("id")})
        elif code == "PAY_BOOKING":
            entry["url"] = reverse(
                "payment-initialize", kwargs={"booking_id": booking.get("id")}
            )
        elif code == "OPEN_CONVERSATION" and booking.get("conversation_id"):
            entry["url"] = reverse(
                "conversation-detail",
                kwargs={"conversation_id": booking["conversation_id"]},
            )
        else:
            entry["url"] = ""
        actions.append(entry)
    return actions


def _booking_service_summary(client, booking):
    """Service and schedule context from the linked request, when available."""
    request_id = booking.get("request_id")
    if not request_id:
        return {}
    try:
        _, request_payload, _ = client.get(
            replace_path("/services/requests/{pk}/", pk=request_id)
        )
    except APIError:
        return {}
    if not isinstance(request_payload, dict):
        return {}
    category = request_payload.get("category")
    location = request_payload.get("location")
    return {
        "service": (
            request_payload.get("service_name")
            or (category.get("name") if isinstance(category, dict) else category)
            or ""
        ),
        "location": (
            location.get("area")
            if isinstance(location, dict)
            else (request_payload.get("location_label") or "")
        ),
        "description": request_payload.get("description") or "",
        "preferred_date": request_payload.get("preferred_date") or "",
    }


@api_guard
def booking_detail(request, pk):
    client = APIClient(request)
    try:
        _, item, _ = client.get(replace_path("/booking/{pk}/", pk=pk))
    except APIError as exc:
        handle_api_error(request, exc)
        return redirect("bookings")
    return page(
        request,
        "bookings/detail.html",
        booking=item,
        booking_actions=_booking_actions(item),
        service_summary=_booking_service_summary(client, item),
        role=item.get("viewer_role") or current_role(request),
        nav_role=current_role(request),
    )


@api_guard
def customer_bookings(request):
    """The customer's own booking dashboard: list, price, schedule, next action."""
    client = APIClient(request)
    try:
        _, payload, _ = client.get("/booking/", params={"limit": "50"})
        items = extract_items(payload)
    except APIError as exc:
        handle_api_error(request, exc)
        items = []
    # The API scopes the list to the signed-in user. `viewer_role` only separates
    # dual-role accounts; an older payload without it must not empty the page.
    if any(item.get("viewer_role") for item in items):
        mine = [
            item
            for item in items
            if item.get("viewer_role") in (None, "customer")
        ]
    else:
        mine = items
    for item in mine:
        item["actions"] = _booking_actions(item)
        item["primary_action"] = next(
            (
                action
                for action in item["actions"]
                if action.get("variant") in {"primary"}
                and action.get("code")
                in {"PAY_BOOKING", "CONFIRM_COMPLETION"}
            ),
            None,
        )
        item["can_cancel"] = any(
            action.get("code") == "CANCEL_BOOKING" for action in item["actions"]
        )
    return page(
        request,
        "customer/bookings.html",
        bookings=mine,
        role="customer",
        nav_role=current_role(request),
    )


def _booking_action(
    request,
    pk,
    action,
    form_cls=None,
    redirect_name="booking-detail",
    template="bookings/action.html",
    title="Booking action",
    cancel_notice="",
    confirm_message="",
    **url_kwargs,
):
    form = form_cls(request.POST or None) if form_cls else None
    if request.method == "POST" and (form is None or form.is_valid()):
        data = cleaned_payload(form.cleaned_data) if form else None
        try:
            _, payload, _ = APIClient(request).post(
                replace_path(action, pk=pk),
                data,
                idempotency_key=new_idempotency_key("booking-action"),
            )
            notices = []
            payment_info = payload.get("payment") if isinstance(payload, dict) else None
            if isinstance(payment_info, dict) and payment_info.get("message"):
                notices.append(payment_info["message"])
            messages.success(
                request,
                payload_message(payload) or "Booking updated.",
            )
            for notice in notices:
                messages.info(request, notice)
            return redirect(
                redirect_name,
                **({"pk": pk} if redirect_name == "booking-detail" else url_kwargs),
            )
        except APIError as exc:
            handle_api_error(request, exc)
    return page(
        request,
        template,
        form=form,
        title=title,
        pk=pk,
        cancel_notice=cancel_notice,
        confirm_message=confirm_message,
    )


@api_guard
def booking_accept(request, pk):
    return _booking_action(request, pk, "/booking/{pk}/accept/", title="Accept booking")


@api_guard
def booking_reject(request, pk):
    return _booking_action(
        request, pk, "/booking/{pk}/reject/", ReasonForm, title="Reject booking"
    )


@api_guard
def booking_cancel(request, pk):
    """Cancel an accepted-but-unpaid booking.

    The confirmation states plainly that no money was taken, because an unpaid
    payment link is voided rather than refunded.
    """
    client = APIClient(request)
    payment_line = "No payment has been taken from your account."
    try:
        _, booking, _ = client.get(replace_path("/booking/{pk}/", pk=pk))
    except APIError as exc:
        handle_api_error(request, exc)
        return redirect("bookings")
    actions = {item.get("code") for item in booking.get("available_actions") or []}
    if "CANCEL_BOOKING" not in actions:
        messages.error(
            request,
            "This booking cannot be cancelled from here. If money is held in escrow, use the payment or dispute process.",
        )
        return redirect("booking-detail", pk=pk)
    return _booking_action(
        request,
        pk,
        "/booking/{pk}/cancel/",
        CancelForm,
        title="Cancel booking",
        cancel_notice=payment_line,
        confirm_message=(
            "Cancel this booking? No payment was taken from your account. "
            "Any payment link for it stops working immediately."
        ),
    )


@api_guard
def booking_start(request, pk):
    return _booking_action(request, pk, "/booking/{pk}/start/", title="Start booking")


@api_guard
def booking_complete(request, pk):
    return _booking_action(
        request, pk, "/booking/{pk}/complete/", NoteForm, title="Mark booking complete"
    )


@api_guard
def booking_confirm_completion(request, pk):
    return _booking_action(
        request, pk, "/booking/{pk}/confirm-completion/", title="Confirm completion"
    )


def _find_booking_dispute_case(client, booking_id):
    _, payload, _ = client.get(
        "/support/cases/",
        params={"search": str(booking_id), "limit": 100, "offset": 0},
    )
    for case in extract_items(payload):
        if (
            isinstance(case, dict)
            and case.get("case_type") == "DISPUTE"
            and str(case.get("booking") or "") == str(booking_id)
        ):
            return case
    return None


@api_guard
def booking_dispute(request, pk):
    client = APIClient(request)
    try:
        _, booking, _ = client.get(replace_path("/booking/{pk}/", pk=pk))
    except APIError as exc:
        handle_api_error(request, exc)
        return redirect("bookings")

    form = DisputeMessageForm(request.POST if request.method == "POST" else None)
    dispute_idempotency_key = request.POST.get(
        "dispute_idempotency_key"
    ) or new_idempotency_key("booking-dispute")
    support_message_key = request.POST.get(
        "support_message_key"
    ) or new_idempotency_key("dispute-support-message")
    if request.method == "POST" and form.is_valid():
        message_text = form.cleaned_data["message"]
        dispute_error = None
        try:
            client.post(
                replace_path("/booking/{pk}/dispute/", pk=pk),
                {"reason": message_text},
                idempotency_key=dispute_idempotency_key,
            )
        except APIError as exc:
            # A previous submit may have created the case before the browser lost its response.
            dispute_error = exc

        search_error = None
        try:
            case = _find_booking_dispute_case(client, pk)
        except APIError as exc:
            search_error = exc
            case = None

        if case:
            try:
                client.post(
                    replace_path("/support/cases/{pk}/messages/", pk=case["id"]),
                    {"message": message_text},
                    idempotency_key=support_message_key,
                )
            except APIError as exc:
                handle_api_error(request, exc)
                messages.warning(
                    request,
                    "Your dispute case exists, but the case message could not be confirmed. Use the support case composer to send your message.",
                )
                response = redirect("support-case-detail", pk=case["id"])
                response["Location"] = f"{response['Location']}#message-composer"
                return response
            messages.success(
                request,
                "Your message was sent to customer support and attached to the dispute case.",
            )
            response = redirect("support-case-detail", pk=case["id"])
            response["Location"] = f"{response['Location']}#latest-message"
            return response

        if dispute_error:
            handle_api_error(request, dispute_error)
        elif search_error:
            messages.success(
                request, "Your dispute report was submitted to customer support."
            )
            messages.warning(
                request,
                "The case thread could not be loaded just now. Open Support cases to continue the conversation.",
            )
            return redirect("support-cases")
        else:
            messages.success(
                request, "Your dispute report was submitted to customer support."
            )
            messages.warning(
                request,
                "The matching support case was not returned. You can continue from Support cases.",
            )
            return redirect("support-cases")

    return page(
        request,
        "bookings/dispute.html",
        booking=booking,
        form=form,
        dispute_idempotency_key=dispute_idempotency_key,
        support_message_key=support_message_key,
    )


@api_guard
def booking_history(request, pk):
    try:
        _, payload, _ = APIClient(request).get(
            replace_path("/booking/{pk}/history/", pk=pk)
        )
    except APIError as exc:
        handle_api_error(request, exc)
        payload = []
    return page(request, "bookings/history.html", history=extract_items(payload), pk=pk)


@api_guard
def booking_activity(request, pk):
    """What is actually going on with one booking, read from the records.

    The chat can only show the next action; when the provider is slow or a link
    has gone stale there is nothing honest for it to say. This page answers the
    question directly: the booking state, the escrow payment, whether the
    checkout can be used right now, and the trail of what happened.
    """
    client = APIClient(request)
    try:
        _, booking, _ = client.get(replace_path("/booking/{pk}/", pk=pk))
    except APIError as exc:
        handle_api_error(request, exc)
        return redirect("bookings")
    if not isinstance(booking, dict):
        booking = {}

    history = []
    try:
        _, payload, _ = client.get(replace_path("/booking/{pk}/history/", pk=pk))
        history = extract_items(payload)
    except APIError:
        history = []

    conversation = {}
    payment = {}
    events = []
    conversation_id = str(booking.get("conversation_id") or "")
    if conversation_id:
        try:
            _, conversation, _ = client.get(f"/conversations/{conversation_id}/")
        except APIError:
            conversation = {}
        summary = (conversation or {}).get("payment") if isinstance(conversation, dict) else {}
        if isinstance(summary, dict) and summary.get("id"):
            payment = _payment_snapshot(client, summary["id"]) or dict(summary)
            for field in ("total_amount", "status"):
                payment.setdefault(field, summary.get(field))
        try:
            _, messages, _ = client.get(
                f"/conversations/{conversation_id}/messages/", params={"limit": "60"}
            )
            events = [
                item
                for item in extract_items(messages)
                if isinstance(item, dict) and item.get("message_type") == "SYSTEM"
            ]
        except APIError:
            events = []

    checkout = payment.get("checkout") if isinstance(payment.get("checkout"), dict) else {}
    checkout_url = _live_checkout_url(payment)

    trail = []
    for item in history:
        if not isinstance(item, dict):
            continue
        trail.append(
            {
                "at": str(item.get("created_at") or ""),
                "title": str(item.get("status") or "").replace("_", " ").title() or "Status changed",
                "detail": str(item.get("notes") or ""),
                "actor": str(item.get("changed_by") or ""),
                "kind": "booking",
            }
        )
    for item in events:
        body = str(item.get("body") or "").split("Next steps:")[0].strip()
        if not body:
            continue
        trail.append(
            {
                "at": str(item.get("created_at") or ""),
                "title": body,
                "detail": "",
                "actor": "",
                "kind": "conversation",
            }
        )
    trail.sort(key=lambda entry: entry["at"])

    booking_status = str(booking.get("status") or "").upper()
    payment_status = str(payment.get("status") or "").upper()
    checkout_state = str(checkout.get("state") or "").upper()
    if payment_status == "HELD":
        next_step = "The money is held in escrow. The artisan can start the work, and payout is released only after you confirm completion."
    elif payment_status == "PAYMENT_PENDING" and checkout_url:
        next_step = "Waiting for you to pay. The secure checkout below accepts card or bank transfer; nothing is held until the provider confirms it."
    elif payment_status == "PAYMENT_PENDING":
        next_step = "A payment link is being prepared with the provider. This page asks for a fresh one every few seconds; nothing has been charged."
    elif payment_status == "FAILED":
        reason = str(payment.get("failure_reason") or "").strip()
        next_step = (
            "The provider refused to create a checkout"
            + (f": {reason}" if reason else ".")
            + " Nothing has been charged. Ask for a new link to try again."
        )
    elif booking_status in {"CANCELLED", "REJECTED"}:
        next_step = "This booking was closed before payment. No money moved."
    elif booking_status == "ACCEPTED":
        next_step = "The agreement is accepted. The escrow payment is the next step."
    else:
        next_step = "Follow the conversation for the next step; every action here is recorded."

    return page(
        request,
        "bookings/activity.html",
        booking=booking,
        booking_history=trail,
        payment=payment,
        checkout=checkout,
        checkout_url=checkout_url,
        conversation_id=conversation_id,
        next_step=next_step,
        role=booking.get("viewer_role") or current_role(request),
        nav_role=current_role(request),
    )


# ---------------- Payments ----------------

PAYMENT_SESSION_CONVERSATION_KEY = "payment_return_conversation"


def _safe_checkout_url(value):
    """Only a real Flutterwave https checkout may be used for a redirect."""
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "https" and host.endswith(".flutterwave.com"):
        return text
    return ""


def _live_checkout_url(payment):
    """A provider link the payer may open right now, or nothing.

    The API reports the link's state (`LIVE`, `EXPIRED`, `OLD_METHODS`,
    `NOT_MINTED`). A link minted before checkout was limited to card and bank
    transfer, or one past the provider countdown, must not be offered: the payer
    would land on a page with methods we no longer allow. Callers that get
    nothing back ask for a fresh mint instead. When the API is an older build and
    reports no state at all, the URL is passed through so a mixed deployment can
    still take payment.
    """
    if not isinstance(payment, dict):
        return ""
    checkout = payment.get("checkout")
    if isinstance(checkout, dict) and checkout:
        if str(checkout.get("state") or "").upper() != "LIVE":
            return ""
    return _safe_checkout_url(payment.get("authorization_url"))


def _payment_snapshot(client, payment_id):
    try:
        _, payment, _ = client.get(
            replace_path("/payments/{payment_id}/", payment_id=payment_id)
        )
    except APIError:
        return {}
    return payment if isinstance(payment, dict) else {}


def _checkout_url_for(client, payment_id, payload=None, tries=13, pause=0.7):
    """Return the provider checkout URL as soon as the queued init has it.

    Initialization is asynchronous on the server, so the first response usually
    carries only `payment_id`. We poll our own payment record briefly instead of
    sending the payer to an intermediate page.
    """
    if isinstance(payload, dict):
        url = _live_checkout_url(payload)
        if url:
            return url
    for attempt in range(max(1, tries)):
        payment = _payment_snapshot(client, payment_id)
        url = _live_checkout_url(payment)
        if url:
            return url
        if attempt < tries - 1:
            time.sleep(pause)
    return ""


def _remember_payment_return(request, conversation_id):
    if conversation_id:
        request.session[PAYMENT_SESSION_CONVERSATION_KEY] = str(conversation_id)


def _conversation_for_payment(client, payment, request=None):
    """The chat a payment belongs to, so the payer never lands on a dead page."""
    conversation_id = str(payment.get("conversation_id") or "").strip()
    if not conversation_id and request is not None:
        conversation_id = str(
            request.session.get(PAYMENT_SESSION_CONVERSATION_KEY) or ""
        ).strip()
    if not conversation_id:
        booking_id = str(payment.get("booking") or "").strip()
        if booking_id:
            try:
                _, booking, _ = client.get(
                    replace_path("/booking/{pk}/", pk=booking_id)
                )
            except APIError:
                booking = {}
            if isinstance(booking, dict):
                conversation_id = str(booking.get("conversation_id") or "").strip()
    return conversation_id




def payment_initialize(request, booking_id):
    if request.method != "POST":
        return page(request, "payments/initialize.html", booking_id=booking_id)
    client = APIClient(request)
    try:
        _, payload, _ = client.post(
            replace_path(
                "/payments/booking/{booking_id}/initialize/", booking_id=booking_id
            ),
            idempotency_key=new_idempotency_key("payment-init"),
        )
    except APIError as exc:
        handle_api_error(request, exc)
        return redirect("booking-detail", pk=booking_id)
    payment_id = (
        str(payload.get("payment_id") or "") if isinstance(payload, dict) else ""
    )
    checkout_url = (
        _checkout_url_for(client, payment_id, payload) if payment_id else ""
    )
    payment = _payment_snapshot(client, payment_id) if payment_id else {}
    conversation_id = _conversation_for_payment(client, payment, request)
    _remember_payment_return(request, conversation_id)
    if checkout_url:
        return redirect(checkout_url)
    messages.info(
        request,
        "The secure payment link is still being prepared. Nothing has been charged. "
        "This chat opens it by itself the moment the provider returns it — no need to tap again.",
    )
    if conversation_id:
        return redirect("conversation-detail", conversation_id=conversation_id)
    return redirect("booking-detail", pk=booking_id)


@require_GET
def payment_status(request, payment_id):
    """Tiny JSON status feed the chat polls while a payment is in flight."""
    client = APIClient(request)
    payment = _payment_snapshot(client, payment_id)
    if not payment:
        return JsonResponse({"detail": "Payment not found."}, status=404)
    payload = {
        "status": str(payment.get("status") or ""),
        "authorization_url": _live_checkout_url(payment),
        "conversation_id": _conversation_for_payment(client, payment, request),
    }
    return JsonResponse(payload)


@require_GET
def payment_provider_return(request):
    """Where Flutterwave lands when only the web host has a public tunnel.

    The provider appends `tx_ref` and `transaction_id` but knows nothing about
    our payment ids. This route hands both to the API (over localhost, server to
    server), takes back the payment it matched, and forwards the browser into the
    normal return page. Without it a single-tunnel setup would 404 after paying.
    """
    tx_ref = str(request.GET.get("tx_ref") or "").strip()
    transaction_id = str(request.GET.get("transaction_id") or "").strip()
    if not tx_ref or not transaction_id:
        messages.info(
            request,
            "The payment provider did not say which payment it was. Nothing has been "
            "charged twice — open the booking to see its exact state.",
        )
        return redirect("bookings")

    client = APIClient(request)
    try:
        _, payload, _ = client.get(
            "/payments/flutterwave/callback/",
            params={"tx_ref": tx_ref, "transaction_id": transaction_id},
        )
    except APIError as exc:
        handle_api_error(request, exc)
        return redirect("bookings")

    payment_id = str((payload or {}).get("payment_id") or "") if isinstance(payload, dict) else ""
    if not payment_id:
        messages.info(
            request,
            "That callback does not belong to a booking payment. If money left your "
            "account, support can trace it with the provider reference — nothing was "
            "charged twice.",
        )
        return redirect("bookings")
    payment = _payment_snapshot(client, payment_id)
    _remember_payment_return(request, _conversation_for_payment(client, payment, request))
    return redirect("payment-return", payment_id=payment_id)


@csrf_exempt
@require_POST
def payment_webhook_proxy(request):
    """Forward Flutterwave's webhook to the API when only this host is public.

    The API validates `flutterwave-signature` / `verif-hash` itself, so this route
    is a pipe, not a gate: it only relays the raw body and those two headers to
    the fixed internal address, and it caps the size.
    """
    body = request.body or b""
    if len(body) > 1_000_000:
        return HttpResponse(status=413)

    headers = {"Content-Type": "application/json"}
    for name in ("flutterwave-signature", "verif-hash"):
        value = request.headers.get(name)
        if value:
            headers[name] = value

    import requests as http

    target = f"{settings.ARTISAN_API_ROOT_URL.rstrip('/')}/api/v1/payments/flutterwave/webhook/"
    try:
        upstream = http.post(target, data=body, headers=headers, timeout=15)
    except Exception:
        # The provider retries failed webhooks; never acknowledge a delivery we
        # could not hand on.
        return HttpResponse(status=502)
    return HttpResponse(status=upstream.status_code)


@require_GET
def payment_return(request, payment_id):
    """Where the provider drops the payer: back into the chat, never a bare page."""
    client = APIClient(request)
    payment = _payment_snapshot(client, payment_id)
    if not payment:
        messages.info(request, "That payment could not be found.")
        return redirect("bookings")
    # If the provider sent the payer straight here, forward the identifiers so the
    # API still queues its own verification (the same job the callback does).
    tx_ref = str(request.GET.get("tx_ref") or "").strip()
    transaction_id = str(request.GET.get("transaction_id") or "").strip()
    if tx_ref and transaction_id:
        try:
            client.get(
                "/payments/flutterwave/callback/",
                params={"tx_ref": tx_ref, "transaction_id": transaction_id},
            )
        except APIError:
            pass
        payment = _payment_snapshot(client, payment_id) or payment

    status = str(payment.get("status") or "").upper()
    conversation_id = _conversation_for_payment(client, payment, request)
    if status == "HELD":
        messages.success(
            request,
            "Payment secured in escrow. The artisan can see it in this chat and start the work.",
        )
    elif status == "PAYMENT_PENDING":
        messages.info(
            request,
            "We are confirming this payment with the provider. This chat updates as soon as it clears.",
        )
    elif status == "CANCELLED":
        messages.info(request, "That payment was cancelled with the booking. No money was taken.")
    elif status == "FAILED":
        messages.error(request, "The provider did not confirm this payment. You can try again here.")
    else:
        messages.info(request, f"Payment status: {status.title() or 'unknown'}.")
    if conversation_id:
        return redirect("conversation-detail", conversation_id=conversation_id)
    return redirect("payment-detail", payment_id=payment_id)


PAID_PAYMENT_STATUSES = {
    "HELD",
    "DISPUTED",
    "RELEASE_PENDING",
    "RELEASED",
    "REFUND_PENDING",
    "REFUNDED",
}


def _payment_with_receipt(client, payment_id):
    """Fetch one payment and the receipt the API built for this viewer.

    The API decides who may see it — the record is scoped to the payer and the
    artisan in the query, and `receipt` is empty when the money has not arrived —
    so this helper only has to stop the page from pretending otherwise.
    """
    _, payment, _ = client.get(
        replace_path("/payments/{payment_id}/", payment_id=payment_id)
    )
    if not isinstance(payment, dict):
        return {}, {}
    receipt = payment.get("receipt") if isinstance(payment.get("receipt"), dict) else {}
    return payment, receipt


@api_guard
def payment_receipt(request, payment_id):
    """The receipt a payer keeps after a successful payment."""
    client = APIClient(request)
    try:
        payment, receipt = _payment_with_receipt(client, payment_id)
    except APIError as exc:
        handle_api_error(request, exc)
        return redirect("bookings")
    if not payment or not receipt.get("available"):
        messages.info(
            request,
            "There is no receipt for this payment yet. A receipt appears once the "
            "provider confirms the money.",
        )
        if payment:
            return redirect("payment-detail", payment_id=payment_id)
        return redirect("bookings")
    receipt["payment_id"] = str(payment.get("id") or payment_id)
    conversation_id = _conversation_for_payment(client, payment, request)
    back_url = (
        reverse("conversation-detail", kwargs={"conversation_id": conversation_id})
        if conversation_id
        else reverse("payment-detail", kwargs={"payment_id": payment_id})
    )
    return page(
        request,
        "payments/receipt.html",
        payment=payment,
        receipt=receipt,
        back_url=back_url,
    )


@api_guard
def payment_receipt_pdf(request, payment_id):
    """The same receipt as a PDF file the payer can keep or forward."""
    client = APIClient(request)
    try:
        payment, receipt = _payment_with_receipt(client, payment_id)
    except APIError as exc:
        handle_api_error(request, exc)
        return redirect("bookings")
    if not payment or not receipt.get("available"):
        messages.info(
            request,
            "There is no receipt for this payment yet. A receipt appears once the "
            "provider confirms the money.",
        )
        if payment:
            return redirect("payment-detail", payment_id=payment_id)
        return redirect("bookings")
    from .receipt_pdf import build_receipt_pdf

    document = build_receipt_pdf(receipt)
    filename = f"receipt-{receipt.get('number') or payment_id}.pdf"
    response = HttpResponse(document, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@api_guard
def payment_detail(request, payment_id):
    try:
        _, payment, _ = APIClient(request).get(
            replace_path("/payments/{payment_id}/", payment_id=payment_id)
        )
    except APIError as exc:
        handle_api_error(request, exc)
        return redirect("bookings")
    receipt = payment.get("receipt") if isinstance(payment, dict) else {}
    return page(
        request,
        "payments/detail.html",
        payment=payment,
        receipt=receipt if isinstance(receipt, dict) else {},
    )


@api_guard
def payout_account(request):
    client = APIClient(request)
    account = {}
    try:
        _, account, _ = client.get("/payments/payout-account/")
    except APIError as exc:
        if exc.status_code != 404:
            handle_api_error(request, exc)
        account = {}
    if not isinstance(account, dict):
        account = {}

    # Saved payout details lock after every change; replacing them needs the
    # emailed one-time code once the lock window has passed.
    locked_until = parse_datetime(str(account.get("payout_change_locked_until") or ""))
    has_account = bool(account.get("account_name"))
    payout_locked = bool(
        has_account and locked_until is not None and locked_until > timezone.now()
    )
    change_mode = False

    banks = []
    try:
        _, bank_payload, _ = client.get("/payments/payout-account/banks/")
        if isinstance(bank_payload, dict) and isinstance(
            bank_payload.get("banks"), list
        ):
            banks = bank_payload["banks"]
    except APIError as exc:
        handle_api_error(request, exc)

    action = str(request.POST.get("action") or "").strip() if request.method == "POST" else ""
    requested_change_mode = bool(
        has_account
        and not payout_locked
        and request.method == "POST"
        and request.POST.get("change_mode")
    )
    change_mode = requested_change_mode

    if request.method == "POST" and action == "cancel_change":
        messages.info(request, "Payout account change cancelled. Your saved details are unchanged.")
        return redirect("payout-account")

    if request.method == "POST" and action == "request_code":
        if not payout_locked:
            try:
                _, payload, _ = client.post("/payments/payout-account/request-otp/")
                detail = (
                    payload.get("detail")
                    if isinstance(payload, dict)
                    else None
                ) or "A 6-digit security code was sent to your email."
                messages.success(request, detail)
            except APIError as exc:
                if exc.status_code == 429:
                    messages.info(
                        request,
                        "A security code was already sent. Check your email for the latest code.",
                    )
                else:
                    handle_api_error(request, exc)
        else:
            messages.warning(
                request,
                "Payout details are still locked. You can request a change once the lock expires.",
            )
        # Stay on the page in change mode so the artisan can enter the code.
        change_mode = True

    form_data = request.POST if request.method == "POST" and not action else None
    form = PayoutAccountForm(
        form_data,
        initial=account,
        banks=banks,
        locked=has_account and not change_mode,
        requires_otp=change_mode,
    )

    if request.method == "POST" and not action:
        if payout_locked:
            messages.warning(
                request,
                "Payout details are locked. Use the change flow once the lock expires.",
            )
        elif form.is_valid():
            payload = {
                key: form.cleaned_data[key]
                for key in ("account_name", "bank_code", "account_number")
            }
            if form.cleaned_data.get("otp"):
                payload["otp"] = form.cleaned_data["otp"]
            try:
                _, account, _ = client.put(
                    "/payments/payout-account/",
                    payload,
                    idempotency_key=new_idempotency_key("payout-account-update"),
                )
                messages.success(
                    request,
                    "Flutterwave verified and activated your payout account. It is now locked from changes for 3 days.",
                )
                return redirect("payout-account")
            except APIError as exc:
                handle_api_error(request, exc)
                # Let the artisan retry with a code rather than re-locking silently.
                change_mode = has_account and not payout_locked
                if change_mode:
                    form = PayoutAccountForm(
                        request.POST,
                        banks=banks,
                        requires_otp=True,
                    )

    form_will_unlock_at = None
    if payout_locked and locked_until is not None:
        form_will_unlock_at = timezone.localtime(locked_until)

    return page(
        request,
        "payments/payout_account.html",
        account=account,
        form=form,
        payout_locked=payout_locked,
        locked_until=form_will_unlock_at,
        change_mode=change_mode,
        has_saved_account=has_account,
    )


@api_guard
def payout_verify(request, pk):
    if request.method != "POST":
        return page(request, "payments/payout_verify.html", pk=pk)
    try:
        _, payload, _ = APIClient(request).post(
            replace_path("/payments/payout-account/{pk}/verify/", pk=pk),
            idempotency_key=new_idempotency_key("payout-account-verify"),
        )
        messages.success(
            request,
            payload_message(payload) or "Payout account verification requested.",
        )
    except APIError as exc:
        handle_api_error(request, exc)
    return redirect("payout-account")


# ---------------- Reviews ----------------


@api_guard
def review_create(request):
    form = ReviewForm(
        request.POST or None, initial={"booking": request.GET.get("booking", "")}
    )
    if request.method == "POST" and form.is_valid():
        try:
            _, review, _ = APIClient(request).post(
                "/reviews/",
                cleaned_payload(form.cleaned_data),
                idempotency_key=new_idempotency_key("review-create"),
            )
            messages.success(request, "Thank you. Your review has been submitted.")
            booking_id = review.get("booking")
            target = reverse("review-thanks")
            if booking_id:
                target = f"{target}?booking={booking_id}"
            return redirect(target)
        except APIError as exc:
            handle_api_error(request, exc)
    return page(
        request,
        "reviews/new.html",
        form=form,
        star_values=[1, 2, 3, 4, 5],
    )


@api_guard
def review_thanks(request):
    """The review landed; show the receipt and ask one small question about us."""
    booking_id = str(request.GET.get("booking") or "").strip()
    next_url = ""
    if booking_id:
        next_url = safe_internal_next(
            request, reverse("booking-detail", kwargs={"pk": booking_id})
        )
    return page(
        request,
        "reviews/thanks.html",
        next_url=next_url,
    )


@require_POST
def review_improvement(request):
    """One improvement note about the platform itself, kept away from ratings."""
    note = str(request.POST.get("note") or "").strip()
    destination = safe_internal_next(request, request.POST.get("next", "")) or ""
    if not note:
        messages.info(request, "Nothing was written, so nothing was sent.")
        return redirect(destination or "bookings")
    try:
        APIClient(request).post(
            "/reviews/platform-feedback/",
            {"note": note[:2000], "page": "review"},
            idempotency_key=new_idempotency_key("platform-feedback"),
        )
        messages.success(request, "Thank you — that is exactly what we need to fix.")
    except APIError as exc:
        handle_api_error(request, exc)
    return redirect(destination or "bookings")


# ---------------- Customer cancellation risk ----------------


@api_guard
def cancellation_risk(request):
    try:
        _, risk, _ = APIClient(request).get("/risk/customer/cancellation-risk/")
    except APIError as exc:
        handle_api_error(request, exc)
        return redirect("dashboard")
    return page(request, "customer/risk.html", risk=risk)


# ---------------- Support ----------------


def _support_page_meta(request, payload, items, page_number, page_size):
    total = _count_from_payload(payload)
    page_count = max(1, math.ceil(total / page_size))
    offset = (page_number - 1) * page_size

    def page_url(number):
        query = request.GET.copy()
        query["page"] = str(number)
        return f"{request.path}?{query.urlencode()}"

    has_previous = page_number > 1
    has_next = offset + len(items) < total
    return {
        "result_total": total,
        "page_number": page_number,
        "page_count": page_count,
        "has_previous": has_previous,
        "has_next": has_next,
        "previous_url": page_url(page_number - 1) if has_previous else "",
        "next_url": page_url(page_number + 1) if has_next else "",
        "range_start": offset + 1 if items else 0,
        "range_end": min(offset + len(items), total),
    }


def _page_number(request):
    try:
        return max(1, min(int(request.GET.get("page") or 1), 10000))
    except (TypeError, ValueError):
        return 1


@api_guard
def support_promotion_applications(request):
    client = APIClient(request)
    capabilities = _support_capabilities(client)
    if not capabilities.get("can_review_promotions"):
        messages.error(
            request, "Your Customer Support role cannot review promotion applications."
        )
        return redirect("support-operations")

    status_filter = str(request.GET.get("status") or "PENDING_REVIEW").upper()
    promotion_type = str(request.GET.get("promotion_type") or "").upper()
    query = " ".join(str(request.GET.get("q") or "").split())[:120]
    try:
        limit = min(60, max(1, int(request.GET.get("limit") or 20)))
        offset = max(0, int(request.GET.get("offset") or 0))
    except (TypeError, ValueError):
        limit, offset = 20, 0
    params = {"status": status_filter, "limit": limit, "offset": offset}
    if promotion_type:
        params["promotion_type"] = promotion_type
    if query:
        params["q"] = query
    try:
        _, payload, _ = client.get("/promotions/review/applications/", params=params)
    except APIError as exc:
        handle_api_error(request, exc)
        payload = {"count": 0, "results": []}
    items = extract_items(payload)
    for item in items:
        if isinstance(item.get("submitted_at"), str):
            item["submitted_at"] = (
                parse_datetime(item["submitted_at"]) or item["submitted_at"]
            )
    total = _count_from_payload(payload)
    next_offset = offset + limit if offset + limit < total else None
    previous_offset = max(0, offset - limit) if offset > 0 else None
    return page(
        request,
        "support/promotion_applications.html",
        role="staff",
        support_capabilities=capabilities,
        promotion_applications=items,
        promotion_application_total=total,
        status_filter=status_filter,
        promotion_type_filter=promotion_type,
        promotion_query=query,
        promotion_limit=limit,
        promotion_offset=offset,
        promotion_next_offset=next_offset,
        promotion_previous_offset=previous_offset,
    )


@api_guard
def support_promotion_application_detail(request, pk):
    client = APIClient(request)
    capabilities = _support_capabilities(client)
    if not capabilities.get("can_review_promotions"):
        messages.error(
            request, "Your Customer Support role cannot inspect promotion applications."
        )
        return redirect("support-operations")
    path = replace_path("/promotions/review/applications/{pk}/", pk=pk)
    form = PromotionDecisionForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            _, result, _ = client.post(
                replace_path("/promotions/review/applications/{pk}/decision/", pk=pk),
                data=form.cleaned_data,
            )
            messages.success(
                request,
                "Your review decision was recorded and the applicant was notified.",
            )
            return redirect("support-promotion-application-detail", pk=pk)
        except APIError as exc:
            handle_api_error(request, exc)
    try:
        _, application, _ = client.get(path)
    except APIError as exc:
        handle_api_error(request, exc)
        return redirect("support-promotion-applications")
    if isinstance(application, dict):
        for field in ("submitted_at", "reviewed_at"):
            if isinstance(application.get(field), str):
                application[field] = (
                    parse_datetime(application[field]) or application[field]
                )
        snapshot = application.get("current_revision_snapshot")
        if isinstance(snapshot, dict) and isinstance(snapshot.get("submitted_at"), str):
            snapshot["submitted_at"] = (
                parse_datetime(snapshot["submitted_at"]) or snapshot["submitted_at"]
            )
        for revision in application.get("revision_history", []) or []:
            if isinstance(revision.get("submitted_at"), str):
                revision["submitted_at"] = (
                    parse_datetime(revision["submitted_at"]) or revision["submitted_at"]
                )
            for sample in revision.get("evidence", []) or []:
                if isinstance(sample.get("uploaded_at"), str):
                    sample["uploaded_at"] = (
                        parse_datetime(sample["uploaded_at"]) or sample["uploaded_at"]
                    )
        for decision in application.get("decisions", []) or []:
            if isinstance(decision.get("created_at"), str):
                decision["created_at"] = (
                    parse_datetime(decision["created_at"]) or decision["created_at"]
                )
    return page(
        request,
        "support/promotion_application_detail.html",
        role="staff",
        support_capabilities=capabilities,
        application=application,
        decision_form=form,
        page_js_path="js/pages/support-promotion-review.js",
    )


@api_guard
def support_promotion_evidence_proxy(request, application_id, evidence_id):
    capabilities = _support_capabilities(APIClient(request))
    if not capabilities.get("can_review_promotions"):
        return HttpResponse(
            "Promotion review permission required.",
            status=403,
            content_type="text/plain; charset=utf-8",
        )
    client = APIClient(request)
    path = replace_path(
        "/promotions/applications/{application_id}/evidence/{evidence_id}/",
        application_id=application_id,
        evidence_id=evidence_id,
    )
    try:
        content, content_type, _headers = client.get_file(path)
    except APIError as exc:
        handle_api_error(request, exc)
        return HttpResponse(
            exc.user_message,
            status=exc.status_code,
            content_type="text/plain; charset=utf-8",
        )
    response = HttpResponse(content, content_type=content_type)
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _support_conversation_context(request, client, raw_conversation_id):
    raw_conversation_id = str(raw_conversation_id or "").strip()
    if not raw_conversation_id:
        return "", None
    try:
        conversation_id = str(UUID(raw_conversation_id))
    except (TypeError, ValueError, AttributeError):
        messages.error(
            request,
            "That conversation link is invalid. Open support from the relevant conversation instead.",
        )
        return "", None
    try:
        _, conversation, _ = client.get(
            replace_path(
                "/conversations/{conversation_id}/", conversation_id=conversation_id
            )
        )
    except APIError as exc:
        handle_api_error(request, exc)
        return "", None
    if (
        not isinstance(conversation, dict)
        or str(conversation.get("id") or "") != conversation_id
        or not conversation.get("viewer_role")
    ):
        messages.error(
            request, "That conversation could not be verified for this account."
        )
        return "", None
    return conversation_id, conversation


@api_guard
def support_cases(request):
    client = APIClient(request)
    capabilities = _support_capabilities(client)
    support_mode = bool(capabilities.get("is_support_staff"))
    promotion_campaign_prefill = str(
        request.GET.get("promotion_campaign") or ""
    ).strip()
    requested_conversation_id = (
        request.POST.get("conversation_id")
        if request.method == "POST"
        else request.GET.get("conversation_id")
    )
    conversation_context_id, conversation_context = (
        _support_conversation_context(request, client, requested_conversation_id)
        if not support_mode
        else ("", None)
    )
    conversation_context_invalid = bool(
        requested_conversation_id and not conversation_context_id and not support_mode
    )
    initial = {}
    if promotion_campaign_prefill and request.method != "POST":
        initial.update(
            promotion_campaign=promotion_campaign_prefill, case_type="REPORT"
        )
    if conversation_context_id and request.method != "POST":
        initial["conversation_id"] = conversation_context_id
    case_form = (
        SupportCaseForm(request.POST or None, initial=initial or None)
        if not support_mode
        else None
    )

    if (
        request.method == "POST"
        and case_form
        and case_form.is_valid()
        and not conversation_context_invalid
    ):
        try:
            _, case, _ = client.post(
                "/support/cases/", cleaned_payload(case_form.cleaned_data)
            )
            messages.success(
                request,
                "Support case opened. You can message customer support from its case thread.",
            )
            return redirect("support-case-detail", pk=case["id"])
        except APIError as exc:
            handle_api_error(request, exc)

    page_size = 20
    page_number = _page_number(request)
    params = {"limit": page_size, "offset": (page_number - 1) * page_size}
    filters = {"search": str(request.GET.get("search") or "").strip()[:100]}
    if filters["search"]:
        params["search"] = filters["search"]

    if support_mode:
        allowed_filters = {
            "status": {"OPEN", "UNDER_REVIEW", "RESOLVED", "CLOSED"},
            "priority": {"LOW", "MEDIUM", "HIGH", "URGENT"},
            "case_type": {
                "DISPUTE",
                "REPORT",
                "PAYMENT",
                "ACCOUNT",
                "TECHNICAL",
                "OTHER",
            },
        }
        for key, choices in allowed_filters.items():
            value = str(request.GET.get(key) or "").upper()
            if value in choices:
                params[key] = value
                filters[key] = value
        assigned_to = str(request.GET.get("assigned_to") or "")
        if assigned_to in {"me", "unassigned"} or assigned_to.isdigit():
            params["assigned_to"] = assigned_to
            filters["assigned_to"] = assigned_to

    try:
        _, payload, _ = client.get("/support/cases/", params=params)
        cases = extract_items(payload)
    except APIError as exc:
        handle_api_error(request, exc)
        payload, cases = [], []

    return page(
        request,
        "support/cases.html",
        role="staff" if support_mode else "",
        cases=cases,
        form=case_form,
        support_mode=support_mode,
        support_capabilities=capabilities,
        conversation_context_id=conversation_context_id,
        conversation_context=conversation_context,
        filters=filters,
        **_support_page_meta(request, payload, cases, page_number, page_size),
    )


@api_guard
def support_case_detail(request, pk):
    client = APIClient(request)
    capabilities = _support_capabilities(client)
    message_form = SupportCaseMessageForm(request.POST or None)
    message_key = request.POST.get("idempotency_key") or new_idempotency_key(
        "support-message"
    )

    if request.method == "POST" and message_form.is_valid():
        try:
            client.post(
                replace_path("/support/cases/{pk}/messages/", pk=pk),
                {"message": message_form.cleaned_data["message"]},
                idempotency_key=message_key,
            )
            messages.success(request, "Your message was sent to customer support.")
            response = redirect("support-case-detail", pk=pk)
            response["Location"] = f"{response['Location']}#latest-message"
            return response
        except APIError as exc:
            handle_api_error(request, exc)

    try:
        _, case, _ = client.get(replace_path("/support/cases/{pk}/", pk=pk))
        for timestamp_field in ("created_at", "updated_at", "resolved_at", "closed_at"):
            if isinstance(case, dict) and isinstance(case.get(timestamp_field), str):
                case[timestamp_field] = (
                    parse_datetime(case[timestamp_field]) or case[timestamp_field]
                )
    except APIError as exc:
        handle_api_error(request, exc)
        return redirect("support-cases")

    support_mode = bool(case.get("can_manage") or capabilities.get("is_support_staff"))
    try:
        _, events_payload, _ = client.get(
            replace_path("/support/cases/{pk}/events/", pk=pk)
        )
        all_events = extract_items(events_payload)
        staff_notes = (
            [event for event in all_events if event.get("action") == "CASE_NOTE_ADDED"]
            if support_mode
            else []
        )
        case_events = [
            event for event in all_events if event.get("action") != "CASE_NOTE_ADDED"
        ]
        for event in case_events + staff_notes:
            if isinstance(event.get("created_at"), str):
                event["created_at"] = (
                    parse_datetime(event["created_at"]) or event["created_at"]
                )
    except APIError as exc:
        handle_api_error(request, exc)
        case_events, staff_notes = [], []

    can_start_refund = bool(
        capabilities.get("can_request_refunds")
        and case.get("case_type") in {"DISPUTE", "PAYMENT"}
        and case.get("status") in {"OPEN", "UNDER_REVIEW"}
    )
    return page(
        request,
        "support/case_detail.html",
        case=case,
        case_events=case_events,
        staff_notes=staff_notes,
        support_mode=support_mode,
        support_capabilities=capabilities,
        can_start_refund=can_start_refund,
        message_form=message_form,
        message_key=message_key,
    )


@api_guard
def support_case_events(request, pk):
    client = APIClient(request)
    capabilities = _support_capabilities(client)
    try:
        _, payload, _ = client.get(replace_path("/support/cases/{pk}/events/", pk=pk))
    except APIError as exc:
        handle_api_error(request, exc)
        payload = []
    return page(
        request,
        "support/events.html",
        events=extract_items(payload),
        pk=pk,
        support_capabilities=capabilities,
    )


def _support_action(
    request,
    path_template,
    pk,
    form_cls=None,
    title="Case action",
    *,
    capability=None,
    form_kwargs=None,
    client=None,
    capabilities=None,
):
    client = client or APIClient(request)
    capabilities = capabilities or _support_capabilities(client)
    if capability and not capabilities.get(capability):
        messages.error(request, "Your support role cannot perform this case action.")
        return redirect("support-case-detail", pk=pk)

    try:
        _, case_record, _ = client.get(replace_path("/support/cases/{pk}/", pk=pk))
    except APIError as exc:
        handle_api_error(request, exc)
        return redirect("support-cases")

    if form_cls is ResolveForm:
        recorded_code = (
            case_record.get("resolution_code")
            if isinstance(case_record, dict)
            else None
        )
        valid_codes = {
            str(value)
            for value, _label in ResolveForm.base_fields["resolution_code"].choices
        }
        if recorded_code in valid_codes and request.method != "POST":
            form_kwargs = {
                **(form_kwargs or {}),
                "initial": {"resolution_code": recorded_code},
            }
    form = form_cls(request.POST or None, **(form_kwargs or {})) if form_cls else None
    if request.method == "POST" and (form is None or form.is_valid()):
        try:
            _, payload, _ = client.post(
                replace_path(path_template, pk=pk),
                cleaned_payload(form.cleaned_data) if form else None,
            )
            messages.success(
                request, payload_message(payload) or "Support case updated."
            )
            return redirect("support-case-detail", pk=pk)
        except APIError as exc:
            handle_api_error(request, exc)
    return page(
        request,
        "support/action.html",
        form=form,
        title=title,
        pk=pk,
        case_record=case_record,
        cancel_url=reverse("support-case-detail", kwargs={"pk": pk}),
        action_intro="The support API checks your role and the current case state before applying this change.",
    )


@api_guard
def support_case_claim(request, pk):
    return _support_action(
        request,
        "/support/cases/{pk}/claim/",
        pk,
        title="Claim support case",
        capability="can_claim_cases",
    )


@api_guard
def support_case_assign(request, pk):
    client = APIClient(request)
    capabilities = _support_capabilities(client)
    if not capabilities.get("can_assign_cases"):
        messages.error(request, "Your support role cannot assign cases.")
        return redirect("support-case-detail", pk=pk)
    try:
        _, staff_payload, _ = client.get("/support/cases/staff/")
        staff = extract_items(staff_payload)
    except APIError as exc:
        handle_api_error(request, exc)
        staff = []
    return _support_action(
        request,
        "/support/cases/{pk}/assign/",
        pk,
        AssignForm,
        "Assign support case",
        capability="can_assign_cases",
        form_kwargs={"staff": staff},
        client=client,
        capabilities=capabilities,
    )


@api_guard
def support_case_note(request, pk):
    return _support_action(
        request,
        "/support/cases/{pk}/notes/",
        pk,
        NoteForm,
        "Add internal support note",
        capability="can_add_private_notes",
    )


@api_guard
def support_case_resolve(request, pk):
    return _support_action(
        request,
        "/support/cases/{pk}/resolve/",
        pk,
        ResolveForm,
        "Resolve support case",
        capability="can_resolve_cases",
    )


@api_guard
def support_case_close(request, pk):
    return _support_action(
        request,
        "/support/cases/{pk}/close/",
        pk,
        title="Close support case",
        capability="can_close_cases",
    )


@api_guard
def support_refunds(request):
    client = APIClient(request)
    capabilities = _support_capabilities(client)
    if not capabilities.get("can_view_refunds"):
        messages.error(request, "Refund queues are available to support staff only.")
        return redirect("support-cases")

    page_size = 20
    page_number = _page_number(request)
    params = {"limit": page_size, "offset": (page_number - 1) * page_size}
    filters = {}
    status_value = str(request.GET.get("status") or "").upper()
    allowed_statuses = {
        "REQUESTED",
        "APPROVED",
        "PROCESSING",
        "PROCESSING_UNKNOWN",
        "SUCCEEDED",
        "FAILED",
        "REJECTED",
    }
    if status_value in allowed_statuses:
        params["status"] = filters["status"] = status_value
    action_type = str(request.GET.get("action_type") or "").upper()
    if action_type in {"REFUND", "PARTIAL_REFUND"}:
        params["action_type"] = filters["action_type"] = action_type
    case_value = str(request.GET.get("case") or "")
    if case_value.isdigit():
        params["case"] = filters["case"] = case_value

    try:
        _, payload, _ = client.get("/support/cases/refunds/", params=params)
        refunds = extract_items(payload)
    except APIError as exc:
        handle_api_error(request, exc)
        payload, refunds = [], []

    return page(
        request,
        "support/refunds.html",
        role="staff",
        refunds=refunds,
        filters=filters,
        support_capabilities=capabilities,
        **_support_page_meta(request, payload, refunds, page_number, page_size),
    )


@api_guard
def support_refund_request(request, pk):
    client = APIClient(request)
    capabilities = _support_capabilities(client)
    if not capabilities.get("can_request_refunds"):
        messages.error(request, "Your support role cannot request refunds.")
        return redirect("support-case-detail", pk=pk)

    try:
        _, payment_payload, _ = client.get(
            replace_path("/support/cases/{pk}/eligible-payments/", pk=pk)
        )
        eligible_payments = extract_items(payment_payload)
    except APIError as exc:
        handle_api_error(request, exc)
        eligible_payments = []

    form = RefundRequestForm(
        request.POST or None,
        eligible_payments=eligible_payments,
    )
    if request.method == "POST" and form.is_valid():
        try:
            _, action, _ = client.post(
                replace_path("/support/cases/{pk}/refunds/", pk=pk),
                cleaned_payload(form.cleaned_data),
                idempotency_key=new_idempotency_key("refund-request"),
            )
            messages.success(
                request,
                "Refund request created. It now needs an independent support approval.",
            )
            return redirect("support-refund-detail", refund_id=action["id"])
        except APIError as exc:
            handle_api_error(request, exc)

    return page(
        request,
        "support/refund_action.html",
        form=form,
        title="Request refund",
        pk=pk,
        eligible_payments=eligible_payments,
        cancel_url=reverse("support-case-detail", kwargs={"pk": pk}),
    )


@api_guard
def support_refund_detail(request, refund_id):
    client = APIClient(request)
    try:
        _, refund, _ = client.get(
            replace_path("/support/cases/refunds/{refund_id}/", refund_id=refund_id)
        )
    except APIError as exc:
        handle_api_error(request, exc)
        return redirect("support-cases")

    capabilities = _support_capabilities(client)
    refund_events = []
    case_id = refund.get("case") if isinstance(refund, dict) else None
    if case_id:
        try:
            _, event_payload, _ = client.get(
                replace_path("/support/cases/{pk}/events/", pk=case_id)
            )
            for event in extract_items(event_payload):
                metadata = (
                    event.get("metadata")
                    if isinstance(event.get("metadata"), dict)
                    else {}
                )
                if str(event.get("action") or "").startswith(
                    "REFUND_"
                ) and metadata.get("refund_reference") == refund.get("reference"):
                    if isinstance(event.get("created_at"), str):
                        event["created_at"] = (
                            parse_datetime(event["created_at"]) or event["created_at"]
                        )
                    refund_events.append(event)
        except APIError:
            refund_events = []

    user_id = request.session.get("api_user", {}).get("id")
    is_superuser = bool(capabilities.get("is_superuser"))
    requester_is_viewer = (
        str(refund.get("requested_by")) == str(user_id)
        if refund.get("requested_by") and user_id
        else False
    )
    approver_is_viewer = (
        str(refund.get("approved_by")) == str(user_id)
        if refund.get("approved_by") and user_id
        else False
    )
    status_value = refund.get("status")
    return page(
        request,
        "support/refund_detail.html",
        refund=refund,
        refund_events=refund_events,
        support_capabilities=capabilities,
        can_approve_refund=bool(
            capabilities.get("can_approve_refunds")
            and status_value == "REQUESTED"
            and (not requester_is_viewer or is_superuser)
        ),
        can_reject_refund=bool(
            capabilities.get("can_approve_refunds")
            and status_value == "REQUESTED"
            and (not requester_is_viewer or is_superuser)
        ),
        can_execute_refund=bool(
            capabilities.get("can_execute_refunds")
            and status_value == "APPROVED"
            and (not approver_is_viewer or is_superuser)
        ),
        can_reconcile_refund=bool(
            capabilities.get("can_reconcile_refunds")
            and status_value in {"PROCESSING", "PROCESSING_UNKNOWN"}
        ),
    )


def _refund_action(
    request,
    path_template,
    refund_id,
    form_cls=None,
    title="Refund action",
    *,
    capability=None,
):
    client = APIClient(request)
    capabilities = _support_capabilities(client)
    if capability and not capabilities.get(capability):
        messages.error(request, "Your support role cannot perform this refund action.")
        return redirect("support-refund-detail", refund_id=refund_id)

    try:
        _, refund_record, _ = client.get(
            replace_path("/support/cases/refunds/{refund_id}/", refund_id=refund_id)
        )
    except APIError as exc:
        handle_api_error(request, exc)
        return redirect("support-refunds")

    form = form_cls(request.POST or None) if form_cls else None
    if request.method == "POST" and (form is None or form.is_valid()):
        try:
            _, payload, _ = client.post(
                replace_path(path_template, refund_id=refund_id),
                cleaned_payload(form.cleaned_data) if form else None,
                idempotency_key=new_idempotency_key("refund-action"),
            )
            messages.success(
                request, payload_message(payload) or "Refund action completed."
            )
            return redirect("support-refund-detail", refund_id=refund_id)
        except APIError as exc:
            handle_api_error(request, exc)
    return page(
        request,
        "support/action.html",
        form=form,
        title=title,
        refund_id=refund_id,
        refund_record=refund_record,
        cancel_url=reverse("support-refund-detail", kwargs={"refund_id": refund_id}),
        action_intro="The API enforces refund permissions, amount checks, separation of duties, and provider state before processing this action.",
    )


@api_guard
def support_refund_approve(request, refund_id):
    return _refund_action(
        request,
        "/support/cases/refunds/{refund_id}/approve/",
        refund_id,
        ApprovalForm,
        "Approve refund",
        capability="can_approve_refunds",
    )


@api_guard
def support_refund_reject(request, refund_id):
    return _refund_action(
        request,
        "/support/cases/refunds/{refund_id}/reject/",
        refund_id,
        RejectionForm,
        "Reject refund",
        capability="can_approve_refunds",
    )


@api_guard
def support_refund_execute(request, refund_id):
    return _refund_action(
        request,
        "/support/cases/refunds/{refund_id}/execute/",
        refund_id,
        title="Execute refund",
        capability="can_execute_refunds",
    )


@api_guard
def support_refund_reconcile(request, refund_id):
    return _refund_action(
        request,
        "/support/cases/refunds/{refund_id}/reconcile/",
        refund_id,
        title="Reconcile refund",
        capability="can_reconcile_refunds",
    )


@api_guard
def support_staff(request):
    client = APIClient(request)
    capabilities = _support_capabilities(client)
    if not capabilities.get("can_manage_staff"):
        messages.error(
            request,
            "Staff directory access requires a supervisor or support administrator role.",
        )
        return redirect("dashboard")
    try:
        _, payload, _ = client.get("/support/cases/staff/")
        staff = extract_items(payload)
    except APIError as exc:
        handle_api_error(request, exc)
        staff = []
    return page(
        request,
        "support/staff.html",
        role="staff",
        staff=staff,
        support_capabilities=capabilities,
    )


@api_guard
def support_staff_roles(request, user_id):
    client = APIClient(request)
    capabilities = _support_capabilities(client)
    if not capabilities.get("can_manage_roles"):
        messages.error(request, "Only support administrators can edit staff roles.")
        return redirect("support-staff")

    staff_member = None
    try:
        _, staff_payload, _ = client.get("/support/cases/staff/")
        staff_member = next(
            (
                item
                for item in extract_items(staff_payload)
                if str(item.get("id")) == str(user_id)
            ),
            None,
        )
    except APIError as exc:
        handle_api_error(request, exc)

    if staff_member is None:
        messages.error(request, "That support staff account could not be found.")
        return redirect("support-staff")

    form = RolesForm(
        request.POST or None,
        initial={"roles": staff_member.get("roles", [])},
    )
    if request.method == "POST" and form.is_valid():
        try:
            _, updated_staff, _ = client.put(
                replace_path("/support/cases/staff/{user_id}/roles/", user_id=user_id),
                cleaned_payload(form.cleaned_data),
            )
            messages.success(
                request,
                f"Roles updated for {updated_staff.get('name') or staff_member.get('name')}.",
            )
            return redirect("support-staff")
        except APIError as exc:
            handle_api_error(request, exc)
    return page(
        request,
        "support/roles.html",
        form=form,
        staff_member=staff_member,
        user_id=user_id,
        support_capabilities=capabilities,
    )


@require_POST
@api_guard
def support_staff_password_reset(request, user_id):
    client = APIClient(request)
    capabilities = _support_capabilities(client)
    if not capabilities.get("is_superuser"):
        messages.error(
            request, "Only a platform manager can initiate staff password recovery."
        )
        return redirect("support-staff")
    path = replace_path("/staff-password-resets/{user_id}/initiate/", user_id=user_id)
    try:
        _, payload, _ = client.post(path, {})
        messages.success(
            request,
            payload_message(payload) or "A staff password recovery code was requested.",
        )
    except APIError as exc:
        handle_api_error(request, exc)
    return redirect("support-staff")


@require_POST
@api_guard
def support_staff_security_setup(request, user_id):
    client = APIClient(request)
    capabilities = _support_capabilities(client)
    if not capabilities.get("is_superuser"):
        messages.error(
            request,
            "Only a platform manager can authorize staff security-question setup.",
        )
        return redirect("support-staff")
    path = replace_path(
        "/staff-password-resets/{user_id}/security-setup/", user_id=user_id
    )
    try:
        _, payload, _ = client.post(path, {})
        messages.success(
            request,
            payload_message(payload)
            or "A staff security-question setup code was requested.",
        )
    except APIError as exc:
        handle_api_error(request, exc)
    return redirect("support-staff")


# ---------------- System ----------------


@api_guard
@require_POST
def manager_snapshot_schedule(request):
    client = APIClient(request)
    capabilities = _support_capabilities(client)
    if not capabilities.get("is_superuser"):
        messages.error(
            request, "Only a platform superuser can change manager snapshot frequency."
        )
        return redirect("support-operations")
    frequency = str(request.POST.get("frequency") or "").strip().upper()
    if frequency not in {"HOURLY", "DAILY", "EVERY_THREE_DAYS", "WEEKLY", "MONTHLY"}:
        messages.error(request, "Choose a supported snapshot frequency.")
        return redirect("dashboard")
    try:
        reason = str(request.POST.get("reason") or "").strip()[:1000]
        client.patch(
            "/manager/snapshot-schedule/",
            data={"frequency": frequency, "reason": reason},
        )
        messages.success(
            request,
            "Manager snapshot frequency was saved. Celery beat and a worker will honor the new schedule.",
        )
    except APIError as exc:
        handle_api_error(request, exc)
    return redirect("dashboard")


@require_POST
def promotion_performance_event(request):
    """Same-origin, CSRF-protected proxy for anonymous promotion placements impression/click events."""
    if len(request.body) > 8192:
        return JsonResponse({"recorded": False}, status=413)
    try:
        payload = json.loads(request.body or b"{}")
    except (TypeError, ValueError):
        return JsonResponse({"recorded": False}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"recorded": False}, status=400)
    event = {
        key: payload.get(key)
        for key in ("token", "visitor_id", "event_type", "artisan_id")
        if payload.get(key) is not None
    }
    try:
        _, result, _ = APIClient(request).post("/promotions/performance-events/", event)
    except APIError as exc:
        return JsonResponse(
            {"recorded": False},
            status=exc.status_code if 400 <= exc.status_code < 600 else 502,
        )
    return JsonResponse(
        {
            "recorded": (
                bool(result.get("recorded")) if isinstance(result, dict) else False
            )
        }
    )


def health_live(request):
    try:
        _, payload, _ = APIClient(request).get("/health/live/")
    except APIError as exc:
        payload = {"status": "unavailable", "detail": str(exc)}
    return page(request, "system/health.html", title="Live health", health=payload)


def health_ready(request):
    try:
        _, payload, _ = APIClient(request).get("/health/ready/")
    except APIError as exc:
        payload = {"status": "unavailable", "detail": str(exc)}
    return page(request, "system/health.html", title="Readiness", health=payload)


# ---------------- Notifications ----------------


def safe_internal_url(value):
    """Accept same-site root-relative links only; absolute and protocol-relative URLs are rejected."""
    value = str(value or "").strip()
    if (
        not value.startswith("/")
        or value.startswith("//")
        or any(char in value for char in ("\\", "\n", "\r"))
    ):
        return ""
    decoded = unquote(value)
    if decoded.startswith("//") or any(char in decoded for char in ("\\", "\n", "\r")):
        return ""
    return value


def _notification_back_url(request):
    explicit_destination = request.POST.get("back_to") or request.GET.get("next")
    candidate = explicit_destination or request.META.get("HTTP_REFERER")
    safe_url = safe_internal_next(request, candidate)

    if not safe_url and not explicit_destination:
        referer = str(request.META.get("HTTP_REFERER") or "").strip()
        if referer and not any(char in referer for char in ("\\", "\r", "\n")):
            try:
                parsed_referer = urlparse(referer)
                same_site = (
                    parsed_referer.scheme in {"http", "https"}
                    and bool(parsed_referer.netloc)
                    and not parsed_referer.username
                    and not parsed_referer.password
                    and url_has_allowed_host_and_scheme(
                        referer,
                        allowed_hosts={request.get_host()},
                        require_https=request.is_secure(),
                    )
                )
                if (
                    same_site
                    and parsed_referer.path.startswith("/")
                    and not parsed_referer.path.startswith("//")
                ):
                    local_path = parsed_referer.path
                    if parsed_referer.query:
                        local_path += "?" + parsed_referer.query
                    if parsed_referer.fragment:
                        local_path += "#" + parsed_referer.fragment
                    safe_url = safe_internal_next(request, local_path)
            except ValueError:
                safe_url = ""

    if safe_url and urlparse(safe_url).path == reverse("notifications-page"):
        safe_url = ""
    return safe_url or reverse("home")


def _notification_list_url(*, only_unread, offset, back_url, delete_all=False):
    query = {"filter": "unread" if only_unread else "all"}
    if offset and not delete_all:
        query["offset"] = offset
    if back_url and back_url != reverse("home"):
        query["next"] = back_url
    return f"{reverse('notifications-page')}?{urlencode(query)}"


@api_guard
def notification_popups(request):
    """Same-origin bridge to the popup feed.

    The browser never talks to the API directly: the session-authenticated
    frontend proxies the request, so no API credential is exposed to the page.
    """
    from django.http import JsonResponse

    if request.method != "GET":
        return JsonResponse({"detail": "Use GET."}, status=405)
    try:
        _, payload, _ = APIClient(request).get("/notifications/popups/")
    except APIError:
        return JsonResponse([], safe=False)
    return JsonResponse(payload, safe=False)


@api_guard
def notification_acknowledge(request, pk):
    """Acknowledge one popup: the durable notification is marked read."""
    from django.http import JsonResponse

    if request.method != "POST":
        return JsonResponse({"detail": "Use POST."}, status=405)
    try:
        APIClient(request).post(f"/notifications/{int(pk)}/read/", {})
    except APIError as exc:
        return JsonResponse({"detail": str(exc)}, status=exc.status_code)
    except (TypeError, ValueError):
        return JsonResponse({"detail": "Invalid notification."}, status=400)
    return JsonResponse({"status": "read"})


@api_guard
def notifications_page(request):
    client = APIClient(request)
    only_unread = (
        request.GET.get("filter") == "unread" or request.POST.get("filter") == "unread"
    )
    offset_value = request.GET.get("offset") or request.POST.get("offset", "0")
    offset = int(offset_value) if str(offset_value).isdigit() else 0
    back_url = _notification_back_url(request)

    if request.method == "POST":
        action = request.POST.get("action")
        delete_all = action == "delete_all"
        redirect_url = _notification_list_url(
            only_unread=only_unread,
            offset=offset,
            back_url=back_url,
            delete_all=delete_all,
        )
        try:
            if action == "read_all":
                client.post("/notifications/read-all/", {})
                messages.success(request, "All notifications were marked as read.")
            elif action == "read":
                notification_id = request.POST.get("notification_id", "")
                if not notification_id.isdigit():
                    messages.error(
                        request,
                        "Choose a valid notification before marking it as read.",
                    )
                    return redirect(redirect_url)
                client.post(f"/notifications/{int(notification_id)}/read/", {})
                messages.success(request, "Notification marked as read.")
            elif action == "delete":
                notification_id = request.POST.get("notification_id", "")
                if not notification_id.isdigit():
                    messages.error(request, "Choose a valid notification to delete.")
                    return redirect(redirect_url)
                client.delete(f"/notifications/{int(notification_id)}/")
                messages.success(request, "Notification deleted.")
            elif action == "delete_all":
                _, result, _ = client.delete("/notifications/delete-all/")
                deleted = result.get("deleted", 0) if isinstance(result, dict) else 0
                if deleted:
                    messages.success(
                        request,
                        f"{deleted} notification{'s' if deleted != 1 else ''} deleted.",
                    )
                else:
                    messages.info(
                        request, "There were no notifications left to delete."
                    )
            else:
                messages.error(request, "That notification action is not available.")
                return redirect(redirect_url)
        except APIError as exc:
            handle_api_error(request, exc)
            return redirect(redirect_url)

        request.session.pop("notif_unread_checked_at", None)
        return redirect(redirect_url)

    only_unread = request.GET.get("filter") == "unread"
    offset = (
        int(request.GET["offset"]) if request.GET.get("offset", "").isdigit() else 0
    )
    params = {"limit": 20, "offset": offset}
    if only_unread:
        params["unread"] = "true"
    items, total = [], 0
    try:
        _, payload, _ = client.get("/notifications/", params=params)
        items = extract_items(payload)
        total = (
            payload.get("count", len(items))
            if isinstance(payload, dict)
            else len(items)
        )
    except APIError as exc:
        handle_api_error(request, exc)
    for item in items:
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        item["safe_url"] = safe_internal_url(data.get("action_url"))
        item["safe_action_label"] = str(data.get("action_label") or "Open update")[:60]
        created_at = parse_datetime(str(item.get("created_at") or ""))
        if created_at:
            if timezone.is_naive(created_at):
                created_at = timezone.make_aware(
                    created_at, timezone.get_default_timezone()
                )
            created_at = timezone.localtime(created_at)
            item["created_at_display"] = created_at.strftime(
                "%d %b %Y, %I:%M %p"
            ).lstrip("0")
        else:
            item["created_at_display"] = str(
                item.get("created_at") or "Time unavailable"
            )[:80]

    back_query = urlencode({"next": back_url}) if back_url != reverse("home") else ""
    return page(
        request,
        "notifications/list.html",
        items=items,
        only_unread=only_unread,
        back_url=back_url,
        back_url_is_fallback=not bool(back_query),
        back_query=back_query,
        next_offset=offset + 20 if offset + 20 < total else None,
        previous_offset=max(offset - 20, 0) if offset > 0 else None,
    )


@api_guard
def promotion_checkout(request, pk):
    """Show the authoritative offer and accept terms before queuing provider initialization."""
    client = APIClient(request)
    application_path = replace_path("/promotions/applications/{pk}/", pk=pk)
    offer_path = replace_path("/promotions/applications/{pk}/offer/", pk=pk)
    try:
        _, application, _ = client.get(application_path)
        if not isinstance(application, dict) or application.get("status") != "APPROVED":
            messages.info(
                request,
                "Only an approved application can proceed to a promotion offer.",
            )
            return redirect("promotion-applications")
        _, offer, _ = client.get(offer_path)
    except APIError as exc:
        handle_api_error(request, exc)
        return redirect("my-promotions")

    idempotency_slot = f"promotion_checkout_key:{pk}"
    checkout_idempotency_key = request.session.get(idempotency_slot)
    if not checkout_idempotency_key:
        checkout_idempotency_key = new_idempotency_key("promotion-checkout")
        request.session[idempotency_slot] = checkout_idempotency_key
        request.session.modified = True

    if request.method == "POST":
        payload = {
            "quote_token": request.POST.get("quote_token", ""),
            "accepted_terms": request.POST.get("accepted_terms") == "on",
            "accepted_terms_version": request.POST.get("terms_version", ""),
        }
        try:
            _, checkout, _ = client.post(
                replace_path("/promotions/applications/{pk}/checkout/", pk=pk),
                data=payload,
                idempotency_key=checkout_idempotency_key,
            )
            request.session.pop(idempotency_slot, None)
            request.session.modified = True
            messages.success(
                request,
                "Your offer terms were accepted. Flutterwave payment initialization is processing; payment is not confirmed until the server verifies it.",
            )
            return redirect(
                f"{reverse('my-promotions')}?payment={checkout.get('payment_id', '')}"
            )
        except APIError as exc:
            handle_api_error(request, exc)
            checkout_idempotency_key = new_idempotency_key("promotion-checkout")
            request.session[idempotency_slot] = checkout_idempotency_key
            request.session.modified = True
            try:
                _, offer, _ = client.get(offer_path)
            except APIError:
                return redirect("my-promotions")

    return page(
        request,
        "artisan/promotion_checkout.html",
        role="artisan",
        application=application,
        offer=offer if isinstance(offer, dict) else {},
        checkout_idempotency_key=checkout_idempotency_key,
        page_js_path="",
    )


@api_guard
def my_promotions(request):
    client = APIClient(request)
    try:
        _, campaign_payload, _ = client.get(
            "/promotions/campaigns/", params={"limit": 100, "offset": 0}
        )
        _, application_payload, _ = client.get(
            "/promotions/applications/", params={"limit": 100, "offset": 0}
        )
    except APIError as exc:
        handle_api_error(request, exc)
        return redirect("artisan-profile")
    campaigns = extract_items(campaign_payload)
    applications = extract_items(application_payload)
    return page(
        request,
        "artisan/my_promotions.html",
        role="artisan",
        campaigns=campaigns,
        applications=applications,
        highlighted_payment=str(request.GET.get("payment") or ""),
        page_js_path="",
    )


@api_guard
def promotion_payment_redirect(request, pk):
    """Redirect only to the provider authorization URL stored by the trusted API."""
    try:
        _, payment, _ = APIClient(request).get(
            replace_path("/promotions/payments/{pk}/", pk=pk)
        )
    except APIError as exc:
        handle_api_error(request, exc)
        return redirect("my-promotions")
    authorization_url = (
        str(payment.get("authorization_url") or "") if isinstance(payment, dict) else ""
    )
    parsed = urlparse(authorization_url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme == "https" and (
        host == "flutterwave.com" or host.endswith(".flutterwave.com")
    ):
        return redirect(authorization_url)
    messages.info(
        request,
        "Flutterwave has not returned a payment link yet. Refresh My Promotions shortly; this is not a payment confirmation.",
    )
    return redirect("my-promotions")


@never_cache
@require_POST
@api_guard
def reveal_manager_values(request):
    """Issue a short-lived, one-use session grant for a protected manager view."""
    scope = str(request.POST.get("scope") or "").strip().lower()
    if scope not in {"manager", "promotion", "hide"}:
        return HttpResponseForbidden("This values request is not authorized.")

    allowed_targets = {
        reverse("home"),
        reverse("dashboard"),
        reverse("support-promotion-campaign-management"),
    }
    candidate = safe_internal_next(request, request.POST.get("next"))
    target = urlparse(candidate).path if candidate else ""
    if target not in allowed_targets:
        target = reverse("home")

    if scope == "hide":
        request.session.pop(MANAGER_VALUES_REVEAL_SESSION_KEY, None)
        request.session.modified = True
        return redirect(target)

    capabilities = _support_capabilities(APIClient(request))
    gate_target = _staff_security_gate_target(request)
    if gate_target:
        return redirect(gate_target)
    is_manager = bool(capabilities.get("is_superuser"))
    can_review_promotions = bool(capabilities.get("can_review_promotions"))
    if scope == "manager" and not is_manager:
        return HttpResponseForbidden("This values request is not authorized.")
    if scope == "promotion" and not (is_manager or can_review_promotions):
        return HttpResponseForbidden("This values request is not authorized.")
    request.session[MANAGER_VALUES_REVEAL_SESSION_KEY] = {
        "scope": scope,
        "target": target,
        "expires_at": timezone.now().timestamp() + MANAGER_VALUES_REVEAL_TTL_SECONDS,
    }
    request.session.modified = True
    return redirect(target)


@api_guard
def support_promotion_campaign_management(request):
    client = APIClient(request)
    capabilities = _support_capabilities(client)
    is_manager = bool(capabilities.get("is_superuser"))
    can_review = bool(capabilities.get("can_review_promotions"))
    if not is_manager and not can_review:
        messages.error(
            request,
            "This promotion management area is restricted to authorized managers and Customer Support reviewers.",
        )
        return redirect("support-operations")

    if request.method == "POST":
        action_kind = str(request.POST.get("action") or "").strip()
        idempotency_key = str(
            request.POST.get("idempotency_key") or ""
        ).strip() or new_idempotency_key("promotion-management")
        try:
            if action_kind == "plan":
                if not is_manager:
                    messages.error(
                        request,
                        "Only an authorized platform manager can change promotion plans.",
                    )
                    return redirect("support-promotion-campaign-management")
                plan_id = str(request.POST.get("plan_id") or "").strip()
                data = {
                    "name": str(request.POST.get("name") or "").strip(),
                    "price": str(request.POST.get("price") or "").strip(),
                    "duration_days": str(
                        request.POST.get("duration_days") or ""
                    ).strip(),
                    "is_enabled": request.POST.get("is_enabled") == "on",
                    "refund_policy": str(
                        request.POST.get("refund_policy") or ""
                    ).strip(),
                    "terms_text": str(request.POST.get("terms_text") or "").strip(),
                    "reason": str(request.POST.get("reason") or "").strip(),
                }
                client.patch(
                    replace_path("/promotions/management/plans/{pk}/", pk=plan_id),
                    data=data,
                    idempotency_key=idempotency_key,
                )
                messages.success(
                    request,
                    "Promotion plan settings were saved and audited. Existing campaign snapshots remain unchanged.",
                )
            elif action_kind == "capacity":
                if not is_manager:
                    messages.error(
                        request,
                        "Only an authorized platform manager can change target capacity.",
                    )
                    return redirect("support-promotion-campaign-management")
                inventory_id = str(request.POST.get("inventory_id") or "").strip()
                client.patch(
                    replace_path(
                        "/promotions/management/inventories/{pk}/capacity/",
                        pk=inventory_id,
                    ),
                    data={
                        "capacity": str(request.POST.get("capacity") or "").strip(),
                        "reason": str(request.POST.get("reason") or "").strip(),
                    },
                    idempotency_key=idempotency_key,
                )
                messages.success(
                    request,
                    "Inventory capacity was saved and audited. The paid schedule is being recalculated.",
                )
            elif action_kind == "campaign":
                if not can_review:
                    messages.error(
                        request, "Your role cannot perform campaign actions."
                    )
                    return redirect("support-promotion-campaign-management")
                campaign_id = str(request.POST.get("campaign_id") or "").strip()
                data = {
                    "action": str(request.POST.get("campaign_action") or "")
                    .strip()
                    .upper(),
                    "support_case_id": str(
                        request.POST.get("support_case_id") or ""
                    ).strip(),
                    "reason": str(request.POST.get("reason") or "").strip(),
                    "evidence_summary": str(
                        request.POST.get("evidence_summary") or ""
                    ).strip(),
                    "confirmed_violation": request.POST.get("confirmed_violation")
                    == "on",
                }
                client.post(
                    replace_path(
                        "/promotions/review/campaigns/{pk}/actions/", pk=campaign_id
                    ),
                    data=data,
                    idempotency_key=idempotency_key,
                )
                messages.success(
                    request,
                    "Campaign action recorded with its linked support case. The artisan was notified; a complaint by itself is not a confirmed violation.",
                )
            else:
                messages.error(request, "Choose a valid promotion management action.")
                return redirect("support-promotion-campaign-management")
            return redirect("support-promotion-campaign-management")
        except APIError as exc:
            handle_api_error(request, exc)

    status_filter = str(request.GET.get("status") or "").upper()
    type_filter = str(request.GET.get("promotion_type") or "").upper()
    campaigns_params = {"limit": 100, "offset": 0}
    if status_filter:
        campaigns_params["status"] = status_filter
    if type_filter:
        campaigns_params["promotion_type"] = type_filter
    try:
        _, campaign_payload, _ = client.get(
            "/promotions/management/campaigns/", params=campaigns_params
        )
    except APIError as exc:
        handle_api_error(request, exc)
        campaign_payload = []
    campaigns = extract_items(campaign_payload)
    for campaign in campaigns:
        campaign["open_support_cases"] = [
            case
            for case in campaign.get("support_cases", [])
            if case.get("status") in {"OPEN", "UNDER_REVIEW"}
        ]
        campaign["management_idempotency_key"] = new_idempotency_key(
            "promotion-campaign-action"
        )
        for field in (
            "payment_verified_at",
            "scheduled_start_at",
            "started_at",
            "expires_at",
            "paused_at",
            "created_at",
        ):
            if isinstance(campaign.get(field), str):
                campaign[field] = parse_datetime(campaign[field]) or campaign[field]
        for event in campaign.get("actions", []) or []:
            if isinstance(event.get("created_at"), str):
                event["created_at"] = (
                    parse_datetime(event["created_at"]) or event["created_at"]
                )
        for case in campaign.get("support_cases", []) or []:
            if isinstance(case.get("created_at"), str):
                case["created_at"] = (
                    parse_datetime(case["created_at"]) or case["created_at"]
                )

    reveal_scope = _consume_manager_values_reveal(request)
    promotion_values_revealed = bool(reveal_scope == "promotion" and can_review)
    manager_values_revealed = bool(promotion_values_revealed and is_manager)
    plans, inventories, promotion_audit_events = [], [], []
    if manager_values_revealed:
        try:
            _, plan_payload, _ = client.get("/promotions/management/plans/")
            plans = extract_items(plan_payload)
            for plan in plans:
                plan["management_idempotency_key"] = new_idempotency_key(
                    "promotion-plan-update"
                )
                if isinstance(plan.get("updated_at"), str):
                    plan["updated_at"] = (
                        parse_datetime(plan["updated_at"]) or plan["updated_at"]
                    )
        except APIError as exc:
            handle_api_error(request, exc)
        try:
            _, inventory_payload, _ = client.get(
                "/promotions/management/inventories/",
                params={"limit": 1000, "offset": 0},
            )
            inventories = extract_items(inventory_payload)
            for inventory in inventories:
                inventory["management_idempotency_key"] = new_idempotency_key(
                    "promotion-inventory-capacity"
                )
                if isinstance(inventory.get("updated_at"), str):
                    inventory["updated_at"] = (
                        parse_datetime(inventory["updated_at"])
                        or inventory["updated_at"]
                    )
        except APIError as exc:
            handle_api_error(request, exc)
        try:
            _, audit_payload, _ = client.get("/promotions/management/audit-events/")
            promotion_audit_events = extract_items(audit_payload)
            for event in promotion_audit_events:
                if isinstance(event.get("created_at"), str):
                    event["created_at"] = (
                        parse_datetime(event["created_at"]) or event["created_at"]
                    )
        except APIError as exc:
            handle_api_error(request, exc)

    roles = set(capabilities.get("roles") or [])
    can_supervise = is_manager or bool(
        roles.intersection({"CUSTOMER_SUPPORT_SUPERVISOR", "CUSTOMER_SUPPORT_ADMIN"})
    )
    response = page(
        request,
        "support/promotion_campaign_management.html",
        role="staff",
        support_capabilities=capabilities,
        can_review_promotions=can_review,
        can_manage_promotion_configuration=is_manager,
        promotion_values_revealed=promotion_values_revealed,
        manager_values_revealed=manager_values_revealed,
        reveal_return_path=request.path,
        can_supervise_campaign_actions=can_supervise,
        plans=plans,
        inventories=inventories,
        promotion_audit_events=promotion_audit_events,
        campaigns=campaigns,
        status_filter=status_filter,
        promotion_type_filter=type_filter,
        campaign_status_options=[
            ("PENDING_PAYMENT", "Awaiting payment"),
            ("PAYMENT_REVIEW", "Payment review required"),
            ("QUEUED", "Queued"),
            ("SCHEDULED", "Scheduled"),
            ("ACTIVE", "Active"),
            ("PAUSED", "Paused"),
            ("SUSPENDED", "Suspended"),
            ("EXPIRED", "Expired"),
            ("REVOKED", "Revoked"),
        ],
        page_js_path="",
    )
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    response["Pragma"] = "no-cache"
    response["Expires"] = "0"
    return response
