from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from booking.models import Booking, BookingAdditionalOffer, BookingAdditionalOrder
from conversations.models import Conversation
from home.models import ArtisanProfile, CustomerProfile, CustomUser, Locations, ServiceCategory
from payments.models import EscrowPayment, EscrowRelease
from payments.services import prepare_additional_order_payment, verify_and_hold_payment
from services.models import ServiceRequest


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "additional-order-tests"}})
class AdditionalOrderWorkflowTests(TestCase):
    def setUp(self):
        self.api = APIClient()
        self.customer_user = CustomUser.objects.create_user(
            email="addon-customer@example.com",
            first_name="Ada",
            last_name="Customer",
            password="StrongPassword123!",
            is_active=True,
        )
        self.artisan_user = CustomUser.objects.create_user(
            email="addon-artisan@example.com",
            first_name="Tunde",
            last_name="Artisan",
            password="StrongPassword123!",
            is_active=True,
        )
        self.customer = CustomerProfile.objects.create(user=self.customer_user)
        self.artisan = ArtisanProfile.objects.create(user=self.artisan_user, profile_status="ACTIVE")
        category = ServiceCategory.objects.create(name="Plumbing")
        location = Locations.objects.create(country="Nigeria", state="Lagos", area="Yaba")
        service_request = ServiceRequest.objects.create(
            requester=self.customer_user,
            category=category,
            location=location,
            preferred_date=timezone.localdate(),
            description="Repair a leaking kitchen tap.",
        )
        start = timezone.now() - timedelta(hours=2)
        self.booking = Booking.objects.create(
            request=service_request,
            artisan=self.artisan,
            customer=self.customer,
            scheduled_time_start=start,
            scheduled_time_end=start + timedelta(hours=2),
            price_agreed="25000.00",
            status=Booking.Status.IN_PROGRESS,
        )
        self.base_payment = EscrowPayment.objects.create(
            booking=self.booking,
            customer=self.customer_user,
            artisan=self.artisan,
            reference="PAY-ADDON-BASE",
            service_amount_kobo=2_500_000,
            total_amount_kobo=2_515_000,
            platform_commission_kobo=250_000,
            artisan_payout_kobo=2_250_000,
            status=EscrowPayment.Status.HELD,
        )

    def create_addon(self, amount="12000.00", description="Replace the worn valve"):
        self.api.force_authenticate(self.artisan_user)
        response = self.api.post(
            f"/api/v1/booking/{self.booking.id}/additional-orders/",
            {"amount": amount, "description": description},
            format="json",
            HTTP_IDEMPOTENCY_KEY=f"addon-create-{BookingAdditionalOrder.objects.count()}-{description[:8]}",
        )
        self.assertEqual(response.status_code, 201, response.data)
        return BookingAdditionalOrder.objects.get(pk=response.data["id"])

    def approve_addon(self, order):
        offer = order.offers.get(revision=1)
        self.api.force_authenticate(self.customer_user)
        response = self.api.post(
            f"/api/v1/booking/{self.booking.id}/additional-orders/{order.id}/offers/{offer.id}/respond/",
            {"action": "accept"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=f"addon-accept-{order.id}",
        )
        self.assertEqual(response.status_code, 200, response.data)
        order.refresh_from_db()
        return order

    def test_multiple_addon_orders_do_not_change_original_booking_terms(self):
        first = self.create_addon("12000.00", "Replace the worn valve")
        second = self.create_addon("6500.00", "Install an extra shutoff")

        self.booking.refresh_from_db()
        self.assertEqual(Decimal(str(self.booking.price_agreed)), Decimal("25000.00"))
        self.assertEqual(self.booking.additional_orders.count(), 2)
        self.assertEqual(first.offers.get(revision=1).amount, 12_000)
        self.assertEqual(second.offers.get(revision=1).amount, 6_500)
        self.assertEqual(first.status, BookingAdditionalOrder.Status.NEGOTIATING)
        self.assertEqual(second.status, BookingAdditionalOrder.Status.NEGOTIATING)

    def test_counter_history_and_acceptance_are_independent_per_order(self):
        order = self.create_addon()
        initial = order.offers.get(revision=1)
        self.api.force_authenticate(self.customer_user)
        response = self.api.post(
            f"/api/v1/booking/{self.booking.id}/additional-orders/{order.id}/offers/{initial.id}/respond/",
            {"action": "counter", "amount": "14000.00", "description": "Replace the valve and test pressure"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=f"addon-counter-{order.id}",
        )
        self.assertEqual(response.status_code, 200, response.data)
        initial.refresh_from_db()
        order.refresh_from_db()
        counter = order.offers.get(revision=2)
        self.assertEqual(initial.status, BookingAdditionalOffer.Status.COUNTERED)
        self.assertEqual(counter.parent_id, initial.id)
        self.assertEqual(counter.created_by_id, self.customer_user.id)
        self.assertEqual(order.status, BookingAdditionalOrder.Status.NEGOTIATING)

        self.api.force_authenticate(self.artisan_user)
        response = self.api.post(
            f"/api/v1/booking/{self.booking.id}/additional-orders/{order.id}/offers/{counter.id}/respond/",
            {"action": "accept"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=f"addon-accept-counter-{order.id}",
        )
        self.assertEqual(response.status_code, 200, response.data)
        order.refresh_from_db()
        self.assertEqual(order.status, BookingAdditionalOrder.Status.APPROVED)
        self.assertEqual(order.accepted_offer_id, counter.id)
        self.assertEqual(order.accepted_offer.amount, 14_000)
        self.assertEqual(Decimal(str(self.booking.price_agreed)), Decimal("25000.00"))

    def test_conversation_payload_hides_completion_until_addon_escrow_is_confirmed(self):
        conversation = Conversation.objects.create(
            customer=self.customer_user,
            artisan=self.artisan,
            service_request=self.booking.request,
        )
        order = self.create_addon("12000.00", "Replace the worn valve")

        self.api.force_authenticate(self.artisan_user)
        response = self.api.get(f"/api/v1/conversations/{conversation.id}/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data["additional_orders"]), 1)
        self.assertNotIn("SUBMIT_COMPLETION", response.data["available_actions"])
        addon_step = next(step for step in response.data["journey"] if step["key"] == "ADD_ONS")
        self.assertTrue(addon_step["current"])

        self.approve_addon(order)
        addon_payment = prepare_additional_order_payment(order=order, user=self.customer_user)
        self.api.force_authenticate(self.customer_user)
        response = self.api.get(f"/api/v1/conversations/{conversation.id}/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["additional_orders"][0]["can_pay"])
        self.assertNotIn("SUBMIT_COMPLETION", response.data["available_actions"])

        provider_result = {"status": "success", "data": {
            "id": 982, "tx_ref": addon_payment.reference, "status": "successful", "currency": "NGN",
            "amount": addon_payment.total_amount_kobo // 100,
        }}
        with patch("payments.services.FlutterwaveClient.verify_transaction", return_value=provider_result):
            verify_and_hold_payment(addon_payment.id, 982)

        self.api.force_authenticate(self.artisan_user)
        response = self.api.get(f"/api/v1/conversations/{conversation.id}/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn("SUBMIT_COMPLETION", response.data["available_actions"])
        addon_step = next(step for step in response.data["journey"] if step["key"] == "ADD_ONS")
        self.assertFalse(addon_step["current"])
        self.assertTrue(addon_step["complete"])

    def test_customer_cannot_propose_an_addon_and_unrelated_user_cannot_respond(self):
        self.api.force_authenticate(self.customer_user)
        response = self.api.post(
            f"/api/v1/booking/{self.booking.id}/additional-orders/",
            {"amount": "1000", "description": "Extra work"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="customer-addon-create",
        )
        self.assertEqual(response.status_code, 403)

        order = self.create_addon()
        offer = order.offers.get(revision=1)
        outsider = CustomUser.objects.create_user(
            email="addon-outsider@example.com", first_name="Other", last_name="User",
            password="StrongPassword123!", is_active=True,
        )
        self.api.force_authenticate(outsider)
        response = self.api.post(
            f"/api/v1/booking/{self.booking.id}/additional-orders/{order.id}/offers/{offer.id}/respond/",
            {"action": "accept"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="outsider-addon-respond",
        )
        self.assertEqual(response.status_code, 403)

    def test_completion_waits_for_provider_confirmed_addon_payment_and_releases_each_payment(self):
        order = self.create_addon("12000.00", "Replace the worn valve")
        self.approve_addon(order)
        addon_payment = prepare_additional_order_payment(order=order, user=self.customer_user)
        self.assertEqual(addon_payment.status, EscrowPayment.Status.PAYMENT_PENDING)
        self.assertEqual(addon_payment.service_amount_kobo, 1_200_000)
        self.assertEqual(Decimal(str(self.booking.price_agreed)), Decimal("25000.00"))

        self.api.force_authenticate(self.artisan_user)
        response = self.api.post(
            f"/api/v1/booking/{self.booking.id}/complete/",
            {"note": "Main work is complete."},
            format="json",
            HTTP_IDEMPOTENCY_KEY="addon-completion-blocked-pending",
        )
        self.assertEqual(response.status_code, 409)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, Booking.Status.IN_PROGRESS)

        provider_result = {"status": "success", "data": {
            "id": 981, "tx_ref": addon_payment.reference, "status": "successful", "currency": "NGN", "amount": 12150,
        }}
        with patch("payments.services.FlutterwaveClient.verify_transaction", return_value=provider_result):
            verified = verify_and_hold_payment(addon_payment.id, 981)
        self.assertEqual(verified.status, EscrowPayment.Status.HELD)

        response = self.api.post(
            f"/api/v1/booking/{self.booking.id}/complete/",
            {"note": "Main work and approved extra work are complete."},
            format="json",
            HTTP_IDEMPOTENCY_KEY="addon-completion-submitted",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.api.force_authenticate(self.customer_user)
        response = self.api.post(
            f"/api/v1/booking/{self.booking.id}/confirm-completion/",
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY="addon-customer-confirmed",
        )
        self.assertEqual(response.status_code, 200, response.data)

        self.booking.refresh_from_db()
        self.base_payment.refresh_from_db()
        addon_payment.refresh_from_db()
        self.assertEqual(self.booking.status, Booking.Status.COMPLETED)
        self.assertEqual(self.base_payment.status, EscrowPayment.Status.RELEASE_PENDING)
        self.assertEqual(addon_payment.status, EscrowPayment.Status.RELEASE_PENDING)
        self.assertEqual(EscrowRelease.objects.get(payment=self.base_payment).reference, f"RELEASE-{self.booking.id}")
        self.assertEqual(EscrowRelease.objects.get(payment=addon_payment).reference, f"RELEASE-ADDON-{order.id}")
