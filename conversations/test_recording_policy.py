import json, subprocess, tempfile
from pathlib import Path
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock, patch
from django.test import TestCase, SimpleTestCase, override_settings
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework.exceptions import ValidationError
from home.models import CustomerProfile, Locations, ServiceCategory, Service
from services.models import ServiceRequest
from booking.models import Booking
from support.models import SupportCase
from .tests import ConversationTestMixin
from .models import (
    Conversation,
    Message,
    ConversationAttachment,
    MediaDerivative,
    MediaPurge,
    MediaPreservationHold,
)
from .private_media import conversation_storage, permanently_delete
from .retention import purge_request, eligible, access_blocked, queue_request
from .security import validate_attachment
from .services import send_message


class RecordingPolicyTests(TestCase, ConversationTestMixin):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings_override = override_settings(
            CONVERSATION_PRIVATE_MEDIA_ROOT=self.directory.name
        )
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        conversation_storage.cache_clear()
        self.addCleanup(conversation_storage.cache_clear)
        field = ConversationAttachment._meta.get_field("file")
        old = field.storage
        field.storage = conversation_storage()
        self.addCleanup(setattr, field, "storage", old)
        self.customer = self.make_user("media-customer@example.invalid")
        self.cp = CustomerProfile.objects.create(user=self.customer)
        self.artisan_user, self.artisan = self.make_artisan(
            "media-artisan@example.invalid"
        )
        self.location = Locations.objects.create(
            country="Nigeria", state="Ogun", area="Abeokuta"
        )
        self.category = ServiceCategory.objects.create(name="Media test category")
        self.service = Service.objects.create(
            name="Media test service", category=self.category
        )
        self.req = ServiceRequest.objects.create(
            requester=self.customer,
            service=self.service,
            category=self.category,
            location=self.location,
            preferred_date=timezone.localdate(),
            description="Test request",
        )
        self.conversation = Conversation.objects.create(
            customer=self.customer, artisan=self.artisan, service_request=self.req
        )
        self.booking = Booking.objects.create(
            request=self.req,
            artisan=self.artisan,
            customer=self.cp,
            scheduled_time_start=timezone.now(),
            scheduled_time_end=timezone.now() + timedelta(hours=1),
            price_agreed=Decimal("100"),
            status="ACCEPTED",
        )
        self.msg = Message.objects.create(
            conversation=self.conversation, sender=self.customer, body="Keep this text"
        )
        self.audio = ConversationAttachment.objects.create(
            message=self.msg,
            file=ContentFile(b"fixture audio bytes", name="fixture.webm"),
            content_type="audio/webm",
            size=19,
            scan_status="CLEAN",
        )
        self.video = ConversationAttachment.objects.create(
            message=self.msg,
            file=ContentFile(b"fixture video bytes", name="fixture.mp4"),
            content_type="video/mp4",
            size=19,
            scan_status="CLEAN",
        )
        self.photo = ConversationAttachment.objects.create(
            message=self.msg,
            file=ContentFile(b"fixture photo bytes", name="fixture.jpg"),
            content_type="image/jpeg",
            size=19,
            scan_status="CLEAN",
        )
        self.store = conversation_storage()
        self.names = [self.audio.file.name, self.video.file.name]
        self.thumb = self.store.save(
            "derivatives/test-thumbnail.jpg", ContentFile(b"thumb")
        )
        MediaDerivative.objects.create(
            attachment=self.video, storage_key=self.thumb, kind="thumbnail"
        )

    def complete_both(self):
        ServiceRequest.objects.filter(pk=self.req.pk).update(status="COMPLETED")
        Booking.objects.filter(pk=self.booking.pk).update(status="COMPLETED")
        self.req.refresh_from_db()

    def test_only_one_completed_does_not_purge(self):
        for request_status, booking_status in [
            ("COMPLETED", "ACCEPTED"),
            ("ATTENDED", "COMPLETED"),
        ]:
            ServiceRequest.objects.filter(pk=self.req.pk).update(status=request_status)
            self.req.refresh_from_db()
            Booking.objects.filter(pk=self.booking.pk).update(status=booking_status)
            self.assertEqual(purge_request(self.req.pk), "NOT_ELIGIBLE")
            self.assertTrue(self.store.exists(self.names[0]))

    def test_purge_deletes_recordings_and_derivatives_preserves_photo_text(self):
        self.complete_both()
        self.assertTrue(access_blocked(self.audio))
        self.assertEqual(purge_request(self.req.pk), "COMPLETE")
        for key in [*self.names, self.thumb]:
            self.assertFalse(self.store.exists(key))
        self.assertFalse(
            ConversationAttachment.objects.filter(
                pk__in=[self.audio.pk, self.video.pk]
            ).exists()
        )
        self.assertFalse(MediaDerivative.objects.exists())
        self.assertTrue(self.store.exists(self.photo.file.name))
        self.msg.refresh_from_db()
        self.assertEqual(self.msg.body, "Keep this text")
        state = MediaPurge.objects.get(service_request=self.req)
        self.assertEqual((state.audio_count, state.video_count), (1, 1))
        audit = {f.name: str(getattr(state, f.name)) for f in state._meta.fields}
        self.assertNotIn("conversation_attachments", json.dumps(audit))
        self.assertEqual(purge_request(self.req.pk), "COMPLETE")
        state.refresh_from_db()
        self.assertEqual(state.audio_count, 1)

    def test_media_only_becomes_generic_system_placeholder(self):
        self.msg.body = ""
        self.msg.metadata = {"url": "must-be-erased"}
        self.msg.save()
        self.photo.delete()
        self.complete_both()
        purge_request(self.req.pk)
        self.msg.refresh_from_db()
        self.assertEqual(self.msg.event_code, "MEDIA_DELETED")
        self.assertEqual(self.msg.metadata, {})
        self.assertEqual(self.msg.message_type, "SYSTEM")
        self.assertIsNone(self.msg.sender_id)

    def test_failed_delete_retains_references_and_blocks_access_until_retry(self):
        self.complete_both()
        with patch(
            "conversations.retention.permanently_delete",
            side_effect=PermissionError("sensitive path not logged"),
        ):
            self.assertEqual(purge_request(self.req.pk), "RETRY_REQUIRED")
        self.assertTrue(
            ConversationAttachment.objects.filter(pk=self.audio.pk).exists()
        )
        self.assertTrue(access_blocked(self.audio))
        self.assertEqual(purge_request(self.req.pk), "COMPLETE")

    def test_storage_false_success_is_not_complete(self):
        self.complete_both()
        with patch.object(self.store, "delete", return_value=None):
            self.assertEqual(purge_request(self.req.pk), "RETRY_REQUIRED")
        self.assertTrue(
            ConversationAttachment.objects.filter(pk=self.audio.pk).exists()
        )

    def test_legal_chargeback_and_explicit_dispute_holds(self):
        self.complete_both()
        for kind in ["LEGAL", "CHARGEBACK", "DISPUTE"]:
            hold = MediaPreservationHold.objects.create(
                service_request=self.req, kind=kind
            )
            self.assertFalse(eligible(self.req))
            self.assertEqual(purge_request(self.req.pk), "NOT_ELIGIBLE")
            self.assertTrue(self.store.exists(self.names[0]))
            hold.active = False
            hold.release_reason = "Test fixture: authorized reviewer resolved the case."
            hold.released_at = timezone.now()
            hold.save()
        self.assertEqual(purge_request(self.req.pk), "COMPLETE")

    def test_open_support_dispute_pauses_and_release_allows_purge(self):
        self.complete_both()
        case = SupportCase.objects.create(
            booking=self.booking,
            conversation=self.conversation,
            opened_by=self.customer,
            case_type="DISPUTE",
            subject="Test dispute",
            description="Service issue",
        )
        self.assertEqual(purge_request(self.req.pk), "NOT_ELIGIBLE")
        case.status = "RESOLVED"
        case.save()
        self.assertEqual(purge_request(self.req.pk), "COMPLETE")

    def test_no_booking_never_guesses_a_closure_rule(self):
        self.complete_both()
        self.booking.delete()
        self.assertEqual(purge_request(self.req.pk), "NOT_ELIGIBLE")

    def test_queue_after_commit_and_broker_failure_is_durable(self):
        self.complete_both()
        with patch(
            "conversations.tasks.purge_request_recordings.delay",
            side_effect=RuntimeError("broker down"),
        ):
            with self.captureOnCommitCallbacks(execute=True):
                queue_request(self.req.pk)
        state = MediaPurge.objects.get(service_request=self.req)
        self.assertEqual(state.status, "PENDING")
        self.assertIsNotNone(state.blocked_at)

    def test_authenticated_playback_and_completion_revocation(self):
        client = APIClient()
        url = reverse("conversation-media", args=[self.conversation.pk, self.audio.pk])
        self.assertEqual(client.get(url).status_code, 401)
        outsider = self.make_user("media-outsider@example.invalid")
        client.force_authenticate(outsider)
        self.assertEqual(client.get(url).status_code, 404)
        for user in [self.customer, self.artisan_user]:
            client.force_authenticate(user)
            response = client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertIn("no-store", response["Cache-Control"])
            response.close()
        self.complete_both()
        client.force_authenticate(self.customer)
        self.assertEqual(client.get(url).status_code, 404)

    def test_restore_of_old_attachment_is_blocked_and_repurged(self):
        self.complete_both()
        purge_request(self.req.pk)
        revived = ConversationAttachment.objects.create(
            message=self.msg,
            file=ContentFile(b"restored content", name="restored.webm"),
            content_type="audio/webm",
            size=16,
            scan_status="CLEAN",
        )
        self.assertTrue(access_blocked(revived))
        self.assertEqual(purge_request(self.req.pk), "COMPLETE")
        self.assertFalse(ConversationAttachment.objects.filter(pk=revived.pk).exists())

    def test_upload_rollback_orphan_and_restored_orphan_namespace_are_purged(self):
        key = self.store.save(
            f"conversation_recordings/{self.conversation.pk}/orphan.webm",
            ContentFile(b"orphaned upload"),
        )
        self.complete_both()
        purge_request(self.req.pk)
        self.assertFalse(self.store.exists(key))
        self.store.save(key, ContentFile(b"restored orphan"))
        purge_request(self.req.pk, force_scan=True)
        self.assertFalse(self.store.exists(key))

    def test_both_roles_upload_privately_and_cannot_impersonate_system_events(self):
        for sender in [self.customer, self.artisan_user]:
            with patch(
                "conversations.services.validate_attachment", return_value="audio/webm"
            ):
                message = send_message(
                    conversation=self.conversation,
                    user=sender,
                    body="Payment confirmed (ordinary user text)",
                    attachments=[SimpleUploadedFile("note.webm", b"fixture")],
                )
            self.assertEqual(message.message_type, "USER")
            self.assertEqual(message.event_code, "")
            item = message.attachments.get()
            self.assertTrue(item.private_storage)
            self.assertTrue(item.file.name.startswith("conversation_recordings/"))

    def test_late_recording_and_draft_recording_uploads_are_rejected(self):
        self.complete_both()
        with patch(
            "conversations.services.validate_attachment", return_value="audio/webm"
        ):
            with self.assertRaises(ValidationError):
                send_message(
                    conversation=self.conversation,
                    user=self.customer,
                    body="",
                    attachments=[SimpleUploadedFile("note.webm", b"fixture")],
                )
            self.conversation.service_request = None
            with self.assertRaises(ValidationError):
                send_message(
                    conversation=self.conversation,
                    user=self.customer,
                    body="",
                    attachments=[SimpleUploadedFile("note.webm", b"fixture")],
                )

    def test_external_tombstone_blocks_a_pre_completion_database_restore(self):
        path = Path(self.directory.name) / "private-ledger.jsonl"
        path.touch(mode=0o600)
        with override_settings(CONVERSATION_PURGE_LEDGER_PATH=str(path)):
            self.complete_both()
            purge_request(self.req.pk)
            restored = ConversationAttachment.objects.create(
                message=self.msg,
                file=ContentFile(b"restored old bytes", name="before-completion.webm"),
                content_type="audio/webm",
                size=18,
                scan_status="CLEAN",
            )
            ServiceRequest.objects.filter(pk=self.req.pk).update(status="OPEN")
            MediaPurge.objects.filter(service_request=self.req).delete()
            self.assertTrue(access_blocked(restored))
            data = path.read_text()
            self.assertNotIn("conversation_recordings", data)
            self.assertNotIn("before-completion", data)


class RecordingValidationTests(SimpleTestCase):
    def make_audio(self, directory, seconds=1):
        path = Path(directory) / "test.wav"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=8000:cl=mono",
                "-t",
                str(seconds),
                str(path),
            ],
            check=True,
            timeout=10,
        )
        return SimpleUploadedFile(
            "test.wav", path.read_bytes(), content_type="application/octet-stream"
        )

    @override_settings(CONVERSATION_RECORDINGS_ENABLED=True)
    def test_real_content_and_duration_enforced(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(validate_attachment(self.make_audio(d)), "audio/wav")
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValidationError):
                validate_attachment(self.make_audio(d, 91))

    @override_settings(CONVERSATION_RECORDINGS_ENABLED=True)
    def test_signature_and_missing_decoder_fail_closed(self):
        with self.assertRaises(ValidationError):
            validate_attachment(
                SimpleUploadedFile("note.webm", b"fake", content_type="audio/webm")
            )
        with tempfile.TemporaryDirectory() as d:
            upload = self.make_audio(d)
            with override_settings(FFPROBE_BINARY="/does/not/exist"):
                with self.assertRaises(ValidationError):
                    validate_attachment(upload)

    def test_flag_defaults_off(self):
        with self.assertRaises(ValidationError):
            validate_attachment(
                SimpleUploadedFile("note.webm", b"anything", content_type="audio/webm")
            )

    def test_s3_permanent_version_deletion(self):
        store = Mock()
        store.bucket_name = "bucket"
        store._normalize_name.return_value = "private/key"
        store.exists.return_value = False
        client = store.connection.meta.client
        client.get_bucket_replication.return_value = {}
        client.get_bucket_versioning.return_value = {"Status": "Enabled"}
        client.get_paginator.return_value.paginate.side_effect = [
            [
                {
                    "Versions": [
                        {"Key": "private/key", "VersionId": "v1"},
                        {"Key": "private/key-neighbour", "VersionId": "safe"},
                    ],
                    "DeleteMarkers": [{"Key": "private/key", "VersionId": "marker"}],
                }
            ],
            [],
        ]
        from botocore.exceptions import ClientError

        client.head_object.side_effect = ClientError(
            {"Error": {"Code": "404"}}, "HeadObject"
        )
        permanently_delete(store, "key")
        self.assertEqual(client.delete_object.call_count, 2)
        self.assertFalse(
            any(
                c.kwargs["Key"].endswith("neighbour")
                for c in client.delete_object.call_args_list
            )
        )

    @override_settings(CONVERSATION_RECORDINGS_ENABLED=True)
    def test_real_video_upload_detects_content_not_browser_mime(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clip.mp4"
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=64x64:r=10",
                    "-t",
                    "1",
                    "-an",
                    "-c:v",
                    "libx264",
                    str(path),
                ],
                check=True,
                timeout=10,
            )
            upload = SimpleUploadedFile(
                "clip.mp4", path.read_bytes(), content_type="image/png"
            )
            self.assertEqual(validate_attachment(upload), "video/mp4")
