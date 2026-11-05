import base64
import hashlib
import hmac
import json
from unittest.mock import patch

from django.core.cache import cache
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient, APIRequestFactory
from django.utils import timezone
from datetime import timedelta

from booking.models import Booking
from home.models import ArtisanProfile, CustomUser, CustomerProfile, Locations, ServiceCategory, Service
from services.models import ServiceRequest
from .models import ArtisanPayoutAccount, EscrowPayment, EscrowRelease
from .serializers import ArtisanPayoutAccountSerializer
from .flutterwave import FlutterwaveClient, FlutterwaveUnknownError
from .calculations import calculate_booking_money
from .ledger import assert_ledger_entry_balanced, assert_payment_ledger_consistent, record_financial_event
from .views import _valid_flutterwave_signature


class PaymentWebhookTests(TestCase):
    @override_settings(FLW_SECRET_HASH="test-secret")
    def test_current_flutterwave_hmac_signature_is_valid(self):
        body = json.dumps({"event": "transfer.completed", "data": {"id": "1"}}).encode()
        signature = base64.b64encode(hmac.new(b"test-secret", body, hashlib.sha256).digest()).decode()
        request = RequestFactory().post("/webhook/", data=body, content_type="application/json")
        request.META["HTTP_FLUTTERWAVE_SIGNATURE"] = signature
        self.assertTrue(_valid_flutterwave_signature(request))

    @override_settings(FLW_SECRET_HASH="test-secret")
    def test_legacy_flutterwave_verif_hash_is_supported(self):
        request = RequestFactory().post("/webhook/", data=b"{}", content_type="application/json")
        request.META["HTTP_VERIF_HASH"] = "test-secret"
        self.assertTrue(_valid_flutterwave_signature(request))

    @override_settings(FLW_SECRET_HASH="test-secret")
    def test_invalid_webhook_signature_is_rejected(self):
        body = b'{"event":"charge.completed","data":{"id":"1"}}'
        request = RequestFactory().post("/webhook/", data=body, content_type="application/json")
        request.META["HTTP_FLUTTERWAVE_SIGNATURE"] = "invalid"
        self.assertFalse(_valid_flutterwave_signature(request))


class PaymentStateTests(TestCase):
    def test_release_reference_is_stable_for_reconciliation(self):
        customer_user = CustomUser.objects.create_user(
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
        customer = CustomerProfile.objects.create(user=customer_user)
        artisan = ArtisanProfile.objects.create(user=artisan_user)
        category = ServiceCategory.objects.create(name="Electrical")
        Service.objects.create(category=category, name="Socket Repair")
        location = Locations.objects.create(country="Nigeria", state="FCT", area="Abuja")
        request = ServiceRequest.objects.create(
            requester=customer_user,
            category=category,
            location=location,
            preferred_date=(timezone.now() + timedelta(days=1)).date(),
            description="Repair socket.",
        )
        booking = Booking.objects.create(
            request=request,
            artisan=artisan,
            customer=customer,
            scheduled_time_start=timezone.now(),
            scheduled_time_end=timezone.now() + timedelta(hours=2),
            price_agreed="10000.00",
            status=Booking.Status.COMPLETED,
        )
        payment = EscrowPayment.objects.create(
            booking=booking,
            customer=customer_user,
            artisan=artisan,
            reference="PAY-REF-1",
            service_amount_kobo=1000000,
            total_amount_kobo=1015000,
            platform_commission_kobo=100000,
            artisan_payout_kobo=900000,
        )
        release = EscrowRelease.objects.create(
            payment=payment,
            reference=f"RELEASE-{booking.id}",
            amount_kobo=payment.artisan_payout_kobo,
        )
        self.assertEqual(release.reference, f"RELEASE-{booking.id}")
        self.assertTrue(assert_ledger_entry_balanced(record_financial_event(payment=payment, event_type="ESCROW_HELD", event_key=f"payment:{payment.id}:ledger-held", status_before=EscrowPayment.Status.PAYMENT_PENDING, status_after=EscrowPayment.Status.HELD, amount_kobo=payment.total_amount_kobo)))
        self.assertTrue(assert_payment_ledger_consistent(payment))


class FlutterwaveClientRefundTests(TestCase):
    def test_refund_transaction_sends_integer_ngn_amount(self):
        client = FlutterwaveClient()
        with patch.object(client, "_request", return_value={"status": "success", "data": {}}) as request:
            client.refund_transaction(
                transaction_id="12345",
                amount_kobo=1015000,
                comments="Approved support refund",
                callback_url="https://example.com/refund-callback",
            )

        request.assert_called_once_with(
            "POST",
            "/transactions/12345/refund",
            json={
                "amount": 10150,
                "comments": "Approved support refund",
                "callbackurl": "https://example.com/refund-callback",
            },
        )

    def test_find_refund_returns_only_exact_amount_and_reference_match(self):
        client = FlutterwaveClient()
        payload = {
            "status": "success",
            "data": [
                {"id": 1, "tx_id": "12345", "flw_ref": "wrong", "amount_refunded": 5000, "status": "completed"},
                {"id": 2, "tx_id": "12345", "flw_ref": "right", "amount_refunded": 10150, "status": "completed"},
            ],
        }
        with patch.object(client, "_request", return_value=payload):
            result = client.find_refund(
                transaction_id="12345",
                provider_ref="right",
                amount_kobo=1015000,
            )

        self.assertEqual(result["data"]["id"], 2)


PAYOUT_TEST_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "payments-payout-security-tests",
    }
}


@override_settings(CACHES=PAYOUT_TEST_CACHES)
class PayoutAccountSecurityTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.artisan_user = CustomUser.objects.create_user(
            email="payout-artisan@example.com",
            first_name="Payout",
            last_name="Artisan",
            password="StrongPassword123!",
            is_active=True,
        )
        self.artisan = ArtisanProfile.objects.create(
            user=self.artisan_user,
            profile_status="ACTIVE",
        )
        from services.kyc_crypto import encrypt_identity_name
        from services.models import ArtisanVerification
        ArtisanVerification.objects.create(
            artisan=self.artisan,
            verification_status="VERIFIED",
            verification_method="NIN",
            verification_tier=2,
            verified_identity_name_encrypted=encrypt_identity_name({
                "first_name": "Payout",
                "middle_name": "",
                "last_name": "Artisan",
            }),
        )
        self.account = ArtisanPayoutAccount.objects.create(
            artisan=self.artisan,
            bank_code="058",
            bank_name="Provider Bank",
            account_number_last4="1234",
            account_name="Payout Artisan",
            provider_beneficiary_id=123456,
            provider_subaccount_id="sub_123",
            status=ArtisanPayoutAccount.Status.VERIFIED,
            is_active=True,
            is_verified=True,
        )
        self.client.force_authenticate(self.artisan_user)

    def test_only_account_name_bank_and_full_account_number_are_accepted(self):
        serializer = ArtisanPayoutAccountSerializer(
            self.account,
            data={
                "account_name": "Payout Artisan",
                "bank_code": "058",
                "account_number": "1234567890",
                "provider_beneficiary_id": 999999,
            },
            partial=True,
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("provider_beneficiary_id", serializer.errors)

        response = self.client.put(
            "/api/v1/payments/payout-account/",
            {
                "account_name": "Payout Artisan",
                "bank_code": "058",
                "account_number": "1234567890",
                "provider_beneficiary_id": 999999,
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="payout-routing-1",
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.account.refresh_from_db()
        self.assertEqual(self.account.provider_beneficiary_id, 123456)

    def test_provider_checked_account_update_does_not_require_or_accept_password(self):
        with (
            patch("payments.views.get_supported_nigerian_banks", return_value=[{"code": "058", "name": "Provider Bank"}]),
            patch("payments.views.FlutterwaveClient") as provider,
        ):
            provider.return_value.resolve_account.return_value = {
                "status": "success",
                "data": {"account_number": "1234567890", "account_name": "ARTISAN PAYOUT"},
            }
            response = self.client.patch(
                "/api/v1/payments/payout-account/",
                {
                    "account_name": "Payout Artisan",
                    "bank_code": "058",
                    "account_number": "1234567890",
                },
                format="json",
                HTTP_IDEMPOTENCY_KEY="payout-account-update-1",
            )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotIn("1234567890", str(response.data))
        self.assertEqual(response.data["account_number_last4"], "7890")
        provider.return_value.resolve_account.assert_called_once_with(
            account_number="1234567890",
            bank_code="058",
        )
        self.account.refresh_from_db()
        self.assertEqual(self.account.status, ArtisanPayoutAccount.Status.PENDING)
        self.assertFalse(self.account.is_active)
        self.assertFalse(self.account.is_verified)
        self.assertIsNone(self.account.provider_beneficiary_id)
        from services.kyc_crypto import decrypt_value
        self.assertEqual(decrypt_value(self.account.account_number_encrypted), "1234567890")


class FlutterwaveUnknownOutcomeTests(TestCase):
    def test_server_error_is_treated_as_unknown_outcome(self):
        class Response:
            status_code = 500
            ok = False
            def json(self):
                return {"status": "error", "message": "provider failure"}
        client = FlutterwaveClient()
        with patch.object(client.session, "request", return_value=Response()):
            with self.assertRaises(FlutterwaveUnknownError):
                client._request("POST", "/payments", json={"tx_ref": "PAY-TEST"})

    def test_boolean_service_amount_is_not_accepted_as_an_integer(self):
        with self.assertRaises(ValueError):
            calculate_booking_money(True)


class CheckoutMethodRestrictionTests(TestCase):
    """Escrow is collected by card or bank transfer. The hosted page otherwise
    falls back to every method on the Flutterwave account, PayPal included, so
    both initialize calls must name the allowed methods in the payload."""

    def _capture(self):
        captured = {}

        def fake_request(self, method, path, **kwargs):
            captured["path"] = path
            captured["payload"] = kwargs["json"]
            return {"status": "success", "data": {"id": 1, "link": "https://checkout.example.test/pay"}}

        return captured, patch.object(FlutterwaveClient, "_request", fake_request)

    def test_booking_checkout_offers_card_and_bank_transfer_only(self):
        from types import SimpleNamespace

        customer = SimpleNamespace(email="buyer@example.test", get_full_name=lambda: "Buyer One")
        captured, patcher = self._capture()
        with patcher:
            FlutterwaveClient().initialize_payment(
                reference="PAY-METHODS-1", amount_ngn=25_000, customer=customer, booking_id="b-1"
            )
        self.assertEqual(captured["path"], "/payments")
        self.assertEqual(captured["payload"]["payment_options"], "card, banktransfer")

    def test_promotion_checkout_offers_card_and_bank_transfer_only(self):
        from types import SimpleNamespace

        customer = SimpleNamespace(email="buyer@example.test", get_full_name=lambda: "Buyer One")
        captured, patcher = self._capture()
        with patcher:
            FlutterwaveClient().initialize_promotion_payment(
                reference="PAY-METHODS-2", amount_ngn=5_000, customer=customer,
                promotion_payment_id="p-1", campaign_id="c-1", promotion_type="boost",
                target_key="artisan:1", target_label="Ada the plumber",
            )
        self.assertEqual(captured["payload"]["payment_options"], "card, banktransfer")

    def test_manager_configuration_cannot_reintroduce_unsupported_methods(self):
        from manager.config_spec import normalise

        self.assertEqual(normalise("platform.payment_methods", ["CARD", "BANK_TRANSFER"]), ["BANK_TRANSFER", "CARD"])
        self.assertEqual(normalise("platform.payment_methods", ["PAYPAL", "CARD"]), ["CARD"])
        with self.assertRaises(Exception):
            normalise("platform.payment_methods", ["PAYPAL"])


class CheckoutLinkLifecycleTests(TestCase):
    """A stored checkout is only reused while it is alive and minted for the
    methods this build offers. Otherwise the payer would be sent back to the
    PayPal page Flutterwave minted the first time round."""

    def setUp(self):
        self.customer_user = CustomUser.objects.create_user(
            email="payer@example.com", first_name="Kehinde", last_name="Omoyayi",
            password="StrongPassword123!", is_active=True,
        )
        self.artisan_user = CustomUser.objects.create_user(
            email="worker@example.com", first_name="Ada", last_name="Obi",
            password="StrongPassword123!", is_active=True,
        )
        self.customer = CustomerProfile.objects.create(user=self.customer_user)
        self.artisan = ArtisanProfile.objects.create(user=self.artisan_user)
        self.category = ServiceCategory.objects.create(name="Repairs")
        Service.objects.create(category=self.category, name="Socket Repair")
        location = Locations.objects.create(country="Nigeria", state="Lagos", area="Lekki")
        request = ServiceRequest.objects.create(
            requester=self.customer_user, category=self.category, location=location,
            preferred_date=(timezone.now() + timedelta(days=1)).date(),
            description="Sockets spark.",
        )
        self.booking = Booking.objects.create(
            request=request, artisan=self.artisan, customer=self.customer,
            scheduled_time_start=timezone.now() + timedelta(days=1),
            scheduled_time_end=timezone.now() + timedelta(days=1, hours=2),
            price_agreed="25000.00", status=Booking.Status.ACCEPTED,
        )

    def _payment(self, *, payload=None, transaction_id="987654", reference="PAY-LINK-1"):
        return EscrowPayment.objects.create(
            booking=self.booking, customer=self.customer_user, artisan=self.artisan,
            reference=reference, provider_transaction_id=transaction_id,
            provider_reference=reference, provider_payload=payload or {},
            service_amount_kobo=2_500_000, total_amount_kobo=2_515_000,
            platform_commission_kobo=250_000, artisan_payout_kobo=2_250_000,
        )

    def _fresh_payload(self):
        return {
            "status": "success",
            "data": {"id": 987654, "link": "https://checkout.flutterwave.com/v3/hosted/pay/fresh"},
            "_checkout": {"minted_at": timezone.now().isoformat(), "methods": "card, banktransfer"},
        }

    def _old_payload(self):
        return {
            "status": "success",
            "data": {"id": 987654, "link": "https://checkout.flutterwave.com/v3/hosted/pay/old"},
            "_checkout": {
                "minted_at": (timezone.now() - timedelta(minutes=40)).isoformat(),
                "methods": "card, banktransfer",
            },
        }

    def test_a_live_link_minted_for_these_methods_is_reused(self):
        from payments.services import prepare_booking_payment

        payment = self._payment(payload=self._fresh_payload())
        again = prepare_booking_payment(booking=self.booking)
        self.assertEqual(again.id, payment.id)
        self.assertEqual(again.reference, "PAY-LINK-1")
        self.assertEqual(again.provider_transaction_id, "987654")

    def test_an_expired_link_is_retired_and_minted_again(self):
        from payments.services import prepare_booking_payment

        payment = self._payment(payload=self._old_payload())
        rotated = prepare_booking_payment(booking=self.booking)
        self.assertNotEqual(rotated.reference, "PAY-LINK-1")
        self.assertIn("PAY-", rotated.reference)
        self.assertIsNone(rotated.provider_transaction_id)
        self.assertEqual(rotated.provider_payload, {})

    def test_a_link_minted_before_the_card_only_rule_is_retired(self):
        """The reported case: a PayPal link created by the old payload."""
        from payments.services import prepare_booking_payment

        legacy = {"status": "success", "data": {"id": 987654, "link": "https://checkout.flutterwave.com/v3/hosted/pay/paypal"}}
        payment = self._payment(payload=legacy)
        rotated = prepare_booking_payment(booking=self.booking)
        self.assertNotEqual(rotated.reference, payment.reference)
        self.assertIsNone(rotated.provider_transaction_id)

    def test_the_replacement_checkout_names_card_and_bank_transfer(self):
        from payments.services import _initialize_prepared_payment_external, prepare_booking_payment

        payment = self._payment(payload=self._old_payload())
        prepared = prepare_booking_payment(booking=self.booking)
        captured = {}

        def fake_request(self, method, path, **kwargs):
            captured["payload"] = kwargs["json"]
            return {"status": "success", "data": {"id": 555, "link": "https://checkout.flutterwave.com/v3/hosted/pay/new"}}

        with patch.object(FlutterwaveClient, "_request", fake_request):
            refreshed, _ = _initialize_prepared_payment_external(prepared)

        self.assertEqual(captured["payload"]["payment_options"], "card, banktransfer")
        self.assertIsNot(refreshed.id, None)
        self.assertEqual(refreshed.provider_payload["_checkout"]["methods"], "card, banktransfer")
        self.assertEqual(refreshed.provider_transaction_id, "555")

    def test_a_dead_link_is_never_shown_to_the_payer(self):
        from payments.serializers import PaymentSerializer
        from django.test import RequestFactory

        payment = self._payment(payload=self._old_payload())
        request = RequestFactory().get("/payments/")
        request.user = self.customer_user
        self.assertIsNone(PaymentSerializer(payment, context={"request": request}).data["authorization_url"])

        payment.provider_payload = self._fresh_payload()
        payment.save(update_fields=["provider_payload"])
        self.assertEqual(
            PaymentSerializer(payment, context={"request": request}).data["authorization_url"],
            "https://checkout.flutterwave.com/v3/hosted/pay/fresh",
        )


class PaymentFailureReasonTests(TestCase):
    """A refused checkout must say why, to the person who can fix it."""

    def setUp(self):
        self.customer_user = CustomUser.objects.create_user(
            email="failed-payer@example.com", first_name="Kehinde", last_name="Omoyayi",
            password="StrongPassword123!", is_active=True,
        )
        self.artisan_user = CustomUser.objects.create_user(
            email="failed-worker@example.com", first_name="Ada", last_name="Obi",
            password="StrongPassword123!", is_active=True,
        )
        self.customer = CustomerProfile.objects.create(user=self.customer_user)
        self.artisan = ArtisanProfile.objects.create(user=self.artisan_user)
        category = ServiceCategory.objects.create(name="Repairs")
        Service.objects.create(category=category, name="Socket Repair")
        location = Locations.objects.create(country="Nigeria", state="Lagos", area="Ikeja")
        request = ServiceRequest.objects.create(
            requester=self.customer_user, category=category, location=location,
            preferred_date=(timezone.now() + timedelta(days=1)).date(),
            description="Sockets spark.",
        )
        booking = Booking.objects.create(
            request=request, artisan=self.artisan, customer=self.customer,
            scheduled_time_start=timezone.now() + timedelta(days=1),
            scheduled_time_end=timezone.now() + timedelta(days=1, hours=2),
            price_agreed="25000.00", status=Booking.Status.ACCEPTED,
        )
        self.payment = EscrowPayment.objects.create(
            booking=booking, customer=self.customer_user, artisan=self.artisan,
            reference="PAY-FAILED-1", status=EscrowPayment.Status.FAILED,
            failure_reason="Redirect URL is not a valid absolute URL.",
            service_amount_kobo=2_500_000, total_amount_kobo=2_515_000,
            platform_commission_kobo=250_000, artisan_payout_kobo=2_250_000,
        )

    def test_the_payer_is_told_what_the_provider_said(self):
        client = APIClient()
        client.force_authenticate(user=self.customer_user)
        response = client.get(f"/api/v1/payments/{self.payment.id}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["failure_reason"], "Redirect URL is not a valid absolute URL.")

    def test_the_artisan_is_not_shown_the_payout_pager_internals(self):
        client = APIClient()
        client.force_authenticate(user=self.artisan_user)
        response = client.get(f"/api/v1/payments/{self.payment.id}/")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data["failure_reason"])


class CheckoutConfigCheckTests(SimpleTestCase):
    """runserver prints these; a payer never has to discover them the hard way."""

    def test_a_placeholder_redirect_url_is_reported(self):
        from payments.checks import checkout_urls

        with override_settings(
            ENVIRONMENT="development",
            FLW_REDIRECT_URL="https://<api-8000>.ngrok-free.app/api/v1/payments/flutterwave/callback/",
            SITE_URL="https://<web-8080>.ngrok-free.app",
        ):
            problems = checkout_urls(None)
        self.assertEqual(len(problems), 2)
        self.assertTrue(all(item.id == "payments.W001" for item in problems))

    def test_localhost_redirect_is_reported(self):
        from payments.checks import checkout_urls

        with override_settings(
            ENVIRONMENT="development",
            FLW_REDIRECT_URL="http://127.0.0.1:8000/api/v1/payments/flutterwave/callback/",
            SITE_URL="http://localhost:8080",
        ):
            problems = checkout_urls(None)
        self.assertEqual(len(problems), 2)
        self.assertTrue(all(item.id == "payments.W002" for item in problems))

    def test_a_real_tunnel_is_quiet(self):
        from payments.checks import checkout_urls

        with override_settings(
            ENVIRONMENT="development",
            FLW_REDIRECT_URL="https://1a2b-3c4d.ngrok-free.app/api/v1/payments/flutterwave/callback/",
            SITE_URL="https://9z8y-7x6w.ngrok-free.app",
        ):
            self.assertEqual(checkout_urls(None), [])


class FlutterwaveCallbackIdentityTests(TestCase):
    """The callback has to say which payment it belongs to.

    It used to answer only "received", which meant a web frontend relaying the
    call (single ngrok tunnel, no public API) had no id to put the browser on
    the right return page — the payer finished paying and landed nowhere.
    """

    def setUp(self):
        cache.clear()
        self.customer_user = CustomUser.objects.create_user(
            email="callback@example.com", first_name="Kehinde", last_name="Omoyayi",
            password="StrongPassword123!", is_active=True,
        )
        self.artisan_user = CustomUser.objects.create_user(
            email="callback-worker@example.com", first_name="Ada", last_name="Obi",
            password="StrongPassword123!", is_active=True,
        )
        self.customer = CustomerProfile.objects.create(user=self.customer_user)
        self.artisan = ArtisanProfile.objects.create(user=self.artisan_user)
        self.category = ServiceCategory.objects.create(name="Callback Repairs")
        Service.objects.create(category=self.category, name="Callback Socket Repair")
        location = Locations.objects.create(country="Nigeria", state="Lagos", area="Yaba")
        request = ServiceRequest.objects.create(
            requester=self.customer_user, category=self.category, location=location,
            preferred_date=(timezone.now() + timedelta(days=1)).date(),
            description="Sockets spark.",
        )
        self.booking = Booking.objects.create(
            request=request, artisan=self.artisan, customer=self.customer,
            scheduled_time_start=timezone.now() + timedelta(days=1),
            scheduled_time_end=timezone.now() + timedelta(days=1, hours=2),
            price_agreed="25000.00", status=Booking.Status.ACCEPTED,
        )

    def _payment(self, *, reference, transaction_id=None):
        return EscrowPayment.objects.create(
            booking=self.booking, customer=self.customer_user, artisan=self.artisan,
            reference=reference, provider_transaction_id=transaction_id,
            provider_reference=reference, provider_payload={},
            service_amount_kobo=2_500_000, total_amount_kobo=2_515_000,
            platform_commission_kobo=250_000, artisan_payout_kobo=2_250_000,
        )

    def test_a_matching_callback_names_the_payment_it_belongs_to(self):
        payment = self._payment(reference="PAY-CB-1")
        client = APIClient()
        with patch("payments.views.verify_flutterwave_payment") as verify:
            response = client.get(
                "/api/v1/payments/flutterwave/callback/",
                {"tx_ref": "PAY-CB-1", "transaction_id": "123456"},
            )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json().get("payment_id"), str(payment.id))
        verify.delay.assert_called_once_with(str(payment.id), "123456")

    def test_a_callback_with_an_unknown_reference_names_no_payment(self):
        client = APIClient()
        response = client.get(
            "/api/v1/payments/flutterwave/callback/",
            {"tx_ref": "PAY-NOT-OURS", "transaction_id": "123456"},
        )
        self.assertEqual(response.status_code, 202)
        self.assertNotIn("payment_id", response.json())

    def test_a_reused_reference_with_another_transaction_never_re_verifies(self):
        payment = self._payment(reference="PAY-CB-2", transaction_id="987654")
        client = APIClient()
        with patch("payments.views.verify_flutterwave_payment") as verify:
            response = client.get(
                "/api/v1/payments/flutterwave/callback/",
                {"tx_ref": "PAY-CB-2", "transaction_id": "111111"},
            )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json().get("payment_id"), str(payment.id))
        verify.delay.assert_not_called()


class CheckoutVisibilityAfterInitializationTests(TestCase):
    """A payment that has just been minted must be visible to its payer at once.

    The chat polls the payment record and only offers the link when the API
    reports state LIVE. If the record is written without the methods marker the
    state reads OLD_METHODS and the payer waits forever beside a link that
    already exists.
    """

    def setUp(self):
        cache.clear()
        self.customer_user = CustomUser.objects.create_user(
            email="live@example.com", first_name="Kehinde", last_name="Omoyayi",
            password="StrongPassword123!", is_active=True,
        )
        artisan_user = CustomUser.objects.create_user(
            email="live-worker@example.com", first_name="Ada", last_name="Obi",
            password="StrongPassword123!", is_active=True,
        )
        customer = CustomerProfile.objects.create(user=self.customer_user)
        artisan = ArtisanProfile.objects.create(user=artisan_user)
        category = ServiceCategory.objects.create(name="Live Repairs")
        Service.objects.create(category=category, name="Live Socket Repair")
        location = Locations.objects.create(country="Nigeria", state="Lagos", area="Ikeja")
        request = ServiceRequest.objects.create(
            requester=self.customer_user, category=category, location=location,
            preferred_date=(timezone.now() + timedelta(days=1)).date(),
            description="Sockets spark.",
        )
        self.booking = Booking.objects.create(
            request=request, artisan=artisan, customer=customer,
            scheduled_time_start=timezone.now() + timedelta(days=1),
            scheduled_time_end=timezone.now() + timedelta(days=1, hours=2),
            price_agreed="25000.00", status=Booking.Status.ACCEPTED,
        )
        self.payment = EscrowPayment.objects.create(
            booking=self.booking, customer=self.customer_user, artisan=artisan,
            reference="PAY-LIVE-1", service_amount_kobo=2_500_000,
            total_amount_kobo=2_515_000, platform_commission_kobo=250_000,
            artisan_payout_kobo=2_250_000,
        )

    @patch("payments.services.FlutterwaveClient")
    def test_a_payment_that_just_minted_reads_as_live(self, client_class):
        link = "https://checkout.flutterwave.com/v3/hosted/pay/flwlnk-abc"
        client_class.return_value.initialize_payment.return_value = {
            "status": "success",
            "data": {"id": 424242, "link": link},
        }
        from payments.services import _initialize_prepared_payment_external

        payment, _ = _initialize_prepared_payment_external(self.payment)

        api = APIClient()
        api.force_authenticate(user=self.customer_user)
        data = api.get(f"/api/v1/payments/{payment.id}/").json()
        self.assertEqual(data["checkout"]["state"], "LIVE")
        self.assertEqual(data["checkout"]["methods"], "card, banktransfer")
        self.assertEqual(data["authorization_url"], link)


class CheckoutMethodPayloadTests(TestCase):
    """What the provider is actually told to offer.

    Flutterwave's hosted page falls back to every method enabled on the merchant
    account — PayPal included — when the payload does not name the allowed ones,
    or when the list is not in the documented "comma + space" form. Escrow is
    card or bank transfer, so the exact string is pinned here.
    """

    def test_the_provider_is_asked_for_card_and_bank_transfer_only(self):
        from payments.flutterwave import PAYMENT_OPTIONS

        self.assertEqual(PAYMENT_OPTIONS, "card, banktransfer")

    def test_the_initialize_payload_carries_that_string(self):
        from payments.flutterwave import FlutterwaveClient

        class Customer:
            email = "payer@example.com"

            def get_full_name(self):
                return "Kehinde Omoyayi"

        with patch.object(FlutterwaveClient, "_request", return_value={"status": "success"}) as request:
            FlutterwaveClient().initialize_payment(
                reference="PAY-OPT-1", amount_ngn=251, customer=Customer(), booking_id="b-1"
            )
        payload = request.call_args.kwargs["json"]
        self.assertEqual(payload["payment_options"], "card, banktransfer")
        self.assertEqual(payload["country"], "NG")
        self.assertEqual(payload["currency"], "NGN")


class BankTransferInChatTests(TestCase):
    """Paying by bank transfer without ever leaving the chat.

    The provider hands over a dedicated virtual account; the payer copies it
    from a panel in the conversation. Nothing here opens a hosted page.
    """

    def setUp(self):
        cache.clear()
        self.customer_user = CustomUser.objects.create_user(
            email="transfer@example.com", first_name="Kehinde", last_name="Omoyayi",
            password="StrongPassword123!", is_active=True,
        )
        artisan_user = CustomUser.objects.create_user(
            email="transfer-worker@example.com", first_name="Ada", last_name="Obi",
            password="StrongPassword123!", is_active=True,
        )
        customer = CustomerProfile.objects.create(user=self.customer_user)
        artisan = ArtisanProfile.objects.create(user=artisan_user)
        category = ServiceCategory.objects.create(name="Transfer Repairs")
        Service.objects.create(category=category, name="Transfer Socket Repair")
        location = Locations.objects.create(country="Nigeria", state="Lagos", area="Surulere")
        request = ServiceRequest.objects.create(
            requester=self.customer_user, category=category, location=location,
            preferred_date=(timezone.now() + timedelta(days=1)).date(),
            description="Sockets spark.",
        )
        self.booking = Booking.objects.create(
            request=request, artisan=artisan, customer=customer,
            scheduled_time_start=timezone.now() + timedelta(days=1),
            scheduled_time_end=timezone.now() + timedelta(days=1, hours=2),
            price_agreed="25150.00", status=Booking.Status.ACCEPTED,
        )
        self.payment = EscrowPayment.objects.create(
            booking=self.booking, customer=self.customer_user, artisan=artisan,
            reference="PAY-TRANSFER-1", service_amount_kobo=2_500_000,
            total_amount_kobo=2_515_000, platform_commission_kobo=250_000,
            artisan_payout_kobo=2_250_000,
        )

    @staticmethod
    def _provider_answer():
        return {
            "status": "success",
            "message": "Charge initiated",
            "meta": {
                "authorization": {
                    "transfer_reference": "ZZSS4548178111081675127069",
                    "transfer_account": "9755152912",
                    "transfer_bank": "Flutterwave MFB",
                    "account_expiration": "2026-06-10 19:00:16",
                    "transfer_note": "Please make a bank transfer to Kehinde Omoyayi",
                    "transfer_amount": "25150.00",
                    "mode": "banktransfer",
                }
            },
        }

    def _api(self):
        client = APIClient()
        client.force_authenticate(user=self.customer_user)
        return client

    @patch("payments.services.FlutterwaveClient")
    def test_the_payer_is_given_the_account_to_transfer_into(self, client_class):
        client_class.return_value.create_bank_transfer_charge.return_value = self._provider_answer()

        response = self._api().post(f"/api/v1/payments/{self.payment.id}/bank-transfer/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["account_number"], "9755152912")
        self.assertEqual(response.json()["bank"], "Flutterwave MFB")
        self.assertEqual(response.json()["amount"], "25150.00")
        # The beneficiary line is not decoration: the payer checks the name
        # before sending money, so it has to be in the answer.
        self.assertEqual(response.json()["account_name"], "Kehinde Omoyayi")

    @patch("payments.services.FlutterwaveClient")
    def test_the_same_account_is_returned_instead_of_minting_a_second_one(self, client_class):
        client_class.return_value.create_bank_transfer_charge.return_value = self._provider_answer()
        api = self._api()

        first = api.post(f"/api/v1/payments/{self.payment.id}/bank-transfer/").json()
        second = api.post(f"/api/v1/payments/{self.payment.id}/bank-transfer/").json()

        self.assertEqual(first["account_number"], second["account_number"])
        self.assertEqual(
            client_class.return_value.create_bank_transfer_charge.call_count, 1
        )

    @patch("payments.services.FlutterwaveClient")
    def test_a_stranger_cannot_get_the_account(self, client_class):
        other = CustomUser.objects.create_user(
            email="nosy@example.com", first_name="No", last_name="Sy",
            password="StrongPassword123!", is_active=True,
        )
        api = APIClient()
        api.force_authenticate(user=other)

        response = api.post(f"/api/v1/payments/{self.payment.id}/bank-transfer/")

        self.assertEqual(response.status_code, 404)
        client_class.return_value.create_bank_transfer_charge.assert_not_called()

    @patch("payments.services.FlutterwaveClient")
    def test_money_is_recorded_from_the_reference_alone(self, client_class):
        """No transaction id exists until the transfer lands: the reference is enough."""
        client_class.return_value.verify_transaction_by_reference.return_value = {
            "status": "success",
            "data": {
                "id": 2028146660,
                "tx_ref": "PAY-TRANSFER-1",
                "status": "successful",
                "currency": "NGN",
                "amount": 25150,
            },
        }
        from payments.services import verify_and_hold_payment

        payment = verify_and_hold_payment(str(self.payment.id))

        self.assertEqual(payment.status, EscrowPayment.Status.HELD)
        self.assertEqual(payment.provider_transaction_id, "2028146660")

    @patch("payments.services.FlutterwaveClient")
    def test_a_settled_transfer_settles_itself_while_the_payer_watches(self, client_class):
        """The money is in -- nobody has to press "I have made this bank transfer".

        The chat polls this payment record, so the poll is where the provider
        gets asked. This is what makes the "Payment is the next step" strip
        disappear by itself after a real transfer.
        """
        client_class.return_value.create_bank_transfer_charge.return_value = self._provider_answer()
        api = self._api()
        api.post(f"/api/v1/payments/{self.payment.id}/bank-transfer/")
        client_class.return_value.verify_transaction_by_reference.return_value = {
            "status": "success",
            "data": {
                "id": 2028146661,
                "tx_ref": "PAY-TRANSFER-1",
                "status": "successful",
                "currency": "NGN",
                "amount": 25150,
            },
        }

        with self.captureOnCommitCallbacks(execute=True):
            response = api.get(f"/api/v1/payments/{self.payment.id}/")

        self.assertEqual(response.status_code, 200)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, EscrowPayment.Status.HELD)
        self.assertEqual(self.payment.provider_transaction_id, "2028146661")

    @patch("payments.services.FlutterwaveClient")
    def test_both_sides_are_told_by_email_and_in_the_app(self, client_class):
        """A landed transfer is news for both of them, not just the payer."""
        from notifications.models import InAppNotification

        client_class.return_value.create_bank_transfer_charge.return_value = self._provider_answer()
        api = self._api()
        api.post(f"/api/v1/payments/{self.payment.id}/bank-transfer/")
        client_class.return_value.verify_transaction_by_reference.return_value = {
            "status": "success",
            "data": {
                "id": 2028146662,
                "tx_ref": "PAY-TRANSFER-1",
                "status": "successful",
                "currency": "NGN",
                "amount": 25150,
            },
        }

        with self.captureOnCommitCallbacks(execute=True):
            api.get(f"/api/v1/payments/{self.payment.id}/")

        notes = InAppNotification.objects.filter(
            event_key__contains=str(self.payment.id)
        )
        recipients = {note.recipient_id for note in notes}
        self.assertEqual(
            recipients, {self.customer_user.id, self.payment.artisan.user_id}
        )
        # Both are popups -- the artisan is told the escrow is funded without
        # having to open the notification list.
        self.assertTrue(all(note.data.get("popup") == "toast" for note in notes))
        self.assertTrue(any("25,150" in note.message for note in notes))

    @patch("payments.services.FlutterwaveClient")
    def test_asking_for_a_new_account_while_one_is_open_is_refused(self, client_class):
        """The API enforces what the chat shows: one live account at a time."""
        client_class.return_value.create_bank_transfer_charge.return_value = self._provider_answer()
        api = self._api()
        api.post(f"/api/v1/payments/{self.payment.id}/bank-transfer/")
        reference = EscrowPayment.objects.get(pk=self.payment.pk).reference

        response = api.post(
            f"/api/v1/payments/{self.payment.id}/bank-transfer/", {"renew": True}
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("still open", response.json()["detail"])
        self.assertEqual(
            client_class.return_value.create_bank_transfer_charge.call_count, 1
        )
        self.assertEqual(
            EscrowPayment.objects.get(pk=self.payment.pk).reference, reference
        )

    @patch("payments.services.FlutterwaveClient")
    def test_the_window_is_the_number_the_card_counts_down(self, client_class):
        """The card, the hidden button and the guard read one deadline."""
        client_class.return_value.create_bank_transfer_charge.return_value = self._provider_answer()
        api = self._api()
        api.post(f"/api/v1/payments/{self.payment.id}/bank-transfer/")

        fresh = api.get(f"/api/v1/payments/{self.payment.id}/").json()["bank_transfer"]
        self.assertTrue(fresh["live"])
        self.assertGreater(fresh["seconds_left"], 840)  # a 15-minute window
        self.assertTrue(fresh["live_until"])

        payload = dict(EscrowPayment.objects.get(pk=self.payment.pk).provider_payload)
        record = dict(payload["bank_transfer"])
        record["minted_at"] = (timezone.now() - timedelta(minutes=16)).isoformat()
        payload["bank_transfer"] = record
        EscrowPayment.objects.filter(pk=self.payment.pk).update(provider_payload=payload)

        stale = api.get(f"/api/v1/payments/{self.payment.id}/").json()["bank_transfer"]
        self.assertFalse(stale["live"])
        self.assertEqual(stale["seconds_left"], 0)
        self.assertEqual(stale["live_until"], "")

    @patch("payments.services.FlutterwaveClient")
    def test_the_pay_route_after_the_window_mints_a_new_account(self, client_class):
        """Pressing pay after the timer is the supported way to a new account."""
        client_class.return_value.create_bank_transfer_charge.return_value = self._provider_answer()
        api = self._api()
        api.post(f"/api/v1/payments/{self.payment.id}/bank-transfer/")
        payload = dict(EscrowPayment.objects.get(pk=self.payment.pk).provider_payload)
        record = dict(payload["bank_transfer"])
        record["minted_at"] = (timezone.now() - timedelta(minutes=16)).isoformat()
        payload["bank_transfer"] = record
        EscrowPayment.objects.filter(pk=self.payment.pk).update(provider_payload=payload)
        retired = EscrowPayment.objects.get(pk=self.payment.pk).reference

        response = api.post(f"/api/v1/payments/{self.payment.id}/bank-transfer/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["account_number"], "9755152912")
        self.assertNotEqual(EscrowPayment.objects.get(pk=self.payment.pk).reference, retired)
        self.assertEqual(
            client_class.return_value.create_bank_transfer_charge.call_count, 2
        )

    @patch("payments.services.FlutterwaveClient")
    def test_a_closed_account_is_replaced_with_a_fresh_one(self, client_class):
        """After the hour runs out the payer asks again and gets a new account."""
        client_class.return_value.create_bank_transfer_charge.return_value = self._provider_answer()
        api = self._api()
        api.post(f"/api/v1/payments/{self.payment.id}/bank-transfer/")

        self.payment.refresh_from_db()
        payload = dict(self.payment.provider_payload)
        record = dict(payload["bank_transfer"])
        record["minted_at"] = (timezone.now() - timedelta(hours=2)).isoformat()
        payload["bank_transfer"] = record
        self.payment.provider_payload = payload
        self.payment.save(update_fields=["provider_payload"])
        retired_reference = self.payment.reference

        response = api.post(
            f"/api/v1/payments/{self.payment.id}/bank-transfer/", {"renew": True}
        )

        self.assertEqual(response.status_code, 200)
        self.payment.refresh_from_db()
        self.assertNotEqual(self.payment.reference, retired_reference)
        self.assertEqual(response.json()["account_number"], "9755152912")
        self.assertEqual(
            client_class.return_value.create_bank_transfer_charge.call_args.kwargs["reference"],
            self.payment.reference,
        )
        self.assertEqual(
            client_class.return_value.create_bank_transfer_charge.call_count, 2
        )


class PaymentReceiptTests(TestCase):
    """A receipt exists only for money that really moved, and shows each party
    what belongs to them."""

    def setUp(self):
        from home.models import ArtisanProfile, CustomerProfile, Locations, Service, ServiceCategory
        from services.models import ServiceRequest

        self.customer_user = CustomUser.objects.create_user(
            email="payer@example.com", first_name="Kehinde", last_name="Omoyayi",
            password="StrongPassword123!", is_active=True,
        )
        self.artisan_user = CustomUser.objects.create_user(
            email="worker@example.com", first_name="Ada", last_name="Obi",
            password="StrongPassword123!", is_active=True,
        )
        self.stranger = CustomUser.objects.create_user(
            email="stranger@example.com", first_name="No", last_name="Body",
            password="StrongPassword123!", is_active=True,
        )
        self.customer = CustomerProfile.objects.create(user=self.customer_user)
        self.artisan = ArtisanProfile.objects.create(user=self.artisan_user)
        self.category = ServiceCategory.objects.create(name="Plumbing")
        Service.objects.create(category=self.category, name="Tap repair")
        location = Locations.objects.create(country="Nigeria", state="Lagos", area="Lekki")
        request = ServiceRequest.objects.create(
            requester=self.customer_user, category=self.category, location=location,
            preferred_date=(timezone.now() + timedelta(days=1)).date(),
            description="The kitchen tap drips.",
        )
        self.booking = Booking.objects.create(
            request=request, artisan=self.artisan, customer=self.customer,
            scheduled_time_start=timezone.now() + timedelta(days=1),
            scheduled_time_end=timezone.now() + timedelta(days=1, hours=2),
            price_agreed="161.00", status=Booking.Status.IN_PROGRESS,
        )
        self.payment = EscrowPayment.objects.create(
            booking=self.booking, customer=self.customer_user, artisan=self.artisan,
            reference="PAY-RECEIPT-1", provider_transaction_id="1000042610311806",
            provider_reference="PAY-RECEIPT-1",
            provider_payload={"bank_transfer": {"account_number": "9432969783"}},
            verification_payload={"data": {"payment_type": "bank_transfer"}},
            service_amount_kobo=16_100, customer_fee_kobo=322, total_amount_kobo=16_422,
            platform_commission_kobo=1_610, artisan_payout_kobo=14_490,
            status=EscrowPayment.Status.HELD,
            verified_at=timezone.now(),
        )

    def _receipt_for(self, user):
        from payments.serializers import PaymentSerializer

        request = APIRequestFactory().get("/api/v1/payments/")
        request.user = user
        payment = EscrowPayment.objects.select_related(
            "booking__customer__user", "artisan__user"
        ).get(pk=self.payment.pk)
        return PaymentSerializer(payment, context={"request": request}).data["receipt"]

    def test_a_held_payment_has_a_receipt_with_the_provider_amount(self):
        receipt = self._receipt_for(self.customer_user)

        self.assertTrue(receipt["available"])
        self.assertEqual(receipt["amounts"]["total"], 164.22)
        self.assertEqual(receipt["amounts"]["service"], 161.0)
        self.assertEqual(receipt["amounts"]["fee"], 3.22)
        self.assertEqual(receipt["method"], "Bank transfer")
        self.assertEqual(receipt["provider_reference"], "1000042610311806")
        self.assertEqual(receipt["service"], "Plumbing")
        self.assertTrue(receipt["number"].startswith("AM-"))

    def test_the_payer_does_not_see_the_artisan_payout(self):
        receipt = self._receipt_for(self.customer_user)

        self.assertNotIn("net_to_payee", receipt["amounts"])
        self.assertEqual(receipt["viewer"], "customer")

    def test_the_artisan_sees_what_they_will_be_paid(self):
        receipt = self._receipt_for(self.artisan_user)

        self.assertEqual(receipt["viewer"], "artisan")
        self.assertEqual(receipt["amounts"]["net_to_payee"], 144.9)

    def test_money_that_never_arrived_has_no_receipt(self):
        EscrowPayment.objects.filter(pk=self.payment.pk).update(
            status=EscrowPayment.Status.PAYMENT_PENDING, verified_at=None
        )

        self.assertFalse(self._receipt_for(self.customer_user)["available"])

    def test_a_stranger_gets_nothing_back(self):
        self.assertEqual(self._receipt_for(self.stranger), {})

    def test_the_api_serves_the_receipt_to_the_payer(self):
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(user=self.customer_user)
        response = client.get(f"/api/v1/payments/{self.payment.pk}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["receipt"]["amounts"]["total"], 164.22)

    def test_the_api_hides_the_receipt_from_a_stranger(self):
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(user=self.stranger)
        response = client.get(f"/api/v1/payments/{self.payment.pk}/")

        self.assertEqual(response.status_code, 404)
