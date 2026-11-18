import logging
from django.db import IntegrityError, transaction
from django.utils import timezone
from .models import InAppNotification

logger = logging.getLogger(__name__)

def create_in_app_notification(*, user, event_key, title, message, data=None):
    if not user or not event_key: return None
    try:
        return InAppNotification.objects.get_or_create(recipient=user, event_key=event_key, defaults={"title": title, "message": message, "data": data or {}})[0]
    except IntegrityError:
        try: return InAppNotification.objects.get(recipient=user, event_key=event_key)
        except InAppNotification.DoesNotExist:
            logger.exception("in_app_notification_race_unresolved event=%s user=%s", event_key, user.id); return None

def _campaign_id_from_event_key(event_key):
    """Campaign notifications are keyed "campaign:<id>:<user id>"."""
    parts = (event_key or "").split(":")
    # Campaign ids are UUIDs, so the segment is passed through as-is; an
    # unparseable id simply matches no delivery.
    if len(parts) >= 2 and parts[0] == "campaign" and parts[1]:
        return parts[1]
    return None


def _mark_campaign_opens(notifications, user):
    """A recipient reading a campaign notification is the only honest "open".

    Nothing else in the platform proves the message was seen, so the campaign
    open metric is fed from here and nowhere else.
    """
    campaign_ids = {
        campaign_id for campaign_id in (_campaign_id_from_event_key(n.event_key) for n in notifications)
        if campaign_id
    }
    if not campaign_ids:
        return
    try:
        from manager.services import campaigns as campaign_service
    except Exception:  # manager app absent or misconfigured: never break the inbox
        logger.exception("campaign_open_hook_unavailable user=%s", user.pk)
        return
    for campaign_id in campaign_ids:
        try:
            campaign_service.mark_open(user=user, campaign_id=campaign_id)
        except Exception:
            logger.exception("campaign_open_mark_failed user=%s campaign=%s", user.pk, campaign_id)


def mark_notification_read(*, notification_id, user):
    notification = InAppNotification.objects.filter(
        pk=notification_id, recipient=user, read_at__isnull=True).first()
    if not notification:
        return 0
    notification.read_at = timezone.now()
    notification.save(update_fields=["read_at"])
    _mark_campaign_opens([notification], user)
    return 1


def mark_all_notifications_read(*, user):
    pending = list(InAppNotification.objects.filter(recipient=user, read_at__isnull=True))
    if not pending:
        return 0
    InAppNotification.objects.filter(
        pk__in=[n.pk for n in pending]).update(read_at=timezone.now())
    _mark_campaign_opens(pending, user)
    return len(pending)
