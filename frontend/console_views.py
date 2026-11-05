"""Server-rendered Manager Console.

The console is a presentation layer. Every figure on these pages comes from
``/api/v1/manager/...``; this module never computes a price, a fee, an audience
size or a permission decision. When the API refuses a call, the page says so
and shows nothing rather than inventing a number.
"""
from __future__ import annotations

from urllib.parse import urlencode

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST

from .api import APIError, APIClient, extract_items
from .views import api_guard, handle_api_error, page

PERIOD_CHOICES = [
    ("today", "Today"),
    ("yesterday", "Yesterday"),
    ("last_7_days", "Last 7 days"),
    ("last_30_days", "Last 30 days"),
    ("last_90_days", "Last 90 days"),
    ("last_12_months", "Last 12 months"),
    ("this_month", "This month"),
    ("last_month", "Last month"),
    ("this_quarter", "This quarter"),
    ("last_quarter", "Last quarter"),
    ("this_year", "This year"),
    ("last_year", "Last year"),
    ("custom", "Custom range"),
]

FILTER_FIELDS = ("category", "service", "location", "payment_status", "booking_status", "segment", "start", "end")

SECTION_TITLES = {
    "overview": ("Management overview", "Volume, money and quality across the whole marketplace for the selected range."),
    "financial": ("Financial intelligence", "Money collected, held, owed and earned — every figure carries its definition."),
    "business": ("Business intelligence", "Growth against the previous period, unit economics and customer quality."),
    "services": ("Service intelligence", "Demand, supply, conversion and disputes by service category."),
    "customers": ("Customer intelligence", "Acquisition, activity, spend and cohort retention."),
    "artisans": ("Artisan intelligence", "Supply, response speed and per-artisan performance."),
    "funnel": ("Request and booking funnel", "Where a request stops moving, and how long each step takes."),
    "marketing": ("Marketing intelligence", "Campaign delivery, engagement and attributed outcomes."),
    "geography": ("Geographic intelligence", "Artisan supply against customer demand by state."),
    "insights": ("Management insights", "Rule-based observations with the figures and method behind each one."),
    "data-quality": ("Data quality", "Records that would distort a decision if left unchecked."),
    "support": ("Support and disputes", "Ticket load, resolution time and money in dispute."),
    "risk": ("Risk monitoring", "Signals that need a human decision — nothing is suspended automatically."),
    "reports": ("Reports and data explorer", "Build a report from a fixed catalogue, save it, or queue an export."),
    "campaigns": ("Campaign board", "Create, get a second approval, and dispatch in bounded batches."),
    "staff": ("Staff and roles", "Who has access, with what role and what second factor."),
    "configuration": ("Configuration centre", "Versioned platform rules with preview, approval and rollback."),
    "jobs": ("Jobs and snapshots", "Background work, queue depth and the snapshot schedule."),
    "system-health": ("System health", "Availability, latency percentiles and provider status."),
    "audit": ("Audit and activity", "Every recorded management action, newest first."),
    "privacy": ("Privacy and retention", "Data classes, retention windows and data subject requests."),
    "step-up": ("Confirm your identity", "A sensitive action needs your password and second factor."),
}

NAV = [
    ("Overview", "console-overview", "overview"),
    ("Financial", "console-financial", "analytics.financial"),
    ("Business", "console-business", "analytics.view"),
    ("Service", "console-services", "analytics.view"),
    ("Customer", "console-customers", "analytics.customer"),
    ("Artisan", "console-artisans", "analytics.artisan"),
    ("Funnel", "console-funnel", "analytics.view"),
    ("Marketing", "console-marketing", "analytics.view"),
    ("Geographic", "console-geography", "analytics.view"),
    ("Insights", "console-insights", "analytics.view"),
    ("Data quality", "console-data-quality", "analytics.view"),
    ("Support & disputes", "console-support", "support.view"),
    ("Risk", "console-risk", "risk.view"),
    ("Reports", "console-reports", "analytics.build"),
    ("Campaigns", "console-campaigns", "campaign.view"),
    ("Staff", "console-staff", "staff.view"),
    ("Configuration", "console-config", "config.view"),
    ("Privacy", "console-privacy", "privacy.view"),
    ("System health", "console-system-health", "system.health"),
    ("Audit", "console-audit", "audit.view"),
    ("Jobs", "console-jobs", "analytics.view"),
]

STEP_UP_SESSION_KEY = "manager_step_up_tokens"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _period_params(request):
    """Whitelisted filters, so an unexpected query string cannot reach the API."""
    params = {"period": request.GET.get("period") or "last_30_days"}
    if params["period"] not in {key for key, _label in PERIOD_CHOICES}:
        params["period"] = "last_30_days"
    for field in FILTER_FIELDS:
        value = (request.GET.get(field) or "").strip()
        if value:
            params[field] = value[:80]
    if params["period"] == "custom":
        params.setdefault("start", request.GET.get("start") or "")
        params.setdefault("end", request.GET.get("end") or "")
    return params


def _session_payload(client):
    try:
        _, payload, _ = client.get("/manager/session/")
        return payload if isinstance(payload, dict) else None
    except APIError:
        return None


def _nav_for(session):
    granted = set((session or {}).get("permissions") or [])
    return [
        {"label": label, "url_name": url_name, "permission": permission}
        for label, url_name, permission in NAV
        if permission in granted or "super-admin" in ((session or {}).get("role") or {}).get("slug", "")
    ]


def _console_page(request, template, section, client, session, payload, **extra):
    title, subtitle = SECTION_TITLES.get(section, ("Manager Console", ""))
    context = {
        "console_title": title,
        "console_subtitle": subtitle,
        "role": "staff",
        "nav_role": "staff",
        "console_section": section,
        "console_session": session,
        "console_nav": _nav_for(session),
        "payload": payload,
        "console_period": (payload.get("range") or {}).get("key") or request.GET.get("period") or "last_30_days",
        "console_range": payload.get("range") or {},
        "period_choices": PERIOD_CHOICES,
        "console_query": request.GET.urlencode(),
        # The console ships one shared script (in base_console.html); the
        # per-template default would point at a file that does not exist.
        "page_js_path": "",
        "show_console_filters": section in {
            "overview", "financial", "business", "services", "customers", "artisans",
            "funnel", "marketing", "geography", "insights", "data-quality", "support", "risk",
        },
        "freshness": payload.get("data_freshness") or {},
        # The console is staff tooling, not a shopper page: the marketplace
        # payment-notice dialog and the onboarding tour do not belong on it.
        "payment_notice_auto_offer": False,
        "tour_auto_offer": False,
    }
    context.update(extra)
    response = page(request, template, **context)
    if hasattr(response, "headers"):
        response["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
        response["Pragma"] = "no-cache"
    return response


def _rows(payload, key="results"):
    """Read a named list out of an API payload without inventing one."""
    value = (payload or {}).get(key)
    return value if isinstance(value, list) else []


def _fetch(request, client, path, params=None):
    try:
        _, payload, _ = client.get(path, params=params or {})
        return payload if isinstance(payload, dict) else {}, None
    except APIError as exc:
        handle_api_error(request, exc)
        return {}, exc


def _step_up_token(request, scope):
    tokens = request.session.get(STEP_UP_SESSION_KEY) or {}
    return tokens.get(scope, "")


def _remember_step_up(request, scope, token):
    tokens = dict(request.session.get(STEP_UP_SESSION_KEY) or {})
    tokens[scope] = token
    request.session[STEP_UP_SESSION_KEY] = tokens
    request.session.modified = True


def _clear_step_up(request, scope):
    tokens = dict(request.session.get(STEP_UP_SESSION_KEY) or {})
    tokens.pop(scope, None)
    request.session[STEP_UP_SESSION_KEY] = tokens
    request.session.modified = True


PENDING_ACTION_KEY = "console_pending_action"


def _post(request, client, path, data, *, scope="", success="", back="console-overview"):
    """POST to the API, and make a refused step-up a resumable detour.

    A sensitive action needs a single-use grant. Rather than sending the manager
    back to a form and asking them to type it all again (or, worse, redirecting to
    a POST-only URL), the request is parked in the session and replayed the moment
    verification succeeds.
    """
    back_url = reverse(back)
    headers = {}
    if scope:
        token = _step_up_token(request, scope)
        if not token:
            _park_pending_action(request, scope=scope, path=path, data=data,
                                 success=success, back_url=back_url)
            messages.info(
                request,
                "Confirm your password and second factor. Your change will be submitted "
                "as soon as verification succeeds.",
            )
            return redirect(f"{reverse('console-step-up')}?{urlencode({'scope': scope, 'next': back_url})}")
        headers["X-Step-Up-Token"] = token

    try:
        _status, payload, _headers = client.post(path, data=data, headers=headers or None)
    except APIError as exc:
        if exc.status_code in (401, 403) and scope:
            _clear_step_up(request, scope)
        handle_api_error(request, exc)
        return redirect(back_url)
    if scope:
        _clear_step_up(request, scope)
    if success:
        detail = payload.get("detail") if isinstance(payload, dict) else ""
        messages.success(request, f"{success}{(' ' + str(detail)) if detail else ''}".strip())
    return redirect(back_url)


def _park_pending_action(request, *, scope, path, data, success, back_url):
    request.session[PENDING_ACTION_KEY] = {
        "scope": scope,
        "path": path,
        "data": _json_safe(data),
        "success": success,
        "back": back_url,
    }
    request.session.modified = True


def _json_safe(value):
    """Session storage is JSON: turn form values into plain lists and strings."""
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return "" if value is None else str(value)


def resume_pending_action(request, client, scope):
    """Replay the action that was interrupted by a step-up, if there is one.

    Returns the message to show, or ``None`` when nothing was waiting.
    """
    pending = request.session.pop(PENDING_ACTION_KEY, None)
    if not pending or pending.get("scope") != scope:
        return None
    token = _step_up_token(request, scope)
    headers = {"X-Step-Up-Token": token} if token else {}
    try:
        _status, payload, _headers = client.post(pending["path"], data=pending.get("data") or {},
                                                 headers=headers or None)
    except APIError as exc:
        _clear_step_up(request, scope)
        handle_api_error(request, exc)
        return ""
    _clear_step_up(request, scope)
    detail = payload.get("detail") if isinstance(payload, dict) else ""
    return f"{pending.get('success') or 'Done.'}{(' ' + str(detail)) if detail else ''}".strip()


def _guard(session, request):
    """Managers only. A customer or artisan is sent back to their own dashboard."""
    if session is None or not session.get("is_manager"):
        messages.error(request, "The Manager Console is restricted to active managers with a second factor enrolled.")
        return redirect("dashboard")
    return None


def _analytics_view(request, template, section, path, extra_builder=None):
    client = APIClient(request)
    session = _session_payload(client)
    blocked = _guard(session, request)
    if blocked:
        return blocked
    params = _period_params(request)
    payload = _fetch(request, client, path, params)[0]
    extra = extra_builder(client) if extra_builder else {}
    return _console_page(request, template, section, client, session, payload, console_filters=params, **extra)


# --------------------------------------------------------------------------- #
# Analytics pages
# --------------------------------------------------------------------------- #
@api_guard
def overview(request):
    return _analytics_view(request, "console/overview.html", "overview", "/manager/analytics/overview/")


@api_guard
def financial(request):
    return _analytics_view(request, "console/financial.html", "financial", "/manager/financial/")


@api_guard
def business(request):
    return _analytics_view(request, "console/business.html", "business", "/manager/business/")


@api_guard
def services(request):
    return _analytics_view(request, "console/services.html", "services", "/manager/services/")


@api_guard
def customers(request):
    return _analytics_view(request, "console/customers.html", "customers", "/manager/customers/")


@api_guard
def artisans(request):
    return _analytics_view(request, "console/artisans.html", "artisans", "/manager/artisans/")


@api_guard
def funnel(request):
    return _analytics_view(request, "console/funnel.html", "funnel", "/manager/funnel/")


@api_guard
def marketing(request):
    return _analytics_view(request, "console/marketing.html", "marketing", "/manager/marketing/")


@api_guard
def geography(request):
    return _analytics_view(request, "console/geography.html", "geography", "/manager/geography/")


@api_guard
def insights(request):
    return _analytics_view(request, "console/insights.html", "insights", "/manager/insights/")


@api_guard
def data_quality(request):
    return _analytics_view(request, "console/data_quality.html", "data-quality", "/manager/data-quality/")


@api_guard
def support_intelligence(request):
    return _analytics_view(request, "console/support.html", "support", "/manager/support/")


@api_guard
def risk(request):
    client = APIClient(request)
    session = _session_payload(client)
    blocked = _guard(session, request)
    if blocked:
        return blocked
    params = _period_params(request)
    payload = _fetch(request, client, "/manager/risk/", params)[0]
    reviews = _fetch(request, client, "/manager/risk/reviews/")[0]
    return _console_page(request, "console/risk.html", "risk", client, session, payload,
                         console_filters=params, reviews=extract_items(reviews))


# --------------------------------------------------------------------------- #
# Reports, explorer, exports
# --------------------------------------------------------------------------- #
@api_guard
def reports(request):
    client = APIClient(request)
    session = _session_payload(client)
    blocked = _guard(session, request)
    if blocked:
        return blocked
    catalogue = _fetch(request, client, "/manager/explorer/catalogue/")[0]
    result, error = None, None
    domain = (request.GET.get("domain") or "").strip()
    spec = {}
    if domain:
        spec = {
            "domain": domain,
            "measure": (request.GET.get("measure") or "count").strip(),
            "dimension": (request.GET.get("dimension") or "").strip(),
            "range": (request.GET.get("range") or "last_30_days").strip(),
        }
        try:
            _, result, _ = client.post("/manager/explorer/run/", data=spec)
        except APIError as exc:
            error = exc.user_message
            handle_api_error(request, exc)
    saved = _fetch(request, client, "/manager/reports/")[0]
    jobs_payload = _fetch(request, client, "/manager/jobs/")[0]
    export_jobs = [row for row in extract_items(jobs_payload) if row.get("kind") == "EXPORT"]
    return _console_page(request, "console/reports.html", "reports", client, session, {},
                         catalogue=catalogue, result=result, error=error, spec=spec,
                         saved_reports=extract_items(saved), export_jobs=export_jobs,
                         period_choices=PERIOD_CHOICES)


@api_guard
@require_POST
def report_save(request):
    client = APIClient(request)
    data = {
        "name": (request.POST.get("name") or "").strip(),
        "description": (request.POST.get("description") or "").strip(),
        "is_shared": request.POST.get("is_shared") == "on",
        "spec": {
            "domain": (request.POST.get("domain") or "").strip(),
            "measure": (request.POST.get("measure") or "count").strip(),
            "dimension": (request.POST.get("dimension") or "").strip(),
            "range": (request.POST.get("range") or "last_30_days").strip(),
        },
    }
    return _post(request, client, "/manager/reports/", data,
                 success="Report saved.", back="console-reports")


@api_guard
@require_POST
def report_run(request, report_id):
    client = APIClient(request)
    try:
        _, payload, _ = client.post(f"/manager/reports/{report_id}/", data={})
    except APIError as exc:
        handle_api_error(request, exc)
        return redirect("console-reports")
    return render(request, "console/_report_result.html", {"result": payload})


@api_guard
@require_POST
def report_delete(request, report_id):
    client = APIClient(request)
    try:
        client.delete(f"/manager/reports/{report_id}/")
        messages.success(request, "Report deleted.")
    except APIError as exc:
        handle_api_error(request, exc)
    return redirect("console-reports")


@api_guard
@require_POST
def export_request(request):
    client = APIClient(request)
    data = {
        "spec": {
            "domain": (request.POST.get("domain") or "bookings").strip(),
            "measure": (request.POST.get("measure") or "count").strip(),
            "dimension": (request.POST.get("dimension") or "").strip(),
            "range": (request.POST.get("range") or "last_30_days").strip(),
        },
        "format": (request.POST.get("format") or "csv").strip(),
        "sensitive": request.POST.get("sensitive") == "on",
    }
    scope = "export:sensitive" if data["sensitive"] else ""
    return _post(request, client, "/manager/exports/", data, scope=scope,
                 success="Export queued. It appears in Exports below when the worker finishes.",
                 back="console-reports")


@api_guard
def export_download(request, job_id):
    client = APIClient(request)
    try:
        body, content_type, headers = client.get_file(f"/manager/exports/{job_id}/download/")
    except APIError as exc:
        handle_api_error(request, exc)
        return redirect("console-reports")
    response = HttpResponse(body, content_type=content_type or "application/octet-stream")
    disposition = headers.get("Content-Disposition") if hasattr(headers, "get") else ""
    response["Content-Disposition"] = disposition or "attachment; filename=\"manager-export\""
    return response


# --------------------------------------------------------------------------- #
# Campaign board
# --------------------------------------------------------------------------- #
@api_guard
def campaigns(request):
    client = APIClient(request)
    session = _session_payload(client)
    blocked = _guard(session, request)
    if blocked:
        return blocked
    payload = _fetch(request, client, "/manager/campaigns/")[0]
    estimate = request.session.pop("console_campaign_estimate", None)
    return _console_page(request, "console/campaigns.html", "campaigns", client, session, payload,
                         campaigns=extract_items(payload), estimate=estimate)


@api_guard
@require_POST
def campaign_create(request):
    client = APIClient(request)
    channels = request.POST.getlist("channels") or ["IN_APP"]
    data = {
        "title": (request.POST.get("title") or "").strip(),
        "subject": (request.POST.get("subject") or "").strip(),
        "body": (request.POST.get("body") or "").strip(),
        "cta_text": (request.POST.get("cta_text") or "").strip(),
        "cta_url": (request.POST.get("cta_url") or "").strip(),
        "channels": channels,
        "segment": (request.POST.get("segment") or "all_customers").strip(),
        "scheduled_for": (request.POST.get("scheduled_for") or "").strip() or None,
    }
    return _post(request, client, "/manager/campaigns/", data,
                 success="Campaign created. It needs a second manager's approval before it can be sent.",
                 back="console-campaigns")


@api_guard
@require_POST
def campaign_estimate(request):
    client = APIClient(request)
    data = {
        "segment": (request.POST.get("segment") or "all_customers").strip(),
        "channels": request.POST.getlist("channels") or ["IN_APP"],
    }
    try:
        _, payload, _ = client.post("/manager/campaigns/estimate/", data=data)
        request.session["console_campaign_estimate"] = payload
    except APIError as exc:
        handle_api_error(request, exc)
    return redirect("console-campaigns")


@api_guard
@require_POST
def campaign_approve(request, campaign_id):
    return _post(request, APIClient(request), f"/manager/campaigns/{campaign_id}/approve/", {},
                 success="Campaign approved. Dispatch is still a separate action.", back="console-campaigns")


@api_guard
@require_POST
def campaign_dispatch(request, campaign_id):
    return _post(request, APIClient(request), f"/manager/campaigns/{campaign_id}/dispatch/", {},
                 success="Dispatch queued on the campaigns queue.", back="console-campaigns")


@api_guard
@require_POST
def campaign_cancel(request, campaign_id):
    return _post(request, APIClient(request), f"/manager/campaigns/{campaign_id}/cancel/", {},
                 success="Campaign cancelled.", back="console-campaigns")


# --------------------------------------------------------------------------- #
# Staff and roles
# --------------------------------------------------------------------------- #
@api_guard
def staff(request):
    client = APIClient(request)
    session = _session_payload(client)
    blocked = _guard(session, request)
    if blocked:
        return blocked
    payload = _fetch(request, client, "/manager/staff/")[0]
    roles_payload = _fetch(request, client, "/manager/staff/roles/")[0]
    # The API names these lists "staff" and "roles"; extract_items() looks for
    # "results" and would quietly render an empty page.
    return _console_page(request, "console/staff.html", "staff", client, session, payload,
                         staff=_rows(payload, "staff"), roles=_rows(payload, "roles"),
                         system_roles=_rows(roles_payload, "system_roles"),
                         permission_catalogue=payload.get("permission_catalogue") or [],
                         can_manage=bool(payload.get("can_manage")))


@api_guard
@require_POST
def staff_role(request, profile_id):
    data = {"role": (request.POST.get("role") or "").strip()}
    return _post(request, APIClient(request), f"/manager/staff/{profile_id}/role/", data,
                 scope="staff:manage", success="Role updated.", back="console-staff")


@api_guard
@require_POST
def staff_suspend(request, profile_id):
    action = "restore" if request.POST.get("action") == "restore" else "suspend"
    data = {"action": action, "reason": (request.POST.get("reason") or "").strip()}
    return _post(request, APIClient(request), f"/manager/staff/{profile_id}/suspend/", data,
                 success=f"Account {action}d.", back="console-staff")


@api_guard
@require_POST
def role_permissions(request, slug):
    data = {"permissions": request.POST.getlist("permissions")}
    return _post(request, APIClient(request), f"/manager/staff/roles/{slug}/permissions/", data,
                 scope="staff:manage", success="Role permissions saved.", back="console-staff")


# --------------------------------------------------------------------------- #
# Configuration centre
# --------------------------------------------------------------------------- #
@api_guard
def config_centre(request):
    client = APIClient(request)
    session = _session_payload(client)
    blocked = _guard(session, request)
    if blocked:
        return blocked
    payload = _fetch(request, client, "/manager/config/")[0]
    history_key = (request.GET.get("key") or "").strip()
    history = {}
    if history_key:
        history = _fetch(request, client, f"/manager/config/{history_key}/history/")[0]
    preview = request.session.pop("console_config_preview", None)
    return _console_page(request, "console/config.html", "configuration", client, session, payload,
                         settings_rows=_rows(payload, "settings"),
                         pending_changes=_rows(payload, "pending_changes"),
                         history=history, history_key=history_key, preview=preview,
                         step_up_scopes=session.get("step_up_scopes") or {})


@api_guard
@require_POST
def config_preview(request):
    client = APIClient(request)
    key = (request.POST.get("key") or "").strip()
    value = request.POST.get("value") or ""
    try:
        _, payload, _ = client.post(f"/manager/config/{key}/preview/", data={"value": value})
        request.session["console_config_preview"] = {"key": key, "payload": payload}
    except APIError as exc:
        handle_api_error(request, exc)
    return redirect(f"{reverse('console-config')}?{urlencode({'key': key})}")


@api_guard
@require_POST
def config_change(request):
    """Propose a new value.

    A ``sensitive``-category setting needs a ``config:finance`` step-up token; the
    API decides that, but asking here first keeps the manager from filling in a
    form that is certain to be refused.
    """
    client = APIClient(request)
    key = (request.POST.get("key") or "").strip()
    category = (request.POST.get("category") or "").strip()
    data = {
        "value": request.POST.get("value") or "",
        "reason": (request.POST.get("reason") or "").strip(),
    }
    scope = "config:finance" if category == "sensitive" else ""
    return _post(request, client, f"/manager/config/{key}/change/", data, scope=scope,
                 success="Change recorded. It takes effect once approved.",
                 back="console-config")


@api_guard
@require_POST
def config_rollback(request):
    key = (request.POST.get("key") or "").strip()
    version = (request.POST.get("version") or "").strip()
    data = {"version": version, "reason": (request.POST.get("reason") or "").strip()}
    return _post(request, APIClient(request), f"/manager/config/{key}/rollback/", data,
                 scope="config:finance", success="Rollback requested as a new version.", back="console-config")


@api_guard
@require_POST
def config_approve(request, change_id):
    return _post(request, APIClient(request), f"/manager/config-changes/{change_id}/approve/", {},
                 scope="config:approve", success="Change approved and applied.", back="console-config")


@api_guard
@require_POST
def config_reject(request, change_id):
    data = {"reason": (request.POST.get("reason") or "").strip()}
    return _post(request, APIClient(request), f"/manager/config-changes/{change_id}/reject/", data,
                 success="Change rejected.", back="console-config")


# --------------------------------------------------------------------------- #
# Snapshots, jobs, health, audit
# --------------------------------------------------------------------------- #
@api_guard
def jobs(request):
    client = APIClient(request)
    session = _session_payload(client)
    blocked = _guard(session, request)
    if blocked:
        return blocked
    payload = _fetch(request, client, "/manager/jobs/")[0]
    snapshots_payload = _fetch(request, client, "/manager/snapshots/")[0]
    return _console_page(request, "console/jobs.html", "jobs", client, session, payload,
                         jobs=extract_items(payload), snapshots=snapshots_payload)


@api_guard
@require_POST
def job_cancel(request, job_id):
    return _post(request, APIClient(request), f"/manager/jobs/{job_id}/cancel/", {},
                 success="Job cancelled.", back="console-jobs")


@api_guard
@require_POST
def snapshot_interval(request):
    seconds = (request.POST.get("interval") or "").strip()
    data = {"interval_seconds": seconds, "reason": (request.POST.get("reason") or "").strip()}
    return _post(request, APIClient(request), "/manager/snapshots/interval/", data,
                 scope="system:manage", success="Snapshot interval updated.", back="console-jobs")


@api_guard
@require_POST
def snapshot_capture(request):
    return _post(request, APIClient(request), "/manager/snapshots/capture/", {},
                 scope="system:manage", success="Snapshot captured.", back="console-jobs")


@api_guard
def system_health(request):
    client = APIClient(request)
    session = _session_payload(client)
    blocked = _guard(session, request)
    if blocked:
        return blocked
    payload = _fetch(request, client, "/manager/health/")[0]
    return _console_page(request, "console/system_health.html", "system-health", client, session, payload)


@api_guard
def audit(request):
    client = APIClient(request)
    session = _session_payload(client)
    blocked = _guard(session, request)
    if blocked:
        return blocked
    params = {"page": request.GET.get("page") or 1, "size": request.GET.get("size") or 50}
    if request.GET.get("action"):
        params["action"] = request.GET["action"]
    payload = _fetch(request, client, "/manager/audit/", params)[0]
    return _console_page(request, "console/audit.html", "audit", client, session, payload,
                         rows=extract_items(payload), filters=_rows(payload, "filters"),
                         audit_action=request.GET.get("action") or "")


# --------------------------------------------------------------------------- #
# Privacy, risk actions, insights, step-up, sessions
# --------------------------------------------------------------------------- #
@api_guard
def privacy(request):
    client = APIClient(request)
    session = _session_payload(client)
    blocked = _guard(session, request)
    if blocked:
        return blocked
    payload = _fetch(request, client, "/manager/privacy/")[0]
    requests_payload = _fetch(request, client, "/manager/privacy/requests/")[0]
    return _console_page(request, "console/privacy.html", "privacy", client, session, payload,
                         retention=_rows(payload, "retention"),
                         data_classes=_rows(payload, "data_classes"),
                         privacy_requests=extract_items(requests_payload))


@api_guard
@require_POST
def privacy_retention(request):
    data_class = (request.POST.get("data_class") or "").strip()
    if not data_class:
        messages.error(request, "Choose a data class before saving a retention policy.")
        return redirect("console-privacy")
    data = {"days": request.POST.get("days") or 0, "enabled": request.POST.get("enabled") == "on"}
    return _post(request, APIClient(request), f"/manager/privacy/retention/{data_class}/", data,
                 scope="privacy:manage", success="Retention policy updated.", back="console-privacy")


@api_guard
@require_POST
def privacy_request(request):
    data = {
        "user_id": (request.POST.get("user_id") or "").strip(),
        "type": (request.POST.get("type") or "ACCESS").strip(),
        "details": (request.POST.get("details") or "").strip(),
    }
    return _post(request, APIClient(request), "/manager/privacy/requests/", data,
                 success="Data subject request logged.", back="console-privacy")


@api_guard
@require_POST
def privacy_request_complete(request, reference):
    data = {"status": (request.POST.get("status") or "COMPLETED").strip(),
            "notes": (request.POST.get("notes") or "").strip()}
    return _post(request, APIClient(request), f"/manager/privacy/requests/{reference}/", data,
                 scope="privacy:manage", success="Data subject request updated.", back="console-privacy")


@api_guard
@require_POST
def risk_decision(request, review_id):
    data = {"status": (request.POST.get("status") or "").strip(),
            "notes": (request.POST.get("notes") or "").strip()}
    return _post(request, APIClient(request), f"/manager/risk/reviews/{review_id}/", data,
                 success="Review decision recorded.", back="console-risk")


@api_guard
@require_POST
def insight_action(request):
    data = {"action": (request.POST.get("action") or "").strip(),
            "key": (request.POST.get("key") or "").strip()}
    return _post(request, APIClient(request), "/manager/insights/action/", data,
                 success="Insight marked.", back="console-insights")


@api_guard
def step_up(request):
    """Request an email code, then exchange it for a scoped, single-use grant."""
    client = APIClient(request)
    session = _session_payload(client)
    blocked = _guard(session, request)
    if blocked:
        return blocked
    scope = (request.GET.get("scope") or request.POST.get("scope") or "").strip()
    next_url = (request.GET.get("next") or request.POST.get("next") or "").strip()
    scopes = session.get("step_up_scopes") or {}
    if scope not in scopes:
        messages.error(request, "Unknown verification scope.")
        return redirect("console-overview")
    if not next_url.startswith("/console/") and not next_url.startswith("/"):
        next_url = "/console/"

    if request.method == "POST":
        action = request.POST.get("action") or "verify"
        if action == "request_code":
            try:
                client.post("/manager/auth/step-up/challenge/", data={})
                messages.info(request, "A verification code was emailed to your account address.")
            except APIError as exc:
                handle_api_error(request, exc)
            return redirect(f"{reverse('console-step-up')}?{urlencode({'scope': scope, 'next': next_url})}")
        password = request.POST.get("password") or ""
        code = (request.POST.get("code") or "").strip()
        try:
            _, grant, _ = client.post("/manager/auth/step-up/",
                                      data={"scope": scope, "password": password, "code": code})
            _remember_step_up(request, scope, grant.get("token", ""))
            resumed = resume_pending_action(request, client, scope)
            if resumed is None:
                messages.success(request, "Verified. You can finish the action now.")
            elif resumed:
                messages.success(request, resumed)
            return redirect(next_url)
        except APIError as exc:
            handle_api_error(request, exc)
            return redirect(f"{reverse('console-step-up')}?{urlencode({'scope': scope, 'next': next_url})}")

    return _console_page(request, "console/step_up.html", "step-up", client, session, {},
                         scope=scope, scope_label=scopes.get(scope, scope), next_url=next_url,
                         totp_enrolled=bool(session.get("totp_enrolled")))


@api_guard
@require_POST
def session_revoke(request, session_id):
    return _post(request, APIClient(request), f"/manager/sessions/{session_id}/revoke/", {},
                 success="Session revoked.", back="console-overview")
