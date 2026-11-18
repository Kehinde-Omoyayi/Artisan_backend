"""Idempotent request-scoped retention; no public URLs and no media in audit logs."""

import logging
from datetime import timedelta
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.conf import settings
from django.utils.module_loading import import_string
from .models import ConversationAttachment, MediaPurge, MediaPreservationHold, Message
from .private_media import attachment_storage, permanently_delete

log = logging.getLogger(__name__)
RECORDINGS = Q(content_type__istartswith="audio/") | Q(
    content_type__istartswith="video/"
)


def eligible(request):
    from booking.models import Booking
    from support.models import SupportCase
    from payments.models import EscrowPayment
    from .chargebacks import unresolved_chargebacks

    if unresolved_chargebacks():
        return False
    closed_due = (
        request.status == "CLOSED"
        and request.closed_at is not None
        and request.closed_at <= timezone.now() - timedelta(days=7)
    )
    if request.status != "COMPLETED" and not closed_due:
        return False
    bookings = Booking.objects.filter(request_id=request.pk)
    # Only explicit, seven-day-old unbooked closure is eligible without a booking.
    if bookings.exclude(customer__user_id=request.requester_id).exists():
        return False
    if closed_due:
        if bookings.exists():
            return False
    else:
        if (
            not bookings.exists()
            or bookings.exclude(
                status__in=["COMPLETED", "CANCELLED", "REJECTED"]
            ).exists()
        ):
            return False
        if not bookings.filter(status="COMPLETED").exists():
            return False
    return not has_active_hold(request)


def has_active_hold(request):
    # Unknown provider/replica/backup configuration is a conservative preservation
    # gate, not evidence that no investigation or unmanaged copy exists.
    if getattr(settings, "ENVIRONMENT", "") == "production" and not all(
        getattr(settings, flag, False)
        for flag in (
            "FLW_CHARGEBACK_WEBHOOKS_CONFIRMED",
            "CONVERSATION_MEDIA_REPLICA_INVENTORY_CONFIRMED",
            "CONVERSATION_MEDIA_BACKUP_POLICY_CONFIRMED",
        )
    ):
        return True
    from support.models import SupportCase
    from payments.models import EscrowPayment
    from .chargebacks import unresolved_chargebacks

    if unresolved_chargebacks():
        return True
    if MediaPreservationHold.objects.filter(
        service_request=request, active=True
    ).exists():
        return True
    if SupportCase.objects.filter(
        Q(booking__request_id=request.pk)
        | Q(conversation__service_request_id=request.pk),
        case_type__in=["DISPUTE", "PAYMENT"],
        status__in=["OPEN", "UNDER_REVIEW"],
    ).exists():
        return True
    if EscrowPayment.objects.filter(
        booking__request_id=request.pk, status="DISPUTED"
    ).exists():
        return True
    return False


def recordings(request_id):
    return ConversationAttachment.objects.filter(
        RECORDINGS, message__conversation__service_request_id=request_id
    )


def access_blocked(attachment):
    if not attachment.content_type.lower().startswith(("audio/", "video/")):
        return False
    rid = attachment.message.conversation.service_request_id
    if not rid:
        return False
    from .purge_ledger import ledger_blocks

    try:
        if ledger_blocks(rid):
            return True
    except Exception:
        log.error("media_restore_guard_unavailable request=%s", rid)
        return True
    from services.models import ServiceRequest

    # Check authoritative current statuses on every playback, not a stale serializer.
    if MediaPurge.objects.filter(
        service_request_id=rid, blocked_at__isnull=False
    ).exists():
        return True
    return eligible(ServiceRequest.objects.get(pk=rid))


def queue_request(request_id, recheck=False):
    from services.models import ServiceRequest

    with transaction.atomic():
        req = ServiceRequest.objects.select_for_update().filter(pk=request_id).first()
        if not req or not eligible(req):
            return
        state, _ = MediaPurge.objects.get_or_create(service_request=req)
        if (
            state.status == MediaPurge.Status.COMPLETE
            and not recordings(req.pk).exists()
        ):
            return
        if (
            not recheck
            and state.next_attempt_at
            and state.next_attempt_at > timezone.now()
        ):
            return
        if recheck:
            state.next_attempt_at = None
        state.blocked_at = state.blocked_at or timezone.now()
        state.status = MediaPurge.Status.PENDING
        state.save(update_fields=["blocked_at", "status", "next_attempt_at"])

        def enqueue():
            from .tasks import purge_request_recordings

            try:
                purge_request_recordings.delay(req.pk)
            except Exception:
                log.error("media_purge_dispatch_failed request=%s", req.pk)

        transaction.on_commit(enqueue, robust=True)


def purge_request(request_id, force_scan=False):
    from services.models import ServiceRequest

    with transaction.atomic():
        req = ServiceRequest.objects.select_for_update().get(pk=request_id)
        state, _ = MediaPurge.objects.select_for_update().get_or_create(
            service_request=req
        )
        from .purge_ledger import ledger_blocks

        restore_deleted = (
            force_scan and ledger_blocks(req.pk) and not has_active_hold(req)
        )
        if not eligible(req) and not restore_deleted:
            if state.status != MediaPurge.Status.COMPLETE:
                state.status = MediaPurge.Status.NOT_ELIGIBLE
                state.save(update_fields=["status"])
            return state.status
        if (
            not force_scan
            and state.status == MediaPurge.Status.COMPLETE
            and not recordings(req.pk).exists()
        ):
            return state.status
        now = timezone.now()
        state.blocked_at = state.blocked_at or now
        state.started_at = now
        state.attempts += 1
        state.status = MediaPurge.Status.PROCESSING
        state.save()
        # Keep references until every binary delete/invalidation is verified.
        attachments = list(
            recordings(req.pk)
            .select_related("message__conversation")
            .prefetch_related("derivatives")
        )
        try:
            from .purge_ledger import remember_access_block

            remember_access_block(req.pk)
            from .private_media import conversation_storage, purge_recording_namespace
            from .models import Conversation

            for cid in Conversation.objects.filter(service_request=req).values_list(
                "pk", flat=True
            ):
                purge_recording_namespace(conversation_storage(), cid)
            for item in attachments:
                store = attachment_storage(item)
                for derived in item.derivatives.all():
                    permanently_delete(store, derived.storage_key)
                permanently_delete(store, item.file.name)
                from .models import MediaRelocation

                journal = MediaRelocation.objects.filter(attachment=item).first()
                if journal:
                    from django.core.files.storage import default_storage

                    permanently_delete(default_storage, journal.source_key)
                    permanently_delete(conversation_storage(), journal.target_key)
            # New private responses are no-store and no CDN is used by this app.
            # Existing/custom CDN deployments MUST supply a verified invalidator.
            if getattr(settings, "CONVERSATION_MEDIA_CDN_ENABLED", False):
                hook = getattr(settings, "CONVERSATION_MEDIA_CDN_PURGE_HOOK", "")
                if not hook or import_string(hook)(req.pk, attachments) is not True:
                    raise OSError("Cache invalidation not verified")
        except Exception:
            state.status = MediaPurge.Status.RETRY_REQUIRED
            state.next_attempt_at = now + timedelta(
                seconds=min(3600, 30 * 2 ** min(state.attempts, 7))
            )
            state.save(update_fields=["status", "next_attempt_at"])
            log.error(
                "media_purge_retry_required request=%s attempt=%s",
                req.pk,
                state.attempts,
            )
            try:
                notify_purge_failure(req.pk)
            except Exception:
                log.error("media_purge_alert_failed request=%s", req.pk)
            return state.status
        # Everything above is idempotent: missing files are a success, not lost keys.
        for item in attachments:
            msg = item.message
            if item.content_type.lower().startswith("audio/"):
                state.audio_count += 1
            else:
                state.video_count += 1
            # Post-delete signal is skipped: objects were already permanently deleted.
            item._retention_verified = True
            from .models import MediaRelocation

            MediaRelocation.objects.filter(attachment=item).delete()
            item.delete()
            if msg.message_type == Message.MessageType.USER and msg.metadata:
                msg.metadata = {}
                msg.save(update_fields=["metadata"])
            if not msg.body.strip() and not msg.attachments.exists():
                msg.body = "Media deleted under the recording retention policy."
                msg.message_type = Message.MessageType.SYSTEM
                msg.sender = None
                msg.event_code = "MEDIA_DELETED"
                msg.metadata = {}
                msg.save(
                    update_fields=[
                        "body",
                        "message_type",
                        "sender",
                        "event_code",
                        "metadata",
                    ]
                )
        state.status = MediaPurge.Status.COMPLETE
        state.completed_at = timezone.now()
        state.next_attempt_at = None
        state.save()
        return state.status


def reconcile(limit=500, force_scan=False):
    from services.models import ServiceRequest

    done = 0
    qs = (
        ServiceRequest.objects.all()
        if force_scan
        else ServiceRequest.objects.filter(
            Q(status="COMPLETED")
            | Q(status="CLOSED", closed_at__lte=timezone.now() - timedelta(days=7))
        )
    )
    for req in qs.order_by("pk").iterator(chunk_size=100):
        state = MediaPurge.objects.filter(service_request=req).first()
        if state and state.next_attempt_at and state.next_attempt_at > timezone.now():
            continue
        if (
            force_scan
            or not state
            or state.status != MediaPurge.Status.COMPLETE
            or recordings(req.pk).exists()
        ) and (
            eligible(req)
            or (
                force_scan
                and __import__(
                    "conversations.purge_ledger", fromlist=["ledger_blocks"]
                ).ledger_blocks(req.pk)
                and not has_active_hold(req)
            )
        ):
            purge_request(req.pk, force_scan=force_scan)
            done += 1
            if done >= limit:
                break
    return done


def notify_purge_failure(request_id):
    """Use existing deduplicated in-app/email outbox; never include object keys."""
    from django.contrib.auth import get_user_model
    from notifications.events import schedule_email

    day = timezone.localdate().isoformat()
    for user in (
        get_user_model().objects.filter(is_superuser=True, is_active=True).iterator()
    ):
        schedule_email(
            event_key=f"recording-purge-failed:{request_id}:{day}:{user.pk}",
            recipient=user,
            subject="Service recording cleanup needs attention",
            message=f"Recording cleanup for request #{request_id} requires a retry. Playback remains blocked. Review the media purge administration status and storage permissions. No media is attached.",
        )
