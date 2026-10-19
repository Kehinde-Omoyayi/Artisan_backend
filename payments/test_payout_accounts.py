from cryptography.fernet import Fernet
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from unittest.mock import patch

from booking.models import Booking
from home.models import ArtisanProfile, CustomUser
from payments.test_lifecycle import PaymentFixtureMixin
from payments.tasks import release_artisan_payout
from services.kyc_crypto import decrypt_value, encrypt_identity_name, encrypt_value
from services.models import ArtisanVerification

from .models import ArtisanPayoutAccount, EscrowPayment, EscrowRelease
from .payout_accounts import get_supported_nigerian_banks


TEST_KYC_KEY = Fernet.generate_key().decode("ascii")
TEST_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "payout-tests",
    }
}


@override_settings(KYC_ENCRYPTION_KEY=TEST_KYC_KEY, CACHES=TEST_CACHES)
class PayoutAccountTests(TestCase):
    def setUp(self):
        cache.clear()
        self.artisan_user = CustomUser.objects.create_user(
            email="payout@example.com",
            first_name="Payout",
            last_name="Artisan",
            password="StrongPassword123!",
            is_active=True,
        )
        self.artisan = ArtisanProfile.objects.create(
            user=self.artisan_user,
            profile_status="ACTIVE",
        )
        self.verification = ArtisanVerification.objects.create(
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
        self.client = APIClient()
        self.client.force_authenticate(self.artisan_user)
        self.payload = {
            "account_name": "Payout Artisan",
            "bank_code": "058",
            "account_number": "1234567890",
        }

    def _resolve_result(self, *, name="ARTISAN PAYOUT", number="1234567890"):
        return {
            "status": "success",
            "data": {"account_number": number, "account_name": name},
        }

    def test_payout_mutation_requires_verified_and_active_artisan(self):
        with patch("payments.views.FlutterwaveClient") as provider:
            self.verification.verification_status = "REJECTED"
            self.verification.save(update_fields=["verification_status"])
            response = self.client.put(
                "/api/v1/payments/payout-account/",
                self.payload,
                format="json",
                HTTP_IDEMPOTENCY_KEY="payout-unverified-1",
            )
            self.assertEqual(response.status_code, 403, response.data)
            self.assertIn("identity verification", response.data["detail"].lower())
            provider.assert_not_called()

            self.verification.verification_status = "VERIFIED"
            self.verification.save(update_fields=["verification_status"])
            self.artisan.profile_status = "INACTIVE"
            self.artisan.save(update_fields=["profile_status"])
            response = self.client.put(
                "/api/v1/payments/payout-account/",
                self.payload,
                format="json",
                HTTP_IDEMPOTENCY_KEY="payout-inactive-1",
            )
            self.assertEqual(response.status_code, 403, response.data)
            self.assertIn("active", response.data["detail"].lower())
            provider.assert_not_called()
        self.assertFalse(ArtisanPayoutAccount.objects.filter(artisan=self.artisan).exists())

    def test_user_entered_account_name_must_match_both_profile_and_verified_identity(self):
        payload = {**self.payload, "account_name": "Different Person"}
        with (
            patch("payments.views.get_supported_nigerian_banks") as banks,
            patch("payments.views.FlutterwaveClient") as provider,
        ):
            response = self.client.put(
                "/api/v1/payments/payout-account/",
                payload,
                format="json",
                HTTP_IDEMPOTENCY_KEY="payout-name-input-mismatch-1",
            )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("verified identity and artisan profile", str(response.data))
        banks.assert_not_called()
        provider.assert_not_called()
        self.assertFalse(ArtisanPayoutAccount.objects.filter(artisan=self.artisan).exists())

    def test_provider_resolved_name_mismatch_is_not_saved_as_verified_or_pending(self):
        with (
            patch("payments.views.get_supported_nigerian_banks", return_value=[{"code": "058", "name": "Provider Bank"}]),
            patch("payments.views.FlutterwaveClient") as provider,
        ):
            provider.return_value.resolve_account.return_value = self._resolve_result(name="Someone Else")
            response = self.client.put(
                "/api/v1/payments/payout-account/",
                self.payload,
                format="json",
                HTTP_IDEMPOTENCY_KEY="payout-name-provider-mismatch-1",
            )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertNotIn("Someone Else", str(response.data))
        self.assertFalse(ArtisanPayoutAccount.objects.filter(artisan=self.artisan).exists())

    def test_only_configured_provider_banks_are_accepted_and_returned(self):
        supported = [{"code": "058", "name": "Provider Bank"}]
        with patch("payments.views.get_supported_nigerian_banks", return_value=supported):
            banks_response = self.client.get("/api/v1/payments/payout-account/banks/")
        self.assertEqual(banks_response.status_code, 200)
        self.assertEqual(banks_response.data, {"banks": supported})

        with (
            patch("payments.views.get_supported_nigerian_banks", return_value=supported),
            patch("payments.views.FlutterwaveClient") as provider,
        ):
            response = self.client.put(
                "/api/v1/payments/payout-account/",
                {**self.payload, "bank_code": "999"},
                format="json",
                HTTP_IDEMPOTENCY_KEY="payout-unsupported-bank-1",
            )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("supported by Flutterwave", str(response.data))
        provider.assert_not_called()

    def test_provider_bank_directory_excludes_mobile_money_and_invalid_entries(self):
        provider_banks = {
            "status": "success",
            "data": [
                {"code": "058", "name": "Provider Bank", "provider_type": "bank"},
                {"code": "999", "name": "Mobile Wallet", "provider_type": "mobile_money"},
                {"code": "bad", "name": "Invalid Code", "provider_type": "bank"},
                {"code": "058", "name": "Duplicate Bank", "provider_type": "bank"},
            ],
        }
        with patch("payments.payout_accounts.FlutterwaveClient") as provider:
            provider.return_value.list_banks.return_value = provider_banks
            banks = get_supported_nigerian_banks(force_refresh=True)

        self.assertEqual(banks, [{"code": "058", "name": "Provider Bank"}])
        provider.return_value.list_banks.assert_called_once_with(country="NG")

    def test_successful_provider_resolution_saves_pending_details_encrypted_until_protected_review(self):
        with (
            patch("payments.views.get_supported_nigerian_banks", return_value=[{"code": "058", "name": "Provider Bank"}]),
            patch("payments.views.FlutterwaveClient") as provider,
        ):
            provider.return_value.resolve_account.return_value = self._resolve_result()
            response = self.client.put(
                "/api/v1/payments/payout-account/",
                self.payload,
                format="json",
                HTTP_IDEMPOTENCY_KEY="payout-provider-resolve-1",
            )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["account_name"], "ARTISAN PAYOUT")
        self.assertEqual(response.data["bank_name"], "Provider Bank")
        self.assertEqual(response.data["account_number_last4"], "7890")
        self.assertNotIn("1234567890", str(response.data))
        self.assertNotIn("account_number_encrypted", response.data)
        account = ArtisanPayoutAccount.objects.get(artisan=self.artisan)
        self.assertEqual(account.status, ArtisanPayoutAccount.Status.PENDING)
        self.assertFalse(account.is_verified)
        self.assertFalse(account.is_active)
        self.assertIsNone(account.provider_beneficiary_id)
        self.assertNotEqual(account.account_number_encrypted, "1234567890")
        self.assertEqual(decrypt_value(account.account_number_encrypted), "1234567890")

    def _pending_account(self):
        return ArtisanPayoutAccount.objects.create(
            artisan=self.artisan,
            bank_code="058",
            bank_name="Provider Bank",
            account_number_last4="7890",
            account_number_encrypted=encrypt_value("1234567890"),
            account_name="ARTISAN PAYOUT",
            status=ArtisanPayoutAccount.Status.PENDING,
            is_active=False,
            is_verified=False,
        )

    def _finance_client(self):
        finance_user = CustomUser.objects.create_superuser(
            email="payout-finance@example.com",
            first_name="Finance",
            last_name="Reviewer",
            password="StrongPassword123!",
        )
        client = APIClient()
        client.force_authenticate(finance_user)
        return client

    def test_protected_finance_verification_requires_provider_resolution_and_beneficiary_match(self):
        account = self._pending_account()
        client = self._finance_client()
        beneficiary = {
            "status": "success",
            "data": {
                "id": 456789,
                "account_number": "1234567890",
                "bank_code": "058",
                "full_name": "Payout Artisan",
            },
        }
        with (
            patch("payments.views.get_supported_nigerian_banks", return_value=[{"code": "058", "name": "Provider Bank"}]),
            patch("payments.views.FlutterwaveClient") as provider,
        ):
            provider.return_value.resolve_account.return_value = self._resolve_result()
            provider.return_value.create_beneficiary.return_value = beneficiary
            response = client.post(
                f"/api/v1/payments/payout-account/{self.artisan.pk}/verify/",
                {},
                format="json",
                HTTP_IDEMPOTENCY_KEY="payout-finance-verify-1",
            )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], ArtisanPayoutAccount.Status.VERIFIED)
        self.assertTrue(response.data["is_verified"])
        self.assertTrue(response.data["is_active"])
        self.assertNotIn("1234567890", str(response.data))
        provider.return_value.create_beneficiary.assert_called_once()
        account.refresh_from_db()
        self.assertEqual(account.provider_beneficiary_id, 456789)
        self.assertEqual(account.account_number_encrypted, "")

    def test_failed_beneficiary_check_leaves_account_pending_and_unverified(self):
        account = self._pending_account()
        client = self._finance_client()
        with (
            patch("payments.views.get_supported_nigerian_banks", return_value=[{"code": "058", "name": "Provider Bank"}]),
            patch("payments.views.FlutterwaveClient") as provider,
        ):
            provider.return_value.resolve_account.return_value = self._resolve_result()
            provider.return_value.create_beneficiary.return_value = {
                "status": "success",
                "data": {
                    "id": 456789,
                    "account_number": "0000000000",
                    "bank_code": "058",
                    "full_name": "Payout Artisan",
                },
            }
            response = client.post(
                f"/api/v1/payments/payout-account/{self.artisan.pk}/verify/",
                {},
                format="json",
                HTTP_IDEMPOTENCY_KEY="payout-finance-verify-failed-1",
            )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertNotIn("1234567890", str(response.data))
        account.refresh_from_db()
        self.assertEqual(account.status, ArtisanPayoutAccount.Status.PENDING)
        self.assertFalse(account.is_verified)
        self.assertFalse(account.is_active)
        self.assertIsNone(account.provider_beneficiary_id)
        self.assertEqual(decrypt_value(account.account_number_encrypted), "1234567890")


@override_settings(KYC_ENCRYPTION_KEY=TEST_KYC_KEY, CACHES=TEST_CACHES)
class PayoutReleaseIdentityGateTests(PaymentFixtureMixin, TestCase):
    def setUp(self):
        self.make_world()
        now = timezone.now()
        self.booking.status = Booking.Status.COMPLETED
        self.booking.artisan_completed_at = now
        self.booking.customer_completed_at = now
        self.booking.save(update_fields=["status", "artisan_completed_at", "customer_completed_at"])
        self.payment.status = EscrowPayment.Status.RELEASE_PENDING
        self.payment.save(update_fields=["status"])

    def _verified_record(self, identity_first="Art", identity_last="Isan"):
        from services.kyc_crypto import encrypt_identity_name

        return ArtisanVerification.objects.create(
            artisan=self.artisan,
            verification_status="VERIFIED",
            verification_method="NIN",
            verification_tier=2,
            verified_identity_name_encrypted=encrypt_identity_name({
                "first_name": identity_first,
                "middle_name": "",
                "last_name": identity_last,
            }),
        )

    def test_profile_name_mismatch_blocks_protected_payout_release(self):
        self._verified_record(identity_first="Different", identity_last="Person")
        with patch("payments.tasks.FlutterwaveClient") as provider:
            result = release_artisan_payout.run(str(self.payment.pk))

        self.assertEqual(result["status"], "artisan_identity_name_mismatch")
        provider.assert_not_called()
        self.assertFalse(EscrowRelease.objects.filter(payment=self.payment).exists())

    def test_rejected_identity_verification_blocks_protected_payout_release(self):
        verification = self._verified_record()
        verification.verification_status = "REJECTED"
        verification.save(update_fields=["verification_status"])

        with patch("payments.tasks.FlutterwaveClient") as provider:
            result = release_artisan_payout.run(str(self.payment.pk))

        self.assertEqual(result["status"], "artisan_identity_not_verified")
        provider.assert_not_called()
        self.assertFalse(EscrowRelease.objects.filter(payment=self.payment).exists())
