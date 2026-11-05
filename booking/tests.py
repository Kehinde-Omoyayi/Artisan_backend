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

    def test_customer_can_cancel_accepted_booking_whose_payment_link_was_generated(
        self,
    ):
        """An unpaid link (generated, opened, or just pending) never blocks cancel."""
        self.client.force_authenticate(self.customer_user)
        response = self.client.post(
            f"/api/v1/booking/{self.booking.id}/cancel/",
            {"reason": "Plans changed before paying."},
            format="json",
            HTTP_IDEMPOTENCY_KEY="cancel-unpaid-1",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, Booking.Status.CANCELLED)
        self.assertIsNotNone(self.booking.finalized_at)
        payment = EscrowPayment.objects.get(booking=self.booking)
        self.assertIsNotNone(payment.link_voided_at)
        # The reference survives so a late provider callback can still be matched.
        self.assertEqual(payment.reference, "PAY-TEST-1")
        self.assertFalse(response.data["payment"]["money_taken_from_customer"])
        self.assertTrue(response.data["payment"]["link_voided"])
        from payments.models import PaymentLedgerEntry

        self.assertTrue(
            PaymentLedgerEntry.objects.filter(
                payment=payment, event_type="PAYMENT_LINK_VOIDED"
            ).exists()
        )

    def test_a_second_cancel_attempt_does_not_duplicate_the_cancellation(self):
        self.client.force_authenticate(self.customer_user)
        first = self.client.post(
            f"/api/v1/booking/{self.booking.id}/cancel/",
            {"reason": "Plans changed."},
            format="json",
            HTTP_IDEMPOTENCY_KEY="cancel-twice-1",
        )
        self.assertEqual(first.status_code, 200, first.data)
        replay = self.client.post(
            f"/api/v1/booking/{self.booking.id}/cancel/",
            {"reason": "Plans changed."},
            format="json",
            HTTP_IDEMPOTENCY_KEY="cancel-twice-1",
        )
        self.assertEqual(replay.status_code, 200, replay.data)
        second = self.client.post(
            f"/api/v1/booking/{self.booking.id}/cancel/",
            {"reason": "Trying again."},
            format="json",
            HTTP_IDEMPOTENCY_KEY="cancel-twice-2",
        )
        self.assertEqual(second.status_code, 409)
        from booking.models import BookingStatusHistory

        self.assertEqual(
            BookingStatusHistory.objects.filter(
                booking=self.booking, status=Booking.Status.CANCELLED
            ).count(),
            1,
        )

    def test_cancelled_booking_cannot_prepare_another_payment(self):
        from payments.services import prepare_booking_payment
        from payments.models import EscrowPayment as Payment

        self.client.force_authenticate(self.customer_user)
        self.client.post(
            f"/api/v1/booking/{self.booking.id}/cancel/",
            {"reason": "Plans changed."},
            format="json",
            HTTP_IDEMPOTENCY_KEY="cancel-nopay-1",
        )
        self.booking.refresh_from_db()
        with self.assertRaisesMessage(ValueError, "cancelled"):
            prepare_booking_payment(booking=self.booking)
        self.assertEqual(Payment.objects.filter(booking=self.booking).count(), 1)

    def test_money_held_in_escrow_still_blocks_cancellation(self):
        payment = EscrowPayment.objects.get(booking=self.booking)
        payment.status = EscrowPayment.Status.HELD
        payment.save(update_fields=["status", "updated_at"])

        self.client.force_authenticate(self.customer_user)
        response = self.client.post(
            f"/api/v1/booking/{self.booking.id}/cancel/",
            {"reason": "Please cancel."},
            format="json",
            HTTP_IDEMPOTENCY_KEY="cancel-held-1",
        )
        self.assertEqual(response.status_code, 409)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, Booking.Status.ACCEPTED)

    def test_available_actions_follow_role_and_state(self):
        self.client.force_authenticate(self.customer_user)
        payload = self.client.get(f"/api/v1/booking/{self.booking.id}/").data
        codes = [item["code"] for item in payload["available_actions"]]
        self.assertIn("CANCEL_BOOKING", codes)
        self.assertIn("PAY_BOOKING", codes)
        self.assertNotIn("START_WORK", codes)
        self.assertNotIn("CONFIRM_COMPLETION", codes)

        self.client.force_authenticate(self.artisan.user)
        payload = self.client.get(f"/api/v1/booking/{self.booking.id}/").data
        codes = [item["code"] for item in payload["available_actions"]]
        self.assertNotIn("START_WORK", codes)  # payment is not held yet
        self.assertNotIn("PAY_BOOKING", codes)
        self.assertNotIn("CANCEL_BOOKING", codes)

        payment = EscrowPayment.objects.get(booking=self.booking)
        payment.status = EscrowPayment.Status.HELD
        payment.save(update_fields=["status", "updated_at"])
        payload = self.client.get(f"/api/v1/booking/{self.booking.id}/").data
        codes = [item["code"] for item in payload["available_actions"]]
        self.assertIn("START_WORK", codes)

        self.booking.status = Booking.Status.REQUESTED
        self.booking.save(update_fields=["status", "updated_at"])
        payload = self.client.get(f"/api/v1/booking/{self.booking.id}/").data
        codes = [item["code"] for item in payload["available_actions"]]
        self.assertIn("ACCEPT_BOOKING", codes)
        self.assertIn("REJECT_BOOKING", codes)

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
        # A notification must open the browser page, never the JSON API route.
        self.assertEqual(
            notification.data["action_url"], f"/bookings/{self.booking.pk}/"
        )
        # The customer who cancelled is not notified about their own action.
        self.assertFalse(
            InAppNotification.objects.filter(
                recipient=self.customer_user, event_key__startswith="booking_cancelled"
            ).exists()
        )


class BookingCountdownTests(TestCase):
    """Countdown starts only on the artisan's authorized start and never resets."""

    def setUp(self):
        self.client = APIClient()
        self.customer_user = CustomUser.objects.create_user(
            email="countdown-customer@example.com",
            first_name="Ada",
            last_name="Obi",
            password="StrongPassword123!",
            is_active=True,
        )
        self.artisan_user = CustomUser.objects.create_user(
            email="countdown-artisan@example.com",
            first_name="Tunde",
            last_name="Bello",
            password="StrongPassword123!",
            is_active=True,
        )
        customer = CustomerProfile.objects.create(user=self.customer_user)
        artisan = ArtisanProfile.objects.create(
            user=self.artisan_user, profile_status="ACTIVE"
        )
        category = ServiceCategory.objects.create(name="Electrical")
        location = Locations.objects.create(
            country="Nigeria", state="Ogun", area="Abeokuta"
        )
        request = ServiceRequest.objects.create(
            requester=self.customer_user,
            category=category,
            location=location,
            preferred_date=(timezone.now() + timedelta(days=1)).date(),
            description="Repair bathroom wiring.",
        )
        self.booking = Booking.objects.create(
            request=request,
            artisan=artisan,
            customer=customer,
            scheduled_time_start=timezone.now() + timedelta(days=1),
            scheduled_time_end=timezone.now() + timedelta(days=4),
            price_agreed="45000.00",
            status=Booking.Status.ACCEPTED,
            service_duration_value=2,
            service_duration_unit="WEEKS",
        )
        self.payment = EscrowPayment.objects.create(
            booking=self.booking,
            customer=self.customer_user,
            artisan=artisan,
            reference="PAY-COUNTDOWN-1",
            service_amount_kobo=4500000,
            customer_fee_kobo=15000,
            total_amount_kobo=4515000,
            platform_commission_kobo=450000,
            artisan_payout_kobo=4050000,
            status=EscrowPayment.Status.HELD,
        )

    def test_start_without_held_payment_is_rejected(self):
        self.payment.status = EscrowPayment.Status.PAYMENT_PENDING
        self.payment.save(update_fields=["status"])
        self.client.force_authenticate(user=self.artisan_user)
        response = self.client.post(
            f"/api/v1/booking/{self.booking.pk}/start/",
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY="start-unpaid",
        )
        self.assertEqual(response.status_code, 409)
        self.booking.refresh_from_db()
        self.assertIsNone(self.booking.actual_started_at)

    def test_non_assigned_user_cannot_start(self):
        self.client.force_authenticate(user=self.customer_user)
        response = self.client.post(
            f"/api/v1/booking/{self.booking.pk}/start/",
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY="start-wrong-user",
        )
        self.assertEqual(response.status_code, 403)
        self.booking.refresh_from_db()
        self.assertIsNone(self.booking.actual_started_at)

    def test_start_sets_persisted_actual_start_and_countdown_end(self):
        self.client.force_authenticate(user=self.artisan_user)
        before = timezone.now()
        response = self.client.post(
            f"/api/v1/booking/{self.booking.pk}/start/",
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY="start-ok",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, Booking.Status.IN_PROGRESS)
        self.assertIsNotNone(self.booking.actual_started_at)
        expected_end = self.booking.actual_started_at + timedelta(weeks=2)
        self.assertAlmostEqual(
            self.booking.countdown_ends_at.timestamp(),
            expected_end.timestamp(),
            delta=2,
        )
        self.assertGreaterEqual(self.booking.countdown_ends_at, before)
        # counts/timestamps are exposed by the API
        self.assertTrue(response.data["actual_started_at"])
        self.assertTrue(response.data["countdown_ends_at"])
        self.assertEqual(response.data["service_duration_unit"], "WEEKS")
        self.assertEqual(response.data["duration_label"], "2 weeks")

    def test_repeated_start_does_not_reset_the_countdown(self):
        self.client.force_authenticate(user=self.artisan_user)
        first = self.client.post(
            f"/api/v1/booking/{self.booking.pk}/start/",
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY="start-repeat-1",
        )
        self.assertEqual(first.status_code, 200)
        self.booking.refresh_from_db()
        first_started = self.booking.actual_started_at
        first_end = self.booking.countdown_ends_at

        # Fresh idempotency key: still a repeat of the business action.
        second = self.client.post(
            f"/api/v1/booking/{self.booking.pk}/start/",
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY="start-repeat-2",
        )
        self.assertEqual(second.status_code, 200)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.actual_started_at, first_started)
        self.assertEqual(self.booking.countdown_ends_at, first_end)
        self.assertEqual(
            self.booking.status_history.filter(status="IN_PROGRESS").count(), 1
        )
