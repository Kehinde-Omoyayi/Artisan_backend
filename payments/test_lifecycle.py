"""Payment lifecycle tests. Only the Flutterwave HTTP boundary is mocked; the database logic is real."""
import base64
import hashlib
import hmac
import json
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from booking.models import Booking
from home.models import ArtisanProfile, CustomerProfile, CustomUser, Locations, Service, ServiceCategory
from payments.flutterwave import FlutterwaveError, _naira, _provider_id
from payments.models import EscrowPayment, PaymentWebhookEvent
from payments.services import PaymentVerificationPending, prepare_booking_payment, verify_and_hold_payment
from services.models import ServiceRequest

REFERENCE = "PAY-LC-1"


def provider_result(**overrides):
    data = {"id": 777, "tx_ref": REFERENCE, "status": "successful", "currency": "NGN", "amount": 20150}
    data.update(overrides)
    return {"status": "success", "data": data}


class PaymentFixtureMixin:
    def make_world(self, booking_status=Booking.Status.ACCEPTED, with_payment=True):
        self.customer_user = CustomUser.objects.create_user(email="cust@example.com", first_name="Cust", last_name="Omer", password="StrongPassword123!", is_active=True)
        artisan_user = CustomUser.objects.create_user(email="art@example.com", first_name="Art", last_name="Isan", password="StrongPassword123!", is_active=True)
        self.customer = CustomerProfile.objects.create(user=self.customer_user)
        self.artisan = ArtisanProfile.objects.create(user=artisan_user, profile_status="ACTIVE")
        category = ServiceCategory.objects.create(name="Painting")
        Service.objects.create(category=category, name="Wall Painting")
        location = Locations.objects.create(country="Nigeria", state="Ogun", area="Alabata")
        request = ServiceRequest.objects.create(requester=self.customer_user, category=category, location=location, preferred_date=(timezone.now() + timedelta(days=1)).date(), description="Paint two rooms.")
        self.booking = Booking.objects.create(request=request, artisan=self.artisan, customer=self.customer, scheduled_time_start=timezone.now() + timedelta(days=1), scheduled_time_end=timezone.now() + timedelta(days=1, hours=2), price_agreed="20000.00", status=booking_status)
        self.payment = None
        if with_payment:
            self.payment = EscrowPayment.objects.create(booking=self.booking, customer=self.customer_user, artisan=self.artisan, reference=REFERENCE, service_amount_kobo=2000000, total_amount_kobo=2015000, platform_commission_kobo=200000, artisan_payout_kobo=1800000)


class VerifyAndHoldTests(PaymentFixtureMixin, TestCase):
    def setUp(self):
        self.make_world()

    def verify(self, result):
        with patch("payments.services.FlutterwaveClient.verify_transaction", return_value=result):
            return verify_and_hold_payment(self.payment.id, 777)

    def test_confirmed_payment_is_held_exactly_once_and_replay_is_a_noop(self):
        from payments.models import PaymentLedgerEntry as FinancialLedgerEntry
        self.verify(provider_result())
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, EscrowPayment.Status.HELD)
        held_at = self.payment.held_at
        entries = FinancialLedgerEntry.objects.filter(payment=self.payment, event_type="ESCROW_HELD").count()
        self.assertEqual(entries, 1)
        self.verify(provider_result())  # duplicate callback / webhook / Celery redelivery
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.held_at, held_at)
        self.assertEqual(FinancialLedgerEntry.objects.filter(payment=self.payment, event_type="ESCROW_HELD").count(), entries)

    def test_tampered_provider_data_never_holds_the_payment(self):
        for tampered in (provider_result(amount=1), provider_result(tx_ref="PAY-SOMEONE-ELSE"), provider_result(currency="USD"), provider_result(id=999)):
            with self.assertRaises(ValueError):
                self.verify(tampered)
            self.payment.refresh_from_db()
            self.assertEqual(self.payment.status, EscrowPayment.Status.PAYMENT_PENDING)

    def test_unconfirmed_provider_status_stays_pending_for_retry(self):
        with self.assertRaises(PaymentVerificationPending):
            self.verify(provider_result(status="pending"))
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, EscrowPayment.Status.PAYMENT_PENDING)

    def test_failed_provider_status_marks_the_payment_failed(self):
        self.verify(provider_result(status="failed"))
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, EscrowPayment.Status.FAILED)


class OrphanedPaymentTests(PaymentFixtureMixin, TestCase):
    def test_money_confirmed_for_a_cancelled_booking_is_recorded_and_flagged_not_dropped(self):
        self.make_world(booking_status=Booking.Status.CANCELLED)
        with patch("payments.services.FlutterwaveClient.verify_transaction", return_value=provider_result()):
            verify_and_hold_payment(self.payment.id, 777)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, EscrowPayment.Status.HELD)
        self.assertIn("Refund review required", self.payment.failure_reason)
        self.assertIsNotNone(self.payment.held_at)


class FlutterwaveClientSafetyTests(TestCase):
    def test_provider_ids_must_be_numeric_before_entering_a_url_path(self):
        self.assertEqual(_provider_id("12345"), 12345)
        for bad in ("../../transfers", "12 34", "1; DROP", "", "١٢٣", None):
            with self.assertRaises(FlutterwaveError):
                _provider_id(bad)

    def test_naira_amounts_keep_kobo_precision(self):
        from decimal import Decimal
        self.assertEqual(_naira(Decimal("22500")), 22500)
        self.assertEqual(_naira(Decimal("904.50")), 904.5)
        self.assertEqual(_naira(90450 / 100), 904.5)
        self.assertEqual(_naira(Decimal("0.07")), 0.07)

    def test_payout_amount_passed_to_the_provider_is_not_truncated(self):
        from decimal import Decimal
        from payments.flutterwave import FlutterwaveClient
        captured = {}
        def fake_request(self, method, path, **kwargs):
            captured["payload"] = kwargs.get("json")
            return {"status": "success", "data": {}}
        with patch.object(FlutterwaveClient, "_request", fake_request):
            FlutterwaveClient().create_transfer(amount_ngn=Decimal("904.50"), beneficiary_id=5, reference="REL-1", narration="x", callback_url="https://example.com/cb")
        self.assertEqual(captured["payload"]["amount"], 904.5)


@override_settings(FLW_SECRET_HASH="test-secret")
class WebhookAndCallbackTests(PaymentFixtureMixin, TestCase):
    def setUp(self):
        self.make_world()
        self.client = APIClient()

    def post_webhook(self, payload, signature="valid"):
        body = json.dumps(payload).encode()
        headers = {}
        if signature == "valid":
            headers["HTTP_FLUTTERWAVE_SIGNATURE"] = base64.b64encode(hmac.new(b"test-secret", body, hashlib.sha256).digest()).decode()
        elif signature:
            headers["HTTP_FLUTTERWAVE_SIGNATURE"] = signature
        return self.client.post("/api/v1/payments/flutterwave/webhook/", data=body, content_type="application/json", **headers)

    def test_unsigned_and_forged_webhooks_are_rejected_and_change_nothing(self):
        payload = {"event": "charge.completed", "data": {"id": 777, "tx_ref": REFERENCE, "status": "successful"}}
        with patch("payments.views.verify_flutterwave_payment.delay") as delay:
            self.assertEqual(self.post_webhook(payload, signature=None).status_code, 401)
            self.assertEqual(self.post_webhook(payload, signature="Zm9yZ2Vk").status_code, 401)
        delay.assert_not_called()
        self.assertFalse(PaymentWebhookEvent.objects.exists())
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, EscrowPayment.Status.PAYMENT_PENDING)

    def test_a_signed_webhook_only_schedules_server_side_verification_it_never_sets_status(self):
        payload = {"event": "charge.completed", "data": {"id": 777, "tx_ref": REFERENCE, "status": "successful", "amount": 1}}
        with patch("payments.views.verify_flutterwave_payment.delay") as delay, self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.post_webhook(payload).status_code, 200)
        delay.assert_called_once()
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, EscrowPayment.Status.PAYMENT_PENDING)

    def test_a_processed_webhook_replay_is_acknowledged_without_new_work(self):
        payload = {"event": "charge.completed", "data": {"id": 777, "tx_ref": REFERENCE}}
        with patch("payments.views.verify_flutterwave_payment.delay"), self.captureOnCommitCallbacks(execute=True):
            self.post_webhook(payload)
        PaymentWebhookEvent.objects.update(processed_at=timezone.now())
        with patch("payments.views.verify_flutterwave_payment.delay") as delay, self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.post_webhook(payload).status_code, 200)
        delay.assert_not_called()
        self.assertEqual(PaymentWebhookEvent.objects.count(), 1)

    def test_malformed_signed_payloads_are_client_errors_not_500s(self):
        body = b"[1, 2, 3]"
        signature = base64.b64encode(hmac.new(b"test-secret", body, hashlib.sha256).digest()).decode()
        response = self.client.post("/api/v1/payments/flutterwave/webhook/", data=body, content_type="application/json", HTTP_FLUTTERWAVE_SIGNATURE=signature)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.post_webhook({"event": "charge.completed", "data": "oops"}).status_code, 200)

    def test_callback_validates_ids_and_only_schedules_verification(self):
        url = "/api/v1/payments/flutterwave/callback/"
        self.assertEqual(self.client.get(url, {"transaction_id": "../x", "tx_ref": REFERENCE}).status_code, 400)
        self.assertEqual(self.client.get(url, {"tx_ref": REFERENCE}).status_code, 400)
        with patch("payments.views.verify_flutterwave_payment.delay") as delay:
            self.assertEqual(self.client.get(url, {"transaction_id": "777", "tx_ref": REFERENCE}).status_code, 202)
            self.assertEqual(self.client.get(url, {"transaction_id": "777", "tx_ref": "UNKNOWN"}).status_code, 202)
        self.assertEqual(delay.call_count, 1)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, EscrowPayment.Status.PAYMENT_PENDING)

    def test_callback_survives_a_broker_outage(self):
        with patch("payments.views.verify_flutterwave_payment.delay", side_effect=RuntimeError("broker down")):
            response = self.client.get("/api/v1/payments/flutterwave/callback/", {"transaction_id": "777", "tx_ref": REFERENCE})
        self.assertEqual(response.status_code, 202)


class PaymentInitializationTests(PaymentFixtureMixin, TestCase):
    def setUp(self):
        self.make_world(booking_status=Booking.Status.REQUESTED, with_payment=False)
        self.client = APIClient()
        self.client.force_authenticate(self.customer_user)
        self.url = f"/api/v1/payments/booking/{self.booking.id}/initialize/"

    def test_retry_with_the_same_key_after_a_rejected_attempt_works(self):
        with patch("payments.views.initialize_prepared_payment.delay"), self.captureOnCommitCallbacks(execute=True):
            first = self.client.post(self.url, {}, format="json", HTTP_IDEMPOTENCY_KEY="init-1")
            self.assertEqual(first.status_code, 400)  # booking not accepted yet
            Booking.objects.filter(pk=self.booking.pk).update(status=Booking.Status.ACCEPTED)
            second = self.client.post(self.url, {}, format="json", HTTP_IDEMPOTENCY_KEY="init-1")
        self.assertEqual(second.status_code, 202, second.data)
        self.assertEqual(EscrowPayment.objects.filter(booking=self.booking).count(), 1)

    def test_amounts_come_from_the_server_and_client_values_are_ignored(self):
        Booking.objects.filter(pk=self.booking.pk).update(status=Booking.Status.ACCEPTED)
        with patch("payments.views.initialize_prepared_payment.delay"), self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(self.url, {"amount": 1, "total_amount": 1, "service_amount_kobo": 1}, format="json", HTTP_IDEMPOTENCY_KEY="init-2")
        self.assertEqual(response.status_code, 202, response.data)
        payment = EscrowPayment.objects.get(booking=self.booking)
        self.assertEqual(payment.service_amount_kobo, 2000000)
        self.assertEqual(payment.total_amount_kobo, 2015000)

    def test_duplicate_requests_create_one_payment(self):
        Booking.objects.filter(pk=self.booking.pk).update(status=Booking.Status.ACCEPTED)
        with patch("payments.views.initialize_prepared_payment.delay") as delay, self.captureOnCommitCallbacks(execute=True):
            for _ in range(3):
                self.assertEqual(self.client.post(self.url, {}, format="json", HTTP_IDEMPOTENCY_KEY="init-3").status_code, 202)
        self.assertEqual(EscrowPayment.objects.filter(booking=self.booking).count(), 1)
        self.assertEqual(delay.call_count, 1)

    def test_only_the_booking_customer_can_initialize_and_a_key_is_required(self):
        other = CustomUser.objects.create_user(email="other@example.com", first_name="O", last_name="T", password="StrongPassword123!", is_active=True)
        Booking.objects.filter(pk=self.booking.pk).update(status=Booking.Status.ACCEPTED)
        stranger = APIClient()
        stranger.force_authenticate(other)
        self.assertEqual(stranger.post(self.url, {}, format="json", HTTP_IDEMPOTENCY_KEY="x").status_code, 404)
        self.assertEqual(self.client.post(self.url, {}, format="json").status_code, 400)
        artisan_client = APIClient()
        artisan_client.force_authenticate(self.artisan.user)
        self.assertEqual(artisan_client.post(self.url, {}, format="json", HTTP_IDEMPOTENCY_KEY="y").status_code, 404)

    def test_failed_payment_retry_uses_a_fresh_provider_reference(self):
        Booking.objects.filter(pk=self.booking.pk).update(status=Booking.Status.ACCEPTED)
        payment = prepare_booking_payment(booking=self.booking)
        old_reference = payment.reference
        EscrowPayment.objects.filter(pk=payment.pk).update(status=EscrowPayment.Status.FAILED)
        retried = prepare_booking_payment(booking=self.booking)
        self.assertEqual(retried.status, EscrowPayment.Status.PAYMENT_PENDING)
        self.assertNotEqual(retried.reference, old_reference)
