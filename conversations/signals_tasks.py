from celery import shared_task
from django.db import transaction

from .media_scan import scan_attachment
from .models import ConversationAttachment


@shared_task(bind=True, max_retries=5, retry_backoff=True, retry_jitter=True)
def scan_conversation_attachment(self, attachment_id):
    try: attachment = ConversationAttachment.objects.get(pk=attachment_id)
    except ConversationAttachment.DoesNotExist: return {"status": "not_found"}
    clean, reason = scan_attachment(attachment)
    with transaction.atomic():
        attachment = ConversationAttachment.objects.select_for_update(of=("self",)).get(pk=attachment_id)
        attachment.scan_status = "CLEAN" if clean is True else "INFECTED" if clean is False else "FAILED"
        attachment.scan_reason = reason
        attachment.save(update_fields=["scan_status", "scan_reason"])
    return {"status": attachment.scan_status, "reason": reason}
