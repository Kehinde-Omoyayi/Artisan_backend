import os
from datetime import timedelta
from io import BytesIO
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import Group
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from PIL import Image

from booking.models import Booking, BookingStatusHistory
from home.models import ArtisanProfile, CustomUser, CustomerProfile, Locations, Service, ServiceCategory
from payments.models import EscrowPayment
from services.models import ServiceRequest
from support.models import SupportCase
from support.permissions import SUPPORT_SUPERVISOR
from support.services import close_support_case, resolve_support_case

from .models import Conversation, ConversationAttachment, Message
from .tasks import purge_finalized_conversation_images


class ConversationImageRetentionTests(TestCase):
    def setUp(self):
        self.customer_user = CustomUser.objects.create_user(
            email="retention-customer@example.com",
            first_name="Rita",
            last_name="Customer",
            password="StrongPassword123!",
            is_active=True,
        )
        self.customer = CustomerProfile.objects.create(
            user=self.customer_user,
            country="Nigeria",
            state="Lagos",
            area="Ikeja",
        )
        self.artisan_user = CustomUser.objects.create_user(
            email="retention-artisan@example.com",
            first_name="Ayo",
            last_name="Artisan",
            password="StrongPassword123!",
            is_active=True,
        )
        self.artisan = ArtisanProfile.objects.create(
            user=self.artisan_user,
            business_name="Retention Artisan",
            profile_status="ACTIVE",
            country="Nigeria",
            state="Lagos",
            area="Ikeja",
        )
        self.location = Locations.objects.create(country="Nigeria", state="Lagos", area="Ikeja")
        category = ServiceCategory.objects.create(name="Retention repairs")
        service = Service.objects.create(category=category, name="Socket repair")
        self.request = ServiceRequest.objects.create(
            requester=self.customer_user,
            service=service,
            category=category,
            location=self.location,
            preferred_date=timezone.localdate() + timedelta(days=1),
            description="Replace a damaged socket.",
        )
        self.conversation = Conversation.objects.create(
            conversation_type=Conversation.ConversationType.SERVICE,
            customer=self.customer_user,
            artisan=self.artisan,
            service_request=self.request,
        )
        self.conversation.participants.add(self.customer_user, self.artisan_user)

    def make_image(self, name="service-photo.png"):
        buffer = BytesIO()
        Image.new("RGB", (2, 2), color=(150, 80, 40)).save(buffer, format="PNG")
        return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")

    def make_attachment(self, conversation, *, content_type="image/png", name="service-photo.png", body="Shared job photo."):
        message = Message.objects.create(conversation=conversation, sender=self.customer_user, body=body)
        if content_type.startswith("image/"):
            upload = self.make_image(name)
        else:
            upload = SimpleUploadedFile(name, b"%PDF-1.4 test document", content_type=content_type)
        attachment = ConversationAttachment.objects.create(
            message=message,
            file=upload,
            content_type=content_type,
            size=upload.size,
            scan_status="CLEAN",
        )
        return message, attachment

    def make_booking(self, *, request=None, status=Booking.Status.COMPLETED, finalized_at=None, price="20000.00"):
        return Booking.objects.create(
            request=request or self.request,
            artisan=self.artisan,
            customer=self.customer,
            scheduled_time_start=timezone.now() + timedelta(days=1),
            scheduled_time_end=timezone.now() + timedelta(days=1, hours=2),
            price_agreed=price,
            status=status,
            finalized_at=finalized_at,
        )

    def make_payment(self, booking, status):
        return EscrowPayment.objects.create(
            booking=booking,
            customer=self.customer_user,
            artisan=self.artisan,
            reference=f"RETENTION-{booking.pk}-{status}",
            service_amount_kobo=2_000_000,
            total_amount_kobo=2_015_000,
            platform_commission_kobo=200_000,
            artisan_payout_kobo=1_800_000,
            status=status,
        )

    def test_retention_task_is_queued_for_the_real_worker_and_daily_beat_schedule(self):
        from Artisan_platform.celery import app as celery_app

        task_name = "conversations.tasks.purge_finalized_conversation_images"
        self.assertEqual(settings.CELERY_TASK_ROUTES[task_name]["queue"], "maintenance")
        self.assertEqual(
            celery_app.conf.beat_schedule["purge-finalized-conversation-images-every-day"]["task"],
            task_name,
        )

    def test_seven_day_retention_waits_for_payment_finality_and_keeps_text_and_records(self):
        now = timezone.now()
        booking = self.make_booking(finalized_at=now - timedelta(days=7, minutes=1))
        payment = self.make_payment(booking, EscrowPayment.Status.HELD)

        with TemporaryDirectory() as media_root, override_settings(
            MEDIA_ROOT=media_root,
            CONVERSATION_IMAGE_RETENTION_DAYS=7,
        ):
            message, attachment = self.make_attachment(self.conversation)
            ConversationAttachment.objects.filter(pk=attachment.pk).update(uploaded_at=now - timedelta(days=8))
            image_path = attachment.file.path

            with patch("django.utils.timezone.now", return_value=now):
                self.assertTrue(os.path.exists(image_path))
                waiting = purge_finalized_conversation_images.run(batch_size=25)
                self.assertEqual(waiting["deleted"], 0)
                self.assertTrue(ConversationAttachment.objects.filter(pk=attachment.pk).exists())

                payment.status = EscrowPayment.Status.RELEASED
                payment.save(update_fields=["status", "updated_at"])
                removed = purge_finalized_conversation_images.run(batch_size=25)

                self.assertEqual(removed["deleted"], 1)
                self.assertFalse(ConversationAttachment.objects.filter(pk=attachment.pk).exists())
                self.assertFalse(os.path.exists(image_path))
                self.assertTrue(Message.objects.filter(pk=message.pk, body="Shared job photo.").exists())
                self.assertTrue(Conversation.objects.filter(pk=self.conversation.pk).exists())
                self.assertTrue(Booking.objects.filter(pk=booking.pk).exists())
                self.assertTrue(EscrowPayment.objects.filter(pk=payment.pk).exists())
                self.assertEqual(purge_finalized_conversation_images.run(batch_size=25)["deleted"], 0)

    def test_in_progress_and_closed_dispute_with_unsettled_payment_attachments_are_retained(self):
        now = timezone.now()
        old_finalization = now - timedelta(days=12)
        active_booking = self.make_booking(status=Booking.Status.IN_PROGRESS, finalized_at=old_finalization)

        disputed_request = ServiceRequest.objects.create(
            requester=self.customer_user,
            category=self.request.category,
            location=self.location,
            preferred_date=timezone.localdate() + timedelta(days=2),
            description="Repair another outlet.",
        )
        disputed_conversation = Conversation.objects.create(
            conversation_type=Conversation.ConversationType.SERVICE,
            customer=self.customer_user,
            artisan=self.artisan,
            service_request=disputed_request,
        )
        disputed_conversation.participants.add(self.customer_user, self.artisan_user)
        disputed_booking = self.make_booking(
            request=disputed_request,
            status=Booking.Status.DISPUTED,
            finalized_at=None,
            price="30000.00",
        )
        BookingStatusHistory.objects.create(
            booking=disputed_booking,
            status=Booking.Status.IN_PROGRESS,
            changed_by="ARTISAN",
            changed_by_user=self.artisan_user,
            notes="The artisan started the booked service.",
        )
        BookingStatusHistory.objects.create(
            booking=disputed_booking,
            status=Booking.Status.DISPUTED,
            changed_by="CUSTOMER",
            changed_by_user=self.customer_user,
            notes="Customer opened a verified dispute.",
        )
        case = SupportCase.objects.create(
            opened_by=self.customer_user,
            booking=disputed_booking,
            case_type=SupportCase.CaseType.DISPUTE,
            subject="Unresolved service dispute",
            description="Support is reviewing the work.",
            status=SupportCase.Status.OPEN,
        )
        disputed_payment = self.make_payment(disputed_booking, EscrowPayment.Status.DISPUTED)
        supervisor = CustomUser.objects.create_user(
            email="retention-supervisor@example.com",
            first_name="Support",
            last_name="Supervisor",
            password="StrongPassword123!",
            is_active=True,
            is_staff=True,
        )
        supervisor.groups.add(Group.objects.create(name=SUPPORT_SUPERVISOR))
        resolve_support_case(
            case=case,
            staff_user=supervisor,
            resolution_code=SupportCase.ResolutionCode.NO_ADJUSTMENT,
            resolution_note="The service dispute was reviewed; payment settlement is still required.",
        )
        with self.assertRaisesRegex(ValueError, "payments must be settled"):
            close_support_case(case=case, staff_user=supervisor)
        disputed_booking.refresh_from_db()
        case.refresh_from_db()
        disputed_payment.refresh_from_db()
        self.assertEqual(case.status, SupportCase.Status.RESOLVED)
        self.assertIsNone(disputed_booking.finalized_at)
        self.assertEqual(disputed_payment.status, EscrowPayment.Status.HELD)

        with TemporaryDirectory() as media_root, override_settings(
            MEDIA_ROOT=media_root,
            CONVERSATION_IMAGE_RETENTION_DAYS=7,
        ):
            _, active_attachment = self.make_attachment(self.conversation, name="active-service.png")
            _, disputed_attachment = self.make_attachment(disputed_conversation, name="disputed-service.png")
            old_upload = now - timedelta(days=10)
            ConversationAttachment.objects.filter(pk__in=[active_attachment.pk, disputed_attachment.pk]).update(uploaded_at=old_upload)

            with patch("django.utils.timezone.now", return_value=now):
                waiting = purge_finalized_conversation_images.run(batch_size=25)
                self.assertEqual(waiting["deleted"], 0)
                self.assertTrue(ConversationAttachment.objects.filter(pk=active_attachment.pk).exists())
                self.assertTrue(ConversationAttachment.objects.filter(pk=disputed_attachment.pk).exists())
                self.assertTrue(SupportCase.objects.filter(pk=case.pk, status=SupportCase.Status.RESOLVED).exists())
                self.assertTrue(EscrowPayment.objects.filter(pk=disputed_payment.pk, status=EscrowPayment.Status.HELD).exists())
                self.assertTrue(Booking.objects.filter(pk=active_booking.pk).exists())

                # Keep the booking and held payment retained while service work is still in progress.
                self.assertEqual(disputed_booking.status, Booking.Status.IN_PROGRESS)
                self.assertIsNone(disputed_booking.finalized_at)

                # Simulate the normal completion workflow followed by provider-verified payment release.
                # The seven-day image-retention clock comes from the booking's real finalization time,
                # not from support merely closing a case.
                completion_time = now - timedelta(days=8)
                disputed_booking.status = Booking.Status.COMPLETED
                disputed_booking.finalized_at = completion_time
                disputed_booking.save(update_fields=["status", "finalized_at", "updated_at"])
                with patch("django.utils.timezone.now", return_value=completion_time):
                    BookingStatusHistory.objects.create(
                        booking=disputed_booking,
                        status=Booking.Status.COMPLETED,
                        changed_by="CUSTOMER",
                        changed_by_user=self.customer_user,
                        notes="Customer confirmed completion after the dispute was resolved.",
                    )
                disputed_payment.status = EscrowPayment.Status.RELEASED
                disputed_payment.save(update_fields=["status", "updated_at"])
                close_support_case(case=case, staff_user=supervisor)
                disputed_booking.refresh_from_db()
                case.refresh_from_db()
                self.assertEqual(case.status, SupportCase.Status.CLOSED)
                self.assertEqual(disputed_booking.finalized_at, completion_time)

                settled = purge_finalized_conversation_images.run(batch_size=25)
                self.assertEqual(settled["deleted"], 1)
                self.assertTrue(ConversationAttachment.objects.filter(pk=active_attachment.pk).exists())
                self.assertFalse(ConversationAttachment.objects.filter(pk=disputed_attachment.pk).exists())

    def test_task_keeps_recent_non_image_and_direct_chat_attachments(self):
        now = timezone.now()
        booking = self.make_booking(finalized_at=now - timedelta(days=9))
        self.make_payment(booking, EscrowPayment.Status.RELEASED)
        direct = Conversation.objects.create(conversation_type=Conversation.ConversationType.DIRECT)
        direct.participants.add(self.customer_user, self.artisan_user)

        with TemporaryDirectory() as media_root, override_settings(
            MEDIA_ROOT=media_root,
            CONVERSATION_IMAGE_RETENTION_DAYS=7,
        ):
            _, recent = self.make_attachment(self.conversation, name="recent-service.png")
            _, document = self.make_attachment(
                self.conversation,
                content_type="application/pdf",
                name="invoice.pdf",
                body="A service record.",
            )
            _, direct_image = self.make_attachment(direct, name="direct-chat.png")
            ConversationAttachment.objects.filter(pk=recent.pk).update(uploaded_at=now - timedelta(days=1))
            old_upload = now - timedelta(days=9)
            ConversationAttachment.objects.filter(pk__in=[document.pk, direct_image.pk]).update(uploaded_at=old_upload)

            with patch("django.utils.timezone.now", return_value=now):
                result = purge_finalized_conversation_images.run(batch_size=25)
                self.assertEqual(result["deleted"], 0)
                self.assertTrue(ConversationAttachment.objects.filter(pk=recent.pk).exists())
                self.assertTrue(ConversationAttachment.objects.filter(pk=document.pk).exists())
                self.assertTrue(ConversationAttachment.objects.filter(pk=direct_image.pk).exists())

                other_user = CustomUser.objects.create_user(
                    email="different-artisan@example.com",
                    first_name="Different",
                    last_name="Artisan",
                    password="StrongPassword123!",
                    is_active=True,
                )
                other_artisan = ArtisanProfile.objects.create(
                    user=other_user,
                    business_name="Different Artisan",
                    profile_status="ACTIVE",
                )
                self.conversation.artisan = other_artisan
                self.conversation.save(update_fields=["artisan", "updated_at"])
                _, mismatched = self.make_attachment(self.conversation, name="mismatched-transaction.png")
                ConversationAttachment.objects.filter(pk=mismatched.pk).update(uploaded_at=old_upload)

                mismatch_result = purge_finalized_conversation_images.run(batch_size=25)
                self.assertEqual(mismatch_result["deleted"], 0)
                self.assertTrue(ConversationAttachment.objects.filter(pk=mismatched.pk).exists())
