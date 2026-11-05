"""Template context shared by every rendered page."""
import logging
import time

from .api import APIClient

logger = logging.getLogger(__name__)
UNREAD_REFRESH_SECONDS = 30
# Shoppers' badges are meaningless on staff tooling, and a manager account is
# refused by the shopper endpoints anyway, so the console is excluded outright.
SKIPPED_PREFIXES = ("/api/", "/static/", "/media/", "/health/", "/admin/", "/console/")


def _skips_badges(request, session):
    """True when this visitor never shows shopper badges."""
    return bool(session.get("is_manager")) or request.path.startswith(SKIPPED_PREFIXES)


def notifications(request):
    """Expose the unread notification count (cached in the session for a few seconds) to the navbar."""
    session = getattr(request, "session", None)
    if session is None or not session.get("api_access") or _skips_badges(request, session):
        return {}
    now = time.time()
    if now - float(session.get("notif_unread_checked_at", 0)) > UNREAD_REFRESH_SECONDS:
        try:
            _, payload, _ = APIClient(request).get("/notifications/unread-count/")
            session["notif_unread"] = int(payload.get("unread_count", 0)) if isinstance(payload, dict) else 0
        except Exception:  # the navbar badge must never break a page
            logger.warning("notification_badge_refresh_failed", exc_info=True)
            session.setdefault("notif_unread", 0)
        session["notif_unread_checked_at"] = now
    return {"unread_notification_count": session.get("notif_unread", 0)}


def unread_messages(request):
    """Expose the unread *message* count separately from notifications.

    The number comes from the same per-user read watermark the inbox list and
    the read endpoint use, so opening a conversation lowers both consistently
    and polling cannot inflate it. Workflow notifications are never counted here.
    """
    session = getattr(request, "session", None)
    if session is None or not session.get("api_access") or _skips_badges(request, session):
        return {}
    now = time.time()
    if now - float(session.get("msg_unread_checked_at", 0)) > UNREAD_REFRESH_SECONDS:
        try:
            _, payload, _ = APIClient(request).get("/conversations/unread-count/")
            session["msg_unread"] = int(payload.get("unread_count", 0)) if isinstance(payload, dict) else 0
        except Exception:  # a badge must never break a page
            logger.warning("message_badge_refresh_failed", exc_info=True)
            session.setdefault("msg_unread", 0)
        session["msg_unread_checked_at"] = now
    return {"unread_message_count": session.get("msg_unread", 0)}
