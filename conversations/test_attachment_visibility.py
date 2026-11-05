"""Uploaded photos/documents must be viewable without any external scanner.

This platform does not run an image/AI inspection step. Uploads are
signature-validated, stored ``CLEAN`` and served straight away. Scanning only
applies if a deployment points ``CLAMAV_SCAN_URL`` at a real scanner, and a file
the scanner explicitly flags is never served.
"""
from io import BytesIO

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from PIL import Image

from conversations.models import ConversationAttachment
from conversations.services import send_message
from conversations.tests import ConversationTestMixin

def _png_bytes():
    buffer = BytesIO()
    Image.new("RGB", (2, 2), (120, 80, 40)).save(buffer, format="PNG")
    return buffer.getvalue()


class AttachmentVisibilityTests(TestCase, ConversationTestMixin):
    def setUp(self):
        self.customer = self.make_user("customer@example.com")
        self.artisan_user, self.artisan = self.make_artisan()
        self.conversation = self.make_conversation()

    def send(self, upload):
        return send_message(
            conversation=self.conversation,
            user=self.customer,
            body="",
            attachments=[upload],
        )

    @override_settings(CLAMAV_SCAN_URL="", REQUIRE_MEDIA_SCAN=False)
    def test_image_is_immediately_clean_and_has_a_url(self):
        from conversations.serializers import AttachmentSerializer

        message = self.send(SimpleUploadedFile("photo.png", _png_bytes(), content_type="image/png"))
        attachment = message.attachments.get()
        self.assertEqual(attachment.scan_status, "CLEAN")
        self.assertEqual(attachment.scan_reason, "signature_validation_only")
        data = AttachmentSerializer(attachment).data
        self.assertIsNotNone(data["url"], "photo must be viewable without a scan worker")

    @override_settings(CLAMAV_SCAN_URL="", REQUIRE_MEDIA_SCAN=False)
    def test_pdf_document_is_immediately_downloadable(self):
        from conversations.serializers import AttachmentSerializer

        pdf = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"
        message = self.send(
            SimpleUploadedFile("quote.pdf", pdf, content_type="application/pdf")
        )
        attachment = message.attachments.get()
        self.assertEqual(attachment.scan_status, "CLEAN")
        self.assertIsNotNone(AttachmentSerializer(attachment).data["url"])

    @override_settings(CLAMAV_SCAN_URL="http://scanner.local/scan", REQUIRE_MEDIA_SCAN=True)
    def test_opt_in_scanner_deployments_still_gate_media_on_the_scan(self):
        from conversations.serializers import AttachmentSerializer

        message = self.send(SimpleUploadedFile("photo.png", _png_bytes(), content_type="image/png"))
        attachment = message.attachments.get()
        self.assertEqual(attachment.scan_status, "PENDING")
        self.assertIsNone(AttachmentSerializer(attachment).data["url"])

    @override_settings(CLAMAV_SCAN_URL="", REQUIRE_MEDIA_SCAN=False)
    def test_media_flagged_by_a_scanner_is_never_served(self):
        from rest_framework.exceptions import NotFound

        from conversations.media_scan import scan_blocked
        from conversations.media_views import ConversationMediaView
        from conversations.serializers import AttachmentSerializer

        message = self.send(SimpleUploadedFile("photo.png", _png_bytes(), content_type="image/png"))
        attachment = message.attachments.get()
        ConversationAttachment.objects.filter(pk=attachment.pk).update(
            scan_status="INFECTED", scan_reason="eicar test signature"
        )
        attachment.refresh_from_db()
        self.assertTrue(scan_blocked(attachment))
        self.assertIsNone(AttachmentSerializer(attachment).data["url"])

    @override_settings(CLAMAV_SCAN_URL="", REQUIRE_MEDIA_SCAN=False)
    def test_pending_attachment_left_over_from_an_old_scan_job_still_previews(self):
        """Rows uploaded before this change are stuck at PENDING; they must work too."""
        from conversations.serializers import AttachmentSerializer

        message = self.send(SimpleUploadedFile("photo.png", _png_bytes(), content_type="image/png"))
        attachment = message.attachments.get()
        ConversationAttachment.objects.filter(pk=attachment.pk).update(scan_status="PENDING")
        attachment.refresh_from_db()
        self.assertIsNotNone(AttachmentSerializer(attachment).data["url"])
