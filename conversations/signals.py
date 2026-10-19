import logging

from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver

from .models import ConversationAttachment

logger = logging.getLogger(__name__)


@receiver(post_delete, sender=ConversationAttachment)
def delete_conversation_attachment_file(sender, instance, using=None, **kwargs):
    storage = getattr(instance.file, "storage", None)
    name = getattr(instance.file, "name", "")
    if not storage or not name:
        return

    def cleanup():
        try:
            storage.delete(name)
        except Exception:
            logger.exception("conversation_attachment_cleanup_failed attachment=%s", instance.pk)

    # Storage is not transactional; defer deletion until the database commit so a
    # rolled-back row deletion cannot leave a still-referenced attachment missing.
    transaction.on_commit(cleanup, using=using)
