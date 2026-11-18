from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from booking.models import Booking, BookingAdditionalOffer, BookingAdditionalOrder
from core.models import OutboxEvent
from home.models import ArtisanProfile, CustomerProfile, CustomUser, Locations, ServiceCategory
from payments.flutterwave import FlutterwaveClient
from payments.models import EscrowPayment, EscrowRelease
from payments.receipts import payout_receipt_pdf
from services.models import ServiceRequest


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "additional-order-payment-tests"}})
class AdditionalOrderPaymentTests(TestCase):
    def setUp(self):
        self.api = APIClient()
        self.customer_user = CustomUser.objects.create_user(
            email="addon-pay-customer@example.com", first_name="Nneka", last_name="Customer",
            password="StrongPassword123!", is_active=True,
        )
        artisan_user = CustomUser.objects.create_user(
            email="addon-pay-artisan@example.com", first_name="Chidi", last_name="Artisan",
            password="StrongPassword123!", is_active=True,
        )
        self.artisan = ArtisanProfile.objects.create(user=artisan_user, profile_status="ACTIVE", business_name="Chidi Repairs")
        self.customer = CustomerProfile.objects.create(user=self.customer_user)
        category = ServiceCategory.objects.create(name="Electrical")
        location = Locations.objects.create(country="Nigeria", state="Lagos", area="Surulere")
        request = ServiceRequest.objects.create(
            requester=self.customer_user, category=category, location=location,
            preferred_date=timezone.localdate(), description="Repair an electrical outlet.",
        )
        start = timezone.now() - timedelta(hours=1)
        self.booking = Booking.objects.create(
            request=request, artisan=self.artisan, customer=self.customer,
            scheduled_time_start=start, scheduled_time_end=start + timedelta(hours=2),
            price_agreed="30000.00", status=Booking.Status.IN_PROGRESS,
        )
        self.base_payment = EscrowPayment.objects.create(
            booking=self.booking, customer=self.customer_user, artisan=self.artisan,
            reference="PAY-ADDON-BASE-2", service_amount_kobo=3_000_000,
            total_amount_kobo=3_015_000, platform_commission_kobo=300_000,
            artisan_payout_kobo=2_700_000, status=EscrowPayment.Status.HELD,
        )
        self.order = BookingAdditionalOrder.objects.create(booking=self.booking, created_by=artisan_user, status=BookingAdditionalOrder.Status.APPROVED)
        offer = BookingAdditionalOffer.objects.create(
            order=self.order, revision=1, created_by=artisan_user, amount="8500.00",
            description="Replace the damaged breaker and test the circuit.", status=BookingAdditionalOffer.Status.ACCEPTED,
            accepted_by=self.customer_user,
        )
        self.order.accepted_offer = offer
        self.order.save(update_fields=["accepted_offer", "updated_at"])

    def test_payment_endpoint_prepares_a_real_pending_provider_payment(self):
        self.api.force_authenticate(self.customer_user)
        with patch("payments.views.initialize_prepared_payment.delay") as initialize:
            response = self.api.post(
                f"/api/v1/payments/additional-orders/{self.order.id}/initialize/",
                {},
                format="json",
                HTTP_IDEMPOTENCY_KEY="addon-payment-init-1",
            )
        self.assertEqual(response.status_code, 202, response.data)
        payment = EscrowPayment.objects.get(addon_order=self.order)
        self.assertEqual(payment.status, EscrowPayment.Status.PAYMENT_PENDING)
        self.assertEqual(payment.service_amount_kobo, 850_000)
        self.assertEqual(payment.total_amount_kobo, 865_000)
        self.assertEqual(Decimal(str(self.booking.price_agreed)), Decimal("30000.00"))
        initialize.assert_not_called()  # on_commit hooks are deferred by TestCase's wrapping transaction.

    def test_only_the_customer_can_initialize_an_addon_payment(self):
        self.api.force_authenticate(self.artisan.user)
        response = self.api.post(
            f"/api/v1/payments/additional-orders/{self.order.id}/initialize/",
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY="addon-payment-init-artisan",
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(EscrowPayment.objects.filter(addon_order=self.order).exists())

    def test_flutterwave_checkout_metadata_identifies_the_addon_without_changing_booking(self):
        payment = EscrowPayment.objects.create(
            booking=self.booking, addon_order=self.order, customer=self.customer_user, artisan=self.artisan,
            reference="PAY-ADDON-META", service_amount_kobo=850_000, total_amount_kobo=865_000,
            platform_commission_kobo=85_000, artisan_payout_kobo=765_000,
        )
        captured = {}
        def fake_request(self, method, path, **kwargs):
            captured["payload"] = kwargs["json"]
            return {"status": "success", "data": {"id": 123, "link": "https://checkout.example.test/pay"}}
        with patch.object(FlutterwaveClient, "_request", fake_request):
            FlutterwaveClient().initialize_payment(
                reference=payment.reference, amount_ngn=payment.total_amount_kobo // 100,
                customer=self.customer_user, booking_id=self.booking.id, addon_order_id=self.order.id,
            )
        self.assertEqual(captured["payload"]["meta"]["booking_id"], str(self.booking.id))
        self.assertEqual(captured["payload"]["meta"]["additional_order_id"], str(self.order.id))
        self.assertIn("additional order", captured["payload"]["customizations"]["description"].lower())
        self.assertEqual(Decimal(str(self.booking.price_agreed)), Decimal("30000.00"))

    def test_artisan_success_email_outbox_includes_a_receipt_descriptor(self):
        payment = EscrowPayment.objects.create(
            booking=self.booking, addon_order=self.order, customer=self.customer_user, artisan=self.artisan,
            reference="PAY-ADDON-RECEIPT", service_amount_kobo=850_000, total_amount_kobo=865_000,
            platform_commission_kobo=85_000, artisan_payout_kobo=765_000, status=EscrowPayment.Status.RELEASED,
        )
        release = EscrowRelease.objects.create(
            payment=payment, reference=f"RELEASE-ADDON-{self.order.id}", amount_kobo=765_000,
            status=EscrowRelease.Status.SUCCESSFUL, completed_at=timezone.now(),
        )
        from notifications.events import notify_payout_successful
        notify_payout_successful(payment)
        outbox = OutboxEvent.objects.get(event_key=f"payout_successful:{payment.id}")
        self.assertEqual(outbox.payload["attachments"][0]["kind"], "payout_receipt_pdf")
        self.assertEqual(outbox.payload["attachments"][0]["payment_id"], str(payment.id))

        pdf = payout_receipt_pdf(payment.id)
        self.assertTrue(pdf.startswith(b"%PDF-1.4"))
        self.assertIn(b"ARTISAN MARKETPLACE PAYOUT RECEIPT", pdf)
        self.assertIn(str(release.reference).encode(), pdf)
        self.assertIn(b"Artisan payout: NGN 7,650.00", pdf)
