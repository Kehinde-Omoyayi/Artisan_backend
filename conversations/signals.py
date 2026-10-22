import logging

from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver

from .models import ConversationAttachment

logger = logging.getLogger(__name__)


@receiver(post_delete, sender=ConversationAttachment)
def delete_conversation_attachment_file(sender, instance, using=None, **kwargs):
    if getattr(instance, "_retention_verified", False):
        return
    from .private_media import attachment_storage

    storage = attachment_storage(instance)
    name = getattr(instance.file, "name", "")
    if not storage or not name:
        return

    def cleanup():
        try:
            storage.delete(name)
        except Exception:
            logger.exception(
                "conversation_attachment_cleanup_failed attachment=%s", instance.pk
            )

    # Storage is not transactional; defer deletion until the database commit so a
    # rolled-back row deletion cannot leave a still-referenced attachment missing.
    transaction.on_commit(cleanup, using=using)


from django.db.models.signals import post_save
from .models import MediaPreservationHold
from services.models import ServiceRequest
from booking.models import Booking
from support.models import SupportCase
from payments.models import EscrowPayment


@receiver(post_save, sender=ServiceRequest)
@receiver(post_save, sender=Booking)
@receiver(post_save, sender=SupportCase)
@receiver(post_save, sender=MediaPreservationHold)
@receiver(post_save, sender=EscrowPayment)
def refresh_recording_retention(sender, instance, **kwargs):
    if kwargs.get("raw"):
        return
    if sender is ServiceRequest:
        rid = instance.pk
    elif sender is Booking:
        rid = instance.request_id
    elif sender is MediaPreservationHold:
        rid = instance.service_request_id
    elif sender is EscrowPayment:
        rid = instance.booking.request_id
    else:
        rid = (
            instance.booking.request_id
            if instance.booking_id
            else (
                instance.conversation.service_request_id
                if instance.conversation_id
                else None
            )
        )
    if rid:
        from .retention import queue_request

        recheck = (
            (sender is MediaPreservationHold and not instance.active)
            or (sender is SupportCase and instance.status in ("RESOLVED", "CLOSED"))
            or (sender is ServiceRequest and instance.status == "COMPLETED")
        )
        transaction.on_commit(lambda: queue_request(rid, recheck=recheck), robust=True)


from django.db.models.signals import pre_delete
from django.db.models.deletion import ProtectedError


@receiver(pre_delete, sender=ConversationAttachment)
def protect_recording_reference(sender, instance, **kwargs):
    if (
        instance.content_type.lower().startswith(("audio/", "video/"))
        and instance.message.conversation.service_request_id
        and not getattr(instance, "_retention_verified", False)
    ):
        raise ProtectedError(
            "Request recording references may only be removed by the verified, hold-aware retention worker.",
            [instance],
        )
