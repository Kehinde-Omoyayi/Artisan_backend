from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from home.auth_tokens import issue_marketplace_tokens
from home.security_mail import decrypt_email_code
from home.models import StaffSecurityChallenge

User = get_user_model()
TEST_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "authentication-hardening-tests",
    }
}
TEST_HASHERS = ["django.contrib.auth.hashers.PBKDF2PasswordHasher"]


@override_settings(CACHES=TEST_CACHES, PASSWORD_HASHERS=TEST_HASHERS)
class AuthenticationHardeningTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def create_user(self, *, email="customer@example.test", staff=False):
        return User.objects.create_user(
            email=email,
            first_name="Nia",
            last_name="Okafor",
            password="Correct!Password95",
            is_active=True,
            is_staff=staff,
        )

    def test_customer_password_login_issues_claim_bound_tokens_and_refresh_is_no_store(self):
        user = self.create_user()
        response = self.client.post(
            "/api/v1/login/",
            {"email": user.email, "password": "Correct!Password95"},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn("no-store", response["Cache-Control"])
        refresh = RefreshToken(response.data["refresh"])
        self.assertEqual(int(refresh["token_version"]), user.token_version)
        self.assertIsInstance(refresh["auth_time"], int)
        self.assertNotIn("staff_mfa", refresh)

        rotated = self.client.post(
            reverse("token-refresh"), {"refresh": response.data["refresh"]}, format="json"
        )
        self.assertEqual(rotated.status_code, 200, rotated.data)
        self.assertIn("no-store", rotated["Cache-Control"])
        self.assertEqual(int(RefreshToken(rotated.data["refresh"])["auth_time"]), int(refresh["auth_time"]))

    def test_staff_password_login_withholds_tokens_until_one_time_mfa_verification(self):
        staff = self.create_user(email="agent@example.test", staff=True)
        with patch("home.staff_mfa.send_staff_security_code.delay") as deliver:
            with self.captureOnCommitCallbacks(execute=True):
                login = self.client.post(
                    "/api/v1/login/",
                    {"email": staff.email, "password": "Correct!Password95"},
                    format="json",
                )

        self.assertEqual(login.status_code, 202, login.data)
        self.assertTrue(login.data["mfa_required"])
        self.assertNotIn("access", login.data)
        self.assertNotIn("refresh", login.data)
        self.assertIn("no-store", login["Cache-Control"])
        self.assertEqual(deliver.call_count, 1)
        challenge_id, encrypted_code = deliver.call_args.args
        code = decrypt_email_code(encrypted_code)
        challenge = StaffSecurityChallenge.objects.get(pk=challenge_id)
        self.assertEqual(challenge.purpose, StaffSecurityChallenge.Purpose.LOGIN_MFA)

        verified = self.client.post(
            reverse("staff-mfa-verify"),
            {"challenge_id": login.data["mfa_challenge_id"], "code": code},
            format="json",
        )
        self.assertEqual(verified.status_code, 200, verified.data)
        self.assertIn("no-store", verified["Cache-Control"])
        refresh = RefreshToken(verified.data["refresh"])
        self.assertIs(refresh["staff_mfa"], True)
        self.assertEqual(int(refresh["staff_mfa_at"]), int(refresh["auth_time"]))
        self.assertTrue(verified.data["staff_security_setup_required"])

        replay = self.client.post(
            reverse("staff-mfa-verify"),
            {"challenge_id": login.data["mfa_challenge_id"], "code": code},
            format="json",
        )
        self.assertEqual(replay.status_code, 400)

    def test_legacy_refresh_token_without_auth_time_is_rejected(self):
        user = self.create_user()
        legacy_refresh = RefreshToken.for_user(user)
        legacy_refresh["token_version"] = user.token_version
        response = self.client.post(
            reverse("token-refresh"), {"refresh": str(legacy_refresh)}, format="json"
        )
        self.assertEqual(response.status_code, 401)
        self.assertIn("no-store", response["Cache-Control"])

    def test_access_token_past_absolute_authentication_age_is_rejected(self):
        user = self.create_user()
        issued_at = timezone.now() - timedelta(seconds=61)
        tokens = issue_marketplace_tokens(user, authenticated_at=issued_at)
        with override_settings(MARKETPLACE_SESSION_MAX_AGE_SECONDS=60):
            self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
            response = self.client.get("/api/v1/notifications/unread-count/")
        self.assertEqual(response.status_code, 401)

    def test_staff_access_token_without_mfa_claim_is_rejected(self):
        staff = self.create_user(email="legacy-agent@example.test", staff=True)
        legacy_refresh = RefreshToken.for_user(staff)
        legacy_refresh["token_version"] = staff.token_version
        legacy_refresh["auth_time"] = int(timezone.now().timestamp())
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {str(legacy_refresh.access_token)}")
        response = self.client.get("/api/v1/support/cases/capabilities/")
        self.assertEqual(response.status_code, 401)

    def test_staff_token_issuer_refuses_to_issue_without_mfa_confirmation(self):
        staff = self.create_user(email="no-mfa@example.test", staff=True)
        with self.assertRaises(ValueError):
            issue_marketplace_tokens(staff)
