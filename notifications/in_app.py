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

def mark_notification_read(*, notification_id, user):
    return InAppNotification.objects.filter(pk=notification_id, recipient=user, read_at__isnull=True).update(read_at=timezone.now())


def mark_all_notifications_read(*, user):
    return InAppNotification.objects.filter(recipient=user, read_at__isnull=True).update(read_at=timezone.now())
