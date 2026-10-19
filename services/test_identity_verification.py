from cryptography.fernet import Fernet
from django.test import TestCase, override_settings
from unittest.mock import patch

from home.models import ArtisanProfile, CustomUser

from .identity_names import (
    PROFILE_NAME_CORRECTION_MESSAGE,
    account_name_matches_identity,
    extract_provider_identity_name,
    profile_name_matches_identity,
    provider_reports_name_mismatch,
    provider_result_is_pending,
)
from .kyc_crypto import decrypt_identity_name, encrypt_identity_name, encrypt_nin, encrypt_otp
from .models import ArtisanVerification
from .tasks import (
    complete_bvn_async,
    set_bvn_session,
    verify_nin_async,
)
from .verification import claim_verification


TEST_KYC_KEY = Fernet.generate_key().decode("ascii")
TEST_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "identity-tests",
    }
}


@override_settings(KYC_ENCRYPTION_KEY=TEST_KYC_KEY, CACHES=TEST_CACHES)
class IdentityNameHelperTests(TestCase):
    def test_profile_and_provider_name_comparison_ignores_case_punctuation_and_middle_names(self):
        identity = {
            "first_name": "Ada",
            "middle_name": "Nneka",
            "last_name": "Okafor",
        }
        self.assertTrue(profile_name_matches_identity(identity, "ada", "OKAFOR"))
        self.assertTrue(account_name_matches_identity("OKAFOR, ADA NNEKA", identity, "Ada", "Okafor"))
        self.assertFalse(profile_name_matches_identity(identity, "Ada", "Adebayo"))
        self.assertFalse(account_name_matches_identity("Ada Adebayo", identity, "Ada", "Okafor"))

    def test_mono_nin_and_bvn_name_shapes_are_extracted_without_storing_other_fields(self):
        nin = extract_provider_identity_name(
            {"data": {"firstname": "ADA", "middlename": "NNEKA", "surname": "OKAFOR", "nin": "00000000000"}},
            "NIN",
        )
        bvn = extract_provider_identity_name(
            {"data": {"first_name": "Ada", "middle_name": "Nneka", "last_name": "Okafor", "bvn": "11111111111"}},
            "BVN",
        )
        self.assertEqual(nin, {"first_name": "ADA", "middle_name": "NNEKA", "last_name": "OKAFOR"})
        self.assertEqual(bvn, {"first_name": "Ada", "middle_name": "Nneka", "last_name": "Okafor"})

    def test_provider_mismatch_and_pending_signals_are_detected(self):
        self.assertTrue(provider_reports_name_mismatch({"status": "successful", "data": {"name_match": False}}))
        self.assertTrue(provider_reports_name_mismatch({"status": "successful", "message": "The identity name does not match."}))
        self.assertTrue(provider_reports_name_mismatch({"status": "successful", "data": {"match": "mismatch"}}))
        self.assertTrue(provider_reports_name_mismatch({"status": "successful", "data": {"identity_mismatch": True}}))
        self.assertFalse(provider_reports_name_mismatch({"status": "successful", "message": "No mismatch found."}))
        self.assertTrue(provider_result_is_pending({"_http_status": 202, "status": "successful"}))
        self.assertTrue(provider_result_is_pending({"status": "successful", "data": {"status": "processing"}}))
        self.assertFalse(provider_result_is_pending({"status": "successful", "data": {"status": "complete"}}))

    def test_identity_name_is_encrypted_at_rest(self):
        parts = {"first_name": "Ada", "middle_name": "Nneka", "last_name": "Okafor"}
        encrypted = encrypt_identity_name(parts)
        self.assertNotIn("Ada", encrypted)
        self.assertNotIn("Okafor", encrypted)
        self.assertEqual(decrypt_identity_name(encrypted), parts)


@override_settings(KYC_ENCRYPTION_KEY=TEST_KYC_KEY, CACHES=TEST_CACHES)
class VerificationTierTests(TestCase):
    def setUp(self):
        self.user = CustomUser.objects.create_user(
            email="identity@example.com",
            first_name="Ada",
            last_name="Okafor",
            password="StrongPassword123!",
            is_active=True,
        )
        self.artisan = ArtisanProfile.objects.create(user=self.user, profile_status="ACTIVE")
        self.verification = ArtisanVerification.objects.create(
            artisan=self.artisan,
            verification_status="PENDING",
            verification_method="NIN",
            verification_tier=4,
        )

    def _nin_result(self, **overrides):
        result = {
            "status": "successful",
            "_http_status": 200,
            "data": {
                "firstname": "ADA",
                "middlename": "NNEKA",
                "surname": "OKAFOR",
                "tracking_id": "TRACK-NIN-1",
            },
        }
        result.update(overrides)
        return result

    def test_nin_success_advances_exactly_one_tier_and_duplicate_processing_is_ignored(self):
        with (
            patch("services.tasks.MonoVerificationService") as mono,
            patch("services.tasks._send_email"),
        ):
            mono.return_value.lookup_nin.return_value = self._nin_result()
            result = verify_nin_async.run(str(self.verification.pk), encrypt_nin("12345678901"))
            self.verification.refresh_from_db()
            self.assertEqual(result["status"], "VERIFIED")
            self.assertEqual(self.verification.verification_status, "VERIFIED")
            self.assertEqual(self.verification.verification_tier, 5)
            self.assertTrue(self.verification.verified_identity_name_encrypted)
            self.assertEqual(
                decrypt_identity_name(self.verification.verified_identity_name_encrypted)["last_name"],
                "OKAFOR",
            )

            repeated = verify_nin_async.run(str(self.verification.pk), encrypt_nin("12345678901"))

        self.assertEqual(repeated["status"], "IGNORED")
        self.verification.refresh_from_db()
        self.assertEqual(self.verification.verification_tier, 5)
        mono.return_value.lookup_nin.assert_called_once()

    def test_nin_profile_name_mismatch_rejects_without_tier_change_or_identity_storage(self):
        self.user.first_name = "Amaka"
        self.user.last_name = "Nwosu"
        self.user.save(update_fields=["first_name", "last_name"])
        with (
            patch("services.tasks.MonoVerificationService") as mono,
            patch("services.tasks._send_email"),
        ):
            mono.return_value.lookup_nin.return_value = self._nin_result()
            result = verify_nin_async.run(str(self.verification.pk), encrypt_nin("12345678901"))

        self.verification.refresh_from_db()
        self.assertEqual(result["status"], "REJECTED")
        self.assertEqual(self.verification.verification_status, "REJECTED")
        self.assertEqual(self.verification.verification_tier, 4)
        self.assertEqual(self.verification.verified_identity_name_encrypted, "")
        self.assertIn("Update the first and last name", result["message"])
        self.assertNotIn("ADA", result["message"])
        self.assertNotIn("OKAFOR", result["message"])

    def test_provider_reported_name_mismatch_is_rejected_even_if_profile_name_matches(self):
        result = self._nin_result(message="Identity name mismatch reported")
        with (
            patch("services.tasks.MonoVerificationService") as mono,
            patch("services.tasks._send_email"),
        ):
            mono.return_value.lookup_nin.return_value = result
            task_result = verify_nin_async.run(str(self.verification.pk), encrypt_nin("12345678901"))

        self.verification.refresh_from_db()
        self.assertEqual(task_result["status"], "REJECTED")
        self.assertEqual(self.verification.verification_status, "REJECTED")
        self.assertEqual(self.verification.verification_tier, 4)
        self.assertFalse(self.verification.verified_identity_name_encrypted)
        self.assertIn("Update the first and last name", task_result["message"])

    def test_pending_nin_result_does_not_verify_or_advance_tier(self):
        pending = {
            "status": "successful",
            "_http_status": 202,
            "data": {"status": "processing", "tracking_id": "TRACK-NIN-PENDING"},
        }
        with (
            patch("services.tasks.MonoVerificationService") as mono,
            patch("services.tasks._send_email"),
        ):
            mono.return_value.lookup_nin.return_value = pending
            task_result = verify_nin_async.run(str(self.verification.pk), encrypt_nin("12345678901"))

        self.verification.refresh_from_db()
        self.assertEqual(task_result["status"], "PENDING")
        self.assertEqual(self.verification.verification_status, "PENDING")
        self.assertEqual(self.verification.verification_tier, 4)
        self.assertFalse(self.verification.verified_identity_name_encrypted)

    def test_provider_pending_status_keeps_nin_pending_without_tier_advance(self):
        pending = {
            "status": "pending",
            "_http_status": 200,
            "data": {"firstname": "ADA", "surname": "OKAFOR", "tracking_id": "TRACK-NIN-PENDING"},
        }
        with (
            patch("services.tasks.MonoVerificationService") as mono,
            patch("services.tasks._send_email"),
        ):
            mono.return_value.lookup_nin.return_value = pending
            task_result = verify_nin_async.run(str(self.verification.pk), encrypt_nin("12345678901"))

        self.verification.refresh_from_db()
        self.assertEqual(task_result["status"], "PENDING")
        self.assertEqual(self.verification.verification_status, "PENDING")
        self.assertEqual(self.verification.verification_tier, 4)
        self.assertFalse(self.verification.verified_identity_name_encrypted)

    def test_bvn_success_advances_one_tier_after_returned_identity_matches(self):
        self.verification.verification_method = "BVN"
        self.verification.save(update_fields=["verification_method"])
        set_bvn_session(self.verification.pk, "mono-session")
        result = {
            "status": "successful",
            "_http_status": 200,
            "data": {
                "first_name": "Ada",
                "middle_name": "Nneka",
                "last_name": "Okafor",
                "tracking_id": "TRACK-BVN-1",
            },
        }
        with (
            patch("services.tasks.MonoVerificationService") as mono,
            patch("services.tasks._send_email"),
        ):
            mono.return_value.fetch_bvn_details.return_value = result
            task_result = complete_bvn_async.run(str(self.verification.pk), encrypt_otp("123456"))
            self.verification.refresh_from_db()
            repeated = complete_bvn_async.run(str(self.verification.pk), encrypt_otp("123456"))

        self.assertEqual(task_result["status"], "VERIFIED")
        self.assertEqual(repeated["status"], "IGNORED")
        self.assertEqual(self.verification.verification_status, "VERIFIED")
        self.assertEqual(self.verification.verification_tier, 5)
        self.assertTrue(self.verification.verified_identity_name_encrypted)
        mono.return_value.fetch_bvn_details.assert_called_once()

    def test_pending_bvn_result_does_not_verify_or_advance_tier(self):
        self.verification.verification_method = "BVN"
        self.verification.save(update_fields=["verification_method"])
        set_bvn_session(self.verification.pk, "mono-session")
        with (
            patch("services.tasks.MonoVerificationService") as mono,
            patch("services.tasks._send_email"),
        ):
            mono.return_value.fetch_bvn_details.return_value = {
                "status": "pending",
                "_http_status": 200,
                "data": {
                    "status": "processing",
                    "first_name": "Ada",
                    "middle_name": "Nneka",
                    "last_name": "Okafor",
                },
            }
            task_result = complete_bvn_async.run(str(self.verification.pk), encrypt_otp("123456"))

        self.verification.refresh_from_db()
        self.assertEqual(task_result["status"], "PENDING")
        self.assertEqual(self.verification.verification_status, "PENDING")
        self.assertEqual(self.verification.verification_tier, 4)
        self.assertFalse(self.verification.verified_identity_name_encrypted)

    def test_bvn_profile_mismatch_rejects_without_tier_advance(self):
        self.user.last_name = "Nwosu"
        self.user.save(update_fields=["last_name"])
        self.verification.verification_method = "BVN"
        self.verification.save(update_fields=["verification_method"])
        set_bvn_session(self.verification.pk, "mono-session")
        with (
            patch("services.tasks.MonoVerificationService") as mono,
            patch("services.tasks._send_email"),
        ):
            mono.return_value.fetch_bvn_details.return_value = {
                "status": "successful",
                "_http_status": 200,
                "data": {"first_name": "Ada", "middle_name": "", "last_name": "Okafor"},
            }
            task_result = complete_bvn_async.run(str(self.verification.pk), encrypt_otp("123456"))

        self.verification.refresh_from_db()
        self.assertEqual(task_result["status"], "REJECTED")
        self.assertEqual(self.verification.verification_status, "REJECTED")
        self.assertEqual(self.verification.verification_tier, 4)
        self.assertFalse(self.verification.verified_identity_name_encrypted)
        self.assertEqual(task_result["message"], PROFILE_NAME_CORRECTION_MESSAGE)

    def test_verified_legacy_record_without_encrypted_name_can_reverify_for_safe_payout_checks(self):
        self.verification.verification_status = "VERIFIED"
        self.verification.verification_method = "NIN"
        self.verification.save(update_fields=["verification_status", "verification_method"])

        claimed = claim_verification(self.artisan, "NIN")

        self.assertEqual(claimed.pk, self.verification.pk)
        self.assertEqual(claimed.verification_status, "PENDING")
        self.assertEqual(claimed.verification_tier, 4)
        self.assertEqual(claimed.verified_identity_name_encrypted, "")
