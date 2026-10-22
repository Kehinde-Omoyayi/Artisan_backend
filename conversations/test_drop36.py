"""Regression contracts for explicit drafts, unbooked closure and production gates."""

import tempfile
from pathlib import Path
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from django.test import TestCase, override_settings
from django.utils import timezone
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.core.management import call_command
from rest_framework.exceptions import ValidationError, PermissionDenied
from core.models import OutboxEvent
from notifications.models import InAppNotification
from services.models import ServiceRequest
from payments.models import PaymentWebhookEvent, EscrowPayment
from .tests import ConversationTestMixin
from . import test_recording_policy as recording_policy
from .models import (
    Conversation,
    ConversationAttachment,
    MediaPreservationHold,
    MediaRelocation,
    MediaPolicyIdentity,
    ConversationReadState,
    Message,
)
from .services import (
    start_category_request,
    apply_request_patch,
    send_message,
    create_offer,
    request_quote_revision,
    mark_conversation_read,
)
from .retention import eligible, purge_request, access_blocked
from .notifications import notify_room_message


class NewLifecycleTests(TestCase, ConversationTestMixin):
    def setUp(self):
        recording_policy.RecordingPolicyTests.setUp(self)

    complete_both = recording_policy.RecordingPolicyTests.complete_both

    def close_unbooked(self):
        self.booking.delete()
        ServiceRequest.objects.filter(pk=self.req.pk).update(status="OPEN")
        _, self.conversation = apply_request_patch(
            conversation=self.conversation,
            user=self.customer,
            payload={"close_request": True},
        )
        self.req.refresh_from_db()

    def test_close_is_explicit_owner_only_and_idempotent(self):
        with self.assertRaises(ValidationError):
            apply_request_patch(
                conversation=self.conversation,
                user=self.customer,
                payload={"close_request": True},
            )
        self.close_unbooked()
        closed = self.req.closed_at
        apply_request_patch(
            conversation=self.conversation,
            user=self.customer,
            payload={"close_request": True},
        )
        self.req.refresh_from_db()
        self.assertEqual(self.req.closed_at, closed)
        with self.assertRaises(PermissionDenied):
            apply_request_patch(
                conversation=self.conversation,
                user=self.artisan_user,
                payload={"close_request": True},
            )

    def test_seven_days_not_idle_age(self):
        self.booking.delete()
        ServiceRequest.objects.filter(pk=self.req.pk).update(
            status="OPEN", created_at=timezone.now() - timedelta(days=365)
        )
        self.req.refresh_from_db()
        self.assertFalse(eligible(self.req))
        apply_request_patch(
            conversation=self.conversation,
            user=self.customer,
            payload={"close_request": True},
        )
        self.req.refresh_from_db()
        self.assertFalse(eligible(self.req))
        self.assertFalse(access_blocked(self.audio))
        ServiceRequest.objects.filter(pk=self.req.pk).update(
            closed_at=timezone.now() - timedelta(days=7, seconds=1)
        )
        self.req.refresh_from_db()
        self.assertTrue(eligible(self.req))
        self.assertTrue(access_blocked(self.audio))
        self.assertEqual(purge_request(self.req.pk), "COMPLETE")
        self.assertTrue(self.store.exists(self.photo.file.name))
        self.msg.refresh_from_db()
        self.assertEqual(self.msg.body, "Keep this text")

    def test_hold_blocks_closed_purge_until_formal_release(self):
        self.close_unbooked()
        ServiceRequest.objects.filter(pk=self.req.pk).update(
            closed_at=timezone.now() - timedelta(days=8)
        )
        self.req.refresh_from_db()
        hold = MediaPreservationHold.objects.create(
            service_request=self.req, kind="LEGAL"
        )
        self.assertFalse(eligible(self.req))
        self.assertEqual(purge_request(self.req.pk), "NOT_ELIGIBLE")
        hold.active = False
        hold.release_reason = "Test fixture: authorized reviewer resolved the case."
        hold.released_at = timezone.now()
        hold.save()
        self.assertTrue(eligible(self.req))

    def test_closed_request_cannot_be_edited_by_automation(self):
        self.close_unbooked()
        before = self.conversation.draft
        with patch("core.tasks.dispatch_outbox_events.delay"):
            send_message(
                conversation=self.conversation,
                user=self.customer,
                body="Actually make it tomorrow",
                idempotency_key="closed-text",
            )
        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.draft, before)
        from .automation import assistant_context

        self.assertEqual(
            assistant_context(conversation=self.conversation, user=self.customer)[
                "mode"
            ],
            "closed",
        )

    def test_category_draft_has_legitimate_stable_reference_without_presence(self):
        room, _ = start_category_request(
            user=self.customer, category_id=self.category.pk
        )
        ref = room.service_request_id
        self.assertEqual(room.service_request.status, "DRAFT")
        self.assertEqual(room.service_request.requester_id, self.customer.pk)
        again, created = start_category_request(
            user=self.customer, category_id=self.category.pk
        )
        self.assertFalse(created)
        self.assertEqual(again.pk, room.pk)
        data = {
            "service_id": self.service.pk,
            "description": "Repair a socket",
            "location_id": str(self.location.pk),
            "preferred_date": (timezone.localdate() + timedelta(days=1)).isoformat(),
        }
        apply_request_patch(conversation=room, user=self.customer, payload=data)
        room.refresh_from_db()
        self.assertEqual(room.service_request.status, "DRAFT")
        apply_request_patch(
            conversation=room, user=self.customer, payload={"submit_request": True}
        )
        room.refresh_from_db()
        self.assertEqual(room.service_request_id, ref)
        self.assertEqual(room.service_request.status, "OPEN")

    def test_closing_category_draft_releases_draft_start_key(self):
        room, _ = start_category_request(
            user=self.customer, category_id=self.category.pk
        )
        apply_request_patch(
            conversation=room, user=self.customer, payload={"close_request": True}
        )
        fresh, created = start_category_request(
            user=self.customer, category_id=self.category.pk
        )
        self.assertTrue(created)
        self.assertNotEqual(fresh.pk, room.pk)

    def test_first_unread_burst_dedupes_and_notification_read_does_not_read_messages(
        self,
    ):
        from notifications.in_app import mark_all_notifications_read

        notify_room_message(self.msg)
        notify_room_message(self.msg)
        prefix = f"chat-burst:{self.conversation.pk}:{self.artisan_user.pk}:"
        self.assertEqual(
            OutboxEvent.objects.filter(event_key__startswith=prefix).count(), 1
        )
        mark_all_notifications_read(user=self.artisan_user)
        self.assertFalse(
            ConversationReadState.objects.filter(
                conversation=self.conversation, user=self.artisan_user
            ).exists()
        )
        mark_conversation_read(conversation=self.conversation, user=self.artisan_user)
        notify_room_message(self.msg)
        self.assertEqual(
            OutboxEvent.objects.filter(event_key__startswith=prefix).count(), 2
        )

    def test_unmatched_authenticated_chargeback_conservatively_pauses_deletion(self):
        self.complete_both()
        with override_settings(
            ENVIRONMENT="production",
            FLW_CHARGEBACK_WEBHOOKS_CONFIRMED=False,
            CONVERSATION_MEDIA_REPLICA_INVENTORY_CONFIRMED=False,
            CONVERSATION_MEDIA_BACKUP_POLICY_CONFIRMED=False,
        ):
            self.assertFalse(eligible(self.req))
        event = PaymentWebhookEvent.objects.create(
            provider="flutterwave",
            event_type="chargeback.initiated",
            event_key="cb-unmatched",
            payload={"data": {"id": 12, "flw_ref": "not-yet-matched"}},
        )
        from .chargebacks import process_chargeback_event

        self.assertFalse(process_chargeback_event(event))
        self.assertFalse(eligible(self.req))

    def test_matched_chargeback_creates_idempotent_hold_and_never_guesses_release(self):
        payment = EscrowPayment.objects.create(
            booking=self.booking,
            customer=self.customer,
            artisan=self.artisan,
            reference="hold-pay",
            provider_transaction_id="123",
            verification_payload={"data": {"flw_ref": "flw-original"}},
            service_amount_kobo=10000,
            total_amount_kobo=25000,
            platform_commission_kobo=1000,
            artisan_payout_kobo=9000,
        )
        event = PaymentWebhookEvent.objects.create(
            provider="flutterwave",
            event_type="chargeback.initiated",
            event_key="cb-match",
            payload={"data": {"id": 13, "flw_ref": "flw-original"}},
        )
        from .chargebacks import process_chargeback_event

        self.assertTrue(process_chargeback_event(event))
        self.assertTrue(process_chargeback_event(event))
        self.assertEqual(
            MediaPreservationHold.objects.filter(
                service_request=self.req, kind="CHARGEBACK", active=True
            ).count(),
            1,
        )
        payment.refresh_from_db()
        self.assertEqual(payment.status, "PAYMENT_PENDING")

    def test_legacy_private_mover_checksums_and_removes_source_and_journal(self):
        from .legacy_media import migrate_attachment

        with tempfile.TemporaryDirectory() as d:
            legacy = FileSystemStorage(location=d)
            old = legacy.save("legacy.jpg", ContentFile(b"unchanged photo"))
            item = ConversationAttachment.objects.create(
                message=self.msg,
                file=old,
                private_storage=False,
                content_type="image/jpeg",
                size=15,
                scan_status="CLEAN",
            )
            with patch("conversations.legacy_media.default_storage", legacy):
                self.assertEqual(migrate_attachment(item.pk), "migrated")
            item.refresh_from_db()
            self.assertTrue(item.private_storage)
            self.assertFalse(legacy.exists(old))
            self.assertTrue(self.store.exists(item.file.name))
            self.assertFalse(MediaRelocation.objects.filter(attachment=item).exists())

    def test_external_ledger_identity_prevents_id_reuse_collision(self):
        from .purge_ledger import remember_access_block, ledger_blocks

        with tempfile.TemporaryDirectory() as d, override_settings(
            CONVERSATION_PURGE_LEDGER_PATH=str(Path(d) / "ledger")
        ):
            remember_access_block(self.req.pk)
            self.assertTrue(ledger_blocks(self.req.pk))
            import uuid

            ServiceRequest.objects.filter(pk=self.req.pk).update(
                media_policy_id=uuid.uuid4()
            )
            self.assertFalse(ledger_blocks(self.req.pk))

    def test_external_receipt_mismatch_is_fail_closed(self):
        from .purge_ledger import verify_database_identity
        from django.core.exceptions import ImproperlyConfigured

        MediaPolicyIdentity.objects.get_or_create(pk=1)
        with tempfile.TemporaryDirectory() as d, override_settings(
            ENVIRONMENT="production",
            CONVERSATION_PURGE_LEDGER_PATH=str(Path(d) / "ledger"),
        ):
            call_command("initialize_media_policy")
            verify_database_identity()
            Path(d, "ledger.identity").write_text("wrong-database")
            with self.assertRaises(ImproperlyConfigured):
                verify_database_identity()

    def test_formal_quote_revision_and_payload_reuse(self):
        payload = self.offer_payload()
        payload.update(
            transport_amount=Decimal("1000"),
            additional_amount=Decimal("500"),
            additional_charge_details="Waste removal",
            total_amount=Decimal("26500"),
        )
        quote = create_offer(
            conversation=self.conversation,
            user=self.artisan_user,
            payload=payload,
            idempotency_key="quote-key",
        )
        self.assertEqual(quote.version, 1)
        with self.assertRaises(ValidationError):
            create_offer(
                conversation=self.conversation,
                user=self.artisan_user,
                payload={**payload, "exclusions": "Different exclusion"},
                idempotency_key="quote-key",
            )
        request_quote_revision(
            conversation=self.conversation, user=self.customer, offer_id=quote.pk
        )
        revised = create_offer(
            conversation=self.conversation,
            user=self.artisan_user,
            payload=payload,
            idempotency_key="quote-v2",
            parent_offer_id=quote.pk,
        )
        self.assertEqual(revised.parent_offer_id, quote.pk)
        self.assertEqual(revised.version, 2)
        quote.refresh_from_db()
        self.assertEqual(quote.status, "COUNTERED")
        revised.status = "ACCEPTED"
        revised.save(update_fields=["status"])
        revised.payment_terms = "Changed after acceptance"
        with self.assertRaises(ValueError):
            revised.save()

    def test_partial_playback_is_authenticated_and_completion_still_denies_range(self):
        from rest_framework.test import APIClient

        client = APIClient()
        url = f"/api/v1/conversations/{self.conversation.pk}/media/{self.audio.pk}/"
        self.assertIn(client.get(url, HTTP_RANGE="bytes=0-3").status_code, (401, 403))
        client.force_authenticate(self.customer)
        response = client.get(url, HTTP_RANGE="bytes=0-3")
        self.assertEqual(response.status_code, 206)
        self.assertEqual(b"".join(response.streaming_content), b"fixt")
        response.close()
        self.assertEqual(client.get(url, HTTP_RANGE="bytes=999999-").status_code, 416)
        self.complete_both()
        self.assertEqual(client.get(url, HTTP_RANGE="bytes=0-3").status_code, 404)

    def test_external_tombstone_repurges_pre_completion_restore_without_inventing_completion(
        self,
    ):
        from .purge_ledger import remember_access_block
        from .retention import reconcile

        with tempfile.TemporaryDirectory() as d, override_settings(
            CONVERSATION_PURGE_LEDGER_PATH=str(Path(d) / "ledger")
        ):
            remember_access_block(self.req.pk)
            self.assertFalse(eligible(self.req))
            self.assertTrue(access_blocked(self.audio))
            self.assertEqual(reconcile(force_scan=True), 1)
            self.assertFalse(self.store.exists(self.audio.file.name))
            self.req.refresh_from_db()
            self.assertNotEqual(self.req.status, "COMPLETED")

    def test_generic_delete_cannot_bypass_media_policy(self):
        from django.db.models.deletion import ProtectedError

        with self.assertRaises(ProtectedError):
            self.audio.delete()
        self.assertTrue(self.store.exists(self.audio.file.name))

    def test_category_chat_infers_only_actual_catalogue_services_and_locations(self):
        room, _ = start_category_request(
            user=self.customer, category_id=self.category.pk
        )
        from .automation import request_options_for, extract_message_hints

        options = request_options_for(room)
        self.assertIn(str(self.service.pk), [r["id"] for r in options["services"]])
        self.assertTrue(options["locations"])
        hints = extract_message_hints(
            conversation=room,
            body=f"{self.service.name} at {self.location.area}, {self.location.state} tomorrow",
        )
        self.assertEqual(str(hints.get("service_id")), str(self.service.pk))


from django.test import SimpleTestCase
from unittest.mock import Mock


class ReplicaDeletionTests(SimpleTestCase):
    def setup_store(self):
        from botocore.exceptions import ClientError

        self.objects = {
            "primary-bucket": {
                "private/conversation_recordings/room/a.webm": "v1",
                "private/conversation_attachments/room/photo.jpg": "photo",
            },
            "replica-bucket": {
                "private/conversation_recordings/room/a.webm": "r1",
                "private/conversation_recordings/room/orphan.webm": "r2",
                "private/conversation_attachments/room/photo.jpg": "photo2",
            },
        }
        store = Mock()
        store.bucket_name = "primary-bucket"
        store.location = "private"
        store._normalize_name.side_effect = lambda key: "private/" + key
        client = store.connection.meta.client
        client.get_bucket_replication.return_value = {
            "ReplicationConfiguration": {
                "Rules": [
                    {
                        "Status": "Enabled",
                        "Destination": {"Bucket": "arn:aws:s3:::replica-bucket"},
                    }
                ]
            }
        }
        client.get_bucket_versioning.return_value = {"Status": "Enabled"}

        def head(Bucket, Key, **kwargs):
            if Key not in self.objects[Bucket]:
                raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
            return {"ReplicationStatus": "COMPLETED"}

        client.head_object.side_effect = head

        def paginator(operation):
            p = Mock()

            def pages(Bucket, Prefix):
                return [
                    {
                        "Versions": [
                            {"Key": k, "VersionId": v}
                            for k, v in self.objects[Bucket].items()
                            if k.startswith(Prefix)
                        ]
                    }
                ]

            p.paginate.side_effect = pages
            return p

        client.get_paginator.side_effect = paginator
        client.delete_object.side_effect = lambda Bucket, Key, **kw: self.objects[
            Bucket
        ].pop(Key, None)
        return store, client

    def test_namespace_removes_replica_only_orphan_and_preserves_photos(self):
        from .private_media import purge_recording_namespace

        store, client = self.setup_store()
        purge_recording_namespace(store, "room")
        for rows in self.objects.values():
            self.assertEqual(len(rows), 1)
            self.assertIn("photo.jpg", next(iter(rows)))

    def test_replication_pending_is_not_marked_deleted(self):
        from .private_media import permanently_delete

        store, client = self.setup_store()
        client.head_object.side_effect = None
        client.head_object.return_value = {"ReplicationStatus": "PENDING"}
        with self.assertRaises(OSError):
            permanently_delete(store, "conversation_recordings/room/a.webm")
        client.delete_object.assert_not_called()


class ReadCursorTests(TestCase, ConversationTestMixin):
    def test_read_marker_cannot_swallow_later_arrival_or_read_another_room(self):
        customer = self.make_user("read-owner@example.invalid")
        artisan_user, artisan = self.make_artisan("read-artisan@example.invalid")
        room = Conversation.objects.create(customer=customer, artisan=artisan)
        first = Message.objects.create(
            conversation=room, sender=artisan_user, body="Visible in fetched batch"
        )
        Message.objects.create(
            conversation=room, sender=artisan_user, body="Arrived after that batch"
        )
        self.assertEqual(
            mark_conversation_read(
                conversation=room, user=customer, last_message_id=first.pk
            ),
            1,
        )
        state = ConversationReadState.objects.get(conversation=room, user=customer)
        self.assertEqual(state.last_read_at, first.created_at)
        foreign = Conversation.objects.create(customer=customer, artisan=artisan)
        other = Message.objects.create(
            conversation=foreign, sender=artisan_user, body="Another room"
        )
        with self.assertRaises(ValidationError):
            mark_conversation_read(
                conversation=room, user=customer, last_message_id=other.pk
            )
