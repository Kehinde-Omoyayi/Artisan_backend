from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from conversations.models import Conversation
from home.models import (
    ArtisanProfile,
    CustomUser,
    Locations,
    Service,
    ServiceCategory,
    CustomerProfile,
)
from payments.models import EscrowPayment
from services.models import ServiceRequest
from .models import Booking


class BookingPaymentGuardTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.customer_user = CustomUser.objects.create_user(
            email="customer@example.com",
            first_name="Customer",
            last_name="User",
            password="StrongPassword123!",
            is_active=True,
        )
        artisan_user = CustomUser.objects.create_user(
            email="artisan@example.com",
            first_name="Artisan",
            last_name="User",
            password="StrongPassword123!",
            is_active=True,
        )
        self.customer = CustomerProfile.objects.create(user=self.customer_user)
        self.artisan = ArtisanProfile.objects.create(
            user=artisan_user, profile_status="ACTIVE"
        )
        category = ServiceCategory.objects.create(name="Painting")
        Service.objects.create(category=category, name="Wall Painting")
        location = Locations.objects.create(
            country="Nigeria", state="Ogun", area="Alabata"
        )
        request = ServiceRequest.objects.create(
            requester=self.customer_user,
            category=category,
            location=location,
            preferred_date=(timezone.now() + timedelta(days=1)).date(),
            description="Paint two rooms.",
        )
        self.booking = Booking.objects.create(
            request=request,
            artisan=self.artisan,
            customer=self.customer,
            scheduled_time_start=timezone.now() + timedelta(days=1),
            scheduled_time_end=timezone.now() + timedelta(days=1, hours=2),
            price_agreed="20000.00",
            status=Booking.Status.ACCEPTED,
        )
        EscrowPayment.objects.create(
            booking=self.booking,
            customer=self.customer_user,
            artisan=self.artisan,
            reference="PAY-TEST-1",
            service_amount_kobo=2000000,
            total_amount_kobo=2015000,
            platform_commission_kobo=200000,
            artisan_payout_kobo=1800000,
        )

    def test_legacy_customer_booking_link_only_exposes_its_exact_service_conversation(
        self,
    ):
        unrelated_user = CustomUser.objects.create_user(
            email="unrelated-conversation-customer@example.com",
            first_name="Other",
            last_name="Customer",
            password="StrongPassword123!",
            is_active=True,
        )
        unrelated = Conversation(
            conversation_type=Conversation.ConversationType.SERVICE,
            customer=unrelated_user,
            artisan=self.artisan,
            service_request=self.booking.request,
        )
        # Deliberately inject a corrupt legacy row without invoking the new owner guard.
        Conversation.objects.bulk_create([unrelated])
        self.client.force_authenticate(self.customer_user)

        response = self.client.get(f"/api/v1/booking/{self.booking.id}/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNone(response.data["conversation_id"])

        unrelated.delete()
        related = Conversation.objects.create(
            conversation_type=Conversation.ConversationType.SERVICE,
            customer=self.customer_user,
            artisan=self.artisan,
            service_request=self.booking.request,
        )
        response = self.client.get(f"/api/v1/booking/{self.booking.id}/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["conversation_id"], str(related.id))

    def test_artisan_completion_only_requests_customer_confirmation(self):
        self.booking.status = Booking.Status.IN_PROGRESS
        self.booking.save(update_fields=["status", "updated_at"])
        self.client.force_authenticate(self.artisan.user)

        response = self.client.post(
            f"/api/v1/booking/{self.booking.id}/complete/",
            {"note": "Work completed and ready for customer review."},
            format="json",
            HTTP_IDEMPOTENCY_KEY="complete-1",
        )

        self.assertEqual(response.status_code, 200)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, Booking.Status.COMPLETION_REQUESTED)
        self.assertIsNotNone(self.booking.artisan_completed_at)
        self.assertIsNone(self.booking.customer_completed_at)

    def test_customer_cannot_cancel_booking_with_associated_payment(self):
        self.client.force_authenticate(self.customer_user)
        response = self.client.post(
            f"/api/v1/booking/{self.booking.id}/cancel/",
            {"reason": "Plans changed."},
            format="json",
            HTTP_IDEMPOTENCY_KEY="cancel-paid-1",
        )
        self.assertEqual(response.status_code, 409)

    def test_customer_can_cancel_after_a_failed_payment(self):
        payment = EscrowPayment.objects.get(booking=self.booking)
        payment.status = EscrowPayment.Status.FAILED
        payment.save(update_fields=["status", "updated_at"])

        self.client.force_authenticate(self.customer_user)
        response = self.client.post(
            f"/api/v1/booking/{self.booking.id}/cancel/",
            {"reason": "Payment failed and booking will not proceed."},
            format="json",
            HTTP_IDEMPOTENCY_KEY="cancel-failed-1",
        )

        self.assertEqual(response.status_code, 200)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, Booking.Status.CANCELLED)
        self.assertIsNotNone(self.booking.finalized_at)

    def test_customer_booking_creation_is_disabled_in_favour_of_chat_negotiation(self):
        self.client.force_authenticate(self.customer_user)
        response = self.client.post(
            "/api/v1/booking/",
            {
                "request": self.booking.request_id,
                "artisan": self.artisan.id,
                "price_agreed": "20000.00",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 410)
        self.assertEqual(Booking.objects.count(), 1)

    def test_cancellation_notifies_the_artisan_with_a_booking_deep_link(self):
        from django.urls import reverse
        from notifications.models import InAppNotification

        payment = EscrowPayment.objects.get(booking=self.booking)
        payment.status = EscrowPayment.Status.FAILED
        payment.save(update_fields=["status", "updated_at"])
        self.client.force_authenticate(self.customer_user)
        response = self.client.post(
            f"/api/v1/booking/{self.booking.id}/cancel/",
            {"reason": "Plans changed and the work is no longer needed."},
            format="json",
            HTTP_IDEMPOTENCY_KEY="cancel-notify-1",
        )
        self.assertEqual(response.status_code, 200, response.data)
        notification = InAppNotification.objects.get(
            recipient=self.artisan.user, event_key__startswith="booking_cancelled"
        )
        self.assertEqual(
            notification.data["action_url"],
            reverse("booking-detail", args=[self.booking.pk]),
        )
        # The customer who cancelled is not notified about their own action.
        self.assertFalse(
            InAppNotification.objects.filter(
                recipient=self.customer_user, event_key__startswith="booking_cancelled"
            ).exists()
        )
