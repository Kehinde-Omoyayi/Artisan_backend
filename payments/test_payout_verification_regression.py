"""Regression cover for the payout-verification workflow.

Two coupled defects made payout verification impossible:

1. An artisan marked VERIFIED through the Django admin has no encrypted
   identity name (the field is raw free text there), so every payout attempt
   is refused by the identity gate before Flutterwave is ever called. The
   artisan is told to "complete identity verification again" although the
   admin shows them as verified: an impossible loop.

2. The protected-finance verification endpoint still demands the retired
   two-step state (status PENDING *and* a stored encrypted account number),
   which the current save flow never produces. Every verification therefore
   failed, and the operator was shown a message that did not describe the
   real problem.
"""

from cryptography.fernet import Fernet
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.test import TestCase, override_settings
from unittest.mock import patch

from home.models import ArtisanProfile, CustomUser
from rest_framework.test import APIClient
from services.kyc_crypto import decrypt_value, encrypt_identity_name, encrypt_value
from services.models import ArtisanVerification
from support.permissions import SUPPORT_FINANCE

from .models import ArtisanPayoutAccount

TEST_KYC_KEY = Fernet.generate_key().decode("ascii")
TEST_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "payout-verification-tests",
    }
}

BANKS = [{"code": "058", "name": "Provider Bank"}]
RESOLVED = {
    "status": "success",
    "data": {"account_number": "1234567890", "account_name": "Payout Artisan"},
}
BENEFICIARY = {
    "status": "success",
    "data": {
        "id": "9911",
        "account_number": "1234567890",
        "bank_code": "058",
        "full_name": "Payout Artisan",
    },
}


def _mock_provider():
    """resolve_account + create_beneficiary both succeed and mirror the request."""
    return patch("payments.views.FlutterwaveClient")  # configured per-test


@override_settings(KYC_ENCRYPTION_KEY=TEST_KYC_KEY, CACHES=TEST_CACHES)
class PayoutVerificationRegressionTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = CustomUser.objects.create_user(
            email="payout-regression@example.com",
            first_name="Payout",
            last_name="Artisan",
            password="StrongPassword123!",
            is_active=True,
        )
        self.artisan = ArtisanProfile.objects.create(
            user=self.user, profile_status="ACTIVE"
        )
        # Exactly what the bare admin produces: VERIFIED, no encrypted name.
        self.verification = ArtisanVerification.objects.create(
            artisan=self.artisan,
            verification_status="VERIFIED",
            verification_method="NIN",
            verification_tier=2,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.payload = {
            "account_name": "Payout Artisan",
            "bank_code": "058",
            "account_number": "1234567890",
        }

    def _finance_user(self):
        finance = CustomUser.objects.create_user(
            email="finance-regression@example.com",
            first_name="Fin",
            last_name="Ops",
            password="StrongPassword123!",
            is_active=True,
            is_staff=True,
        )
        group, _ = Group.objects.get_or_create(name=SUPPORT_FINANCE)
        finance.groups.add(group)
        return finance

    # -- 1. the admin-verified artisan can actually save payout details -----

    def test_admin_verified_artisan_can_verify_and_activate_payout(self):
        """After the supported admin approval the full provider flow succeeds."""
        from services.verification import mark_identity_verified_for_admin

        mark_identity_verified_for_admin(self.verification)

        with (
            patch("payments.views.get_supported_nigerian_banks", return_value=BANKS),
            patch("payments.views.FlutterwaveClient") as provider,
        ):
            provider.return_value.resolve_account.return_value = RESOLVED
            provider.return_value.create_beneficiary.return_value = BENEFICIARY
            response = self.client.put(
                "/api/v1/payments/payout-account/",
                self.payload,
                format="json",
                HTTP_IDEMPOTENCY_KEY="payout-regression-1",
            )

        self.assertEqual(response.status_code, 201, response.data)
        account = ArtisanPayoutAccount.objects.get(artisan=self.artisan)
        self.assertEqual(account.status, ArtisanPayoutAccount.Status.VERIFIED)
        self.assertTrue(account.is_verified and account.is_active)
        self.assertEqual(account.provider_beneficiary_id, 9911)

    def test_identity_gate_refuses_a_verification_without_an_encrypted_name(self):
        """The gate stays closed when the encrypted name is missing."""
        with (
            patch("payments.views.get_supported_nigerian_banks", return_value=BANKS),
            patch("payments.views.FlutterwaveClient") as provider,
        ):
            provider.return_value.resolve_account.return_value = RESOLVED
            provider.return_value.create_beneficiary.return_value = BENEFICIARY
            response = self.client.put(
                "/api/v1/payments/payout-account/",
                self.payload,
                format="json",
                HTTP_IDEMPOTENCY_KEY="payout-regression-2",
            )

        self.assertEqual(response.status_code, 403, response.data)
        provider.return_value.resolve_account.assert_not_called()
        self.assertFalse(ArtisanPayoutAccount.objects.filter(artisan=self.artisan).exists())

    # -- 2. the protected-finance verification endpoint ---------------------

    def test_verification_of_a_row_without_a_stored_number_explains_itself(self):
        """An admin-created row cannot be verified; say what to do about it."""
        ArtisanPayoutAccount.objects.create(
            artisan=self.artisan,
            bank_code="058",
            bank_name="Provider Bank",
            account_name="Payout Artisan",
            account_number_last4="7890",
            status=ArtisanPayoutAccount.Status.PENDING,
        )
        client = APIClient()
        client.force_authenticate(self._finance_user())

        response = client.post(
            f"/api/v1/payments/payout-account/{self.artisan.pk}/verify/",
            format="json",
            HTTP_IDEMPOTENCY_KEY="payout-regression-3",
        )

        self.assertEqual(response.status_code, 409, response.data)
        detail = str(response.data.get("detail", ""))
        self.assertIn("submit the payout details again", detail)

    def test_a_row_with_a_stored_number_is_still_verified_end_to_end(self):
        """The protected review path keeps working for rows that hold a number."""
        account = ArtisanPayoutAccount.objects.create(
            artisan=self.artisan,
            bank_code="058",
            bank_name="Provider Bank",
            account_name="Payout Artisan",
            account_number_last4="7890",
            account_number_encrypted=encrypt_value("1234567890"),
            status=ArtisanPayoutAccount.Status.PENDING,
        )
        self.verification.verified_identity_name_encrypted = encrypt_identity_name(
            {"first_name": "Payout", "middle_name": "", "last_name": "Artisan"}
        )
        self.verification.save()

        client = APIClient()
        client.force_authenticate(self._finance_user())

        with (
            patch("payments.views.get_supported_nigerian_banks", return_value=BANKS),
            patch("payments.views.FlutterwaveClient") as provider,
        ):
            provider.return_value.resolve_account.return_value = RESOLVED
            provider.return_value.create_beneficiary.return_value = BENEFICIARY
            response = client.post(
                f"/api/v1/payments/payout-account/{self.artisan.pk}/verify/",
                format="json",
                HTTP_IDEMPOTENCY_KEY="payout-regression-4",
            )

        self.assertEqual(response.status_code, 200, response.data)
        account.refresh_from_db()
        self.assertEqual(account.status, ArtisanPayoutAccount.Status.VERIFIED)
        self.assertEqual(account.provider_beneficiary_id, 9911)
        self.assertEqual(account.account_number_encrypted, "")
