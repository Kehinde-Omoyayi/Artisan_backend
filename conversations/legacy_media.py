"""Move legacy public-origin attachments with a crash-safe, temporary journal.

Never drops the old source before checksum-verifying the private copy. A retry can
finish after source deletion or DB rollback because both keys/digest are committed
before any binary side effect. Journals are removed only after verified completion.
"""

import hashlib, uuid
from pathlib import Path
from django.db import transaction
from django.core.files.storage import default_storage
from django.conf import settings
from django.utils.module_loading import import_string
from .models import ConversationAttachment, MediaRelocation
from .private_media import conversation_storage, permanently_delete


def digest(storage, key):
    h = hashlib.sha256()
    size = 0
    with storage.open(key, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def migrate_attachment(attachment_id):
    from services.models import ServiceRequest

    item = (
        ConversationAttachment.objects.select_related("message__conversation")
        .filter(pk=attachment_id, private_storage=False)
        .first()
    )
    if not item:
        return "already_private_or_removed"
    journal = MediaRelocation.objects.filter(attachment=item).first()
    if journal is None:
        sha, size = digest(default_storage, item.file.name)
        prefix = (
            "conversation_recordings"
            if item.content_type.lower().startswith(("audio/", "video/"))
            else "conversation_attachments"
        )
        key = f"{prefix}/{item.message.conversation_id}/{uuid.uuid4().hex}{Path(item.file.name).suffix.lower()}"
        with transaction.atomic():
            rid = item.message.conversation.service_request_id
            if rid:
                ServiceRequest.objects.select_for_update().get(pk=rid)
            item = (
                ConversationAttachment.objects.select_for_update()
                .filter(pk=item.pk, private_storage=False)
                .first()
            )
            if not item:
                return "already_private_or_removed"
            journal, _ = MediaRelocation.objects.get_or_create(
                attachment=item,
                defaults={
                    "source_key": item.file.name,
                    "target_key": key,
                    "sha256": sha,
                    "byte_size": size,
                },
            )
    with transaction.atomic():
        rid = item.message.conversation.service_request_id
        if rid:
            ServiceRequest.objects.select_for_update().get(pk=rid)
        item = (
            ConversationAttachment.objects.select_for_update()
            .filter(pk=item.pk, private_storage=False)
            .first()
        )
        if not item:
            return "already_private_or_removed"
        journal = MediaRelocation.objects.select_for_update().get(attachment=item)
        private = conversation_storage()
        expected = (journal.sha256, journal.byte_size)
        if not private.exists(journal.target_key):
            with default_storage.open(journal.source_key, "rb") as source:
                stored = private.save(journal.target_key, source)
            if stored != journal.target_key:
                raise OSError(
                    "Reserved private key was unexpectedly renamed; preserve both origins for review."
                )
        if digest(private, journal.target_key) != expected:
            raise OSError("Private copy verification failed; source is retained.")
        permanently_delete(default_storage, journal.source_key)
        if getattr(settings, "CONVERSATION_LEGACY_CDN_ENABLED", False):
            hook = getattr(settings, "CONVERSATION_LEGACY_CDN_REVOKE_HOOK", "")
            if not hook or import_string(hook)(item.pk, journal.source_key) is not True:
                raise OSError("Legacy CDN origin revocation not verified")
        item.size = journal.byte_size
        item.file.name = journal.target_key
        item.private_storage = True
        item.save(update_fields=["file", "private_storage", "size"])
        journal.delete()
        return "migrated"
