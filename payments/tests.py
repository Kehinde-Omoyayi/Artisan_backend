import base64
import hashlib
import hmac
import json
from unittest.mock import patch

from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings
from rest_framework.test import APIClient
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
