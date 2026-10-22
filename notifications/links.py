"""Deep links for notifications.

Every link is derived from a named frontend route, never from user input, so an in-app notification or an
email can only point at pages that exist. ``SITE_URL`` (public https origin) makes links absolute for email.
"""

import logging

from django.conf import settings
from django.urls import NoReverseMatch, reverse

logger = logging.getLogger(__name__)


def _path(name, *args):
    try:
        return reverse(name, args=args)
    except NoReverseMatch:
        # API and browser UI are separate projects; these are the frontend's
        # existing route contracts, not invented backend views.
        routes = {
            "booking-detail": "/bookings/{}/",
            "payment-detail": "/payments/{}/",
            "conversation-detail": "/discover/conversations/{}/",
            "support-case-detail": "/support/cases/{}/",
            "service-request-detail": "/requests/{}/",
            "verification": "/account/artisan/verification/",
            "cancellation-risk": "/account/risk/cancellation/",
            "promotion-applications": "/account/artisan/promotions/",
            "my-promotions": "/account/artisan/promotions/me/",
            "home": "/",
        }
        route = routes.get(name)
        if route is None:
            return None
        from urllib.parse import quote

        return route.format(*(quote(str(x), safe="") for x in args))


def absolute_url(path):
    """Return an absolute URL for ``path`` when ``SITE_URL`` is configured; otherwise ``None``."""
    base = str(getattr(settings, "SITE_URL", "") or "").rstrip("/")
    if not path or not base or not str(path).startswith("/"):
        return None
    return f"{base}{path}"


def booking_link(booking):
    return _path("booking-detail", booking.pk)


def payment_link(payment):
    return _path("payment-detail", payment.pk)


def conversation_link(conversation):
    return _path("conversation-detail", conversation.pk)


def support_case_link(case):
    return _path("support-case-detail", case.pk)


def service_request_link(service_request):
    return _path("service-request-detail", service_request.pk)


def verification_link():
    return _path("verification")


def cancellation_risk_link():
    return _path("cancellation-risk")


def promotion_application_link(application):
    path = _path("promotion-applications")
    if not path:
        return None
    return f"{path}?application={application.pk}"


def promotion_center_link():
    return _path("my-promotions") or _path("promotion-applications")


def dashboard_link():
    return _path("home")
