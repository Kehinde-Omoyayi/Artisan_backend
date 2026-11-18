from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.test import TestCase, override_settings
from django.utils import timezone

from home.models import StaffSecurityChallenge, StaffSecurityProfile


TEST_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "staff-admin-mfa-tests",
    }
}
TEST_HASHERS = ["django.contrib.auth.hashers.PBKDF2PasswordHasher"]
User = get_user_model()


@override_settings(CACHES=TEST_CACHES, PASSWORD_HASHERS=TEST_HASHERS)
class StaffAdminMFATests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            email="admin-staff@example.test",
            first_name="Ayo",
            last_name="Admin",
            password="Existing!Password84",
            is_active=True,
            is_staff=True,
        )
        StaffSecurityProfile.objects.create(
            user=self.staff,
            question_1="first_school",
            answer_hash_1=make_password("bright future school"),
            question_2="first_job",
            answer_hash_2=make_password("shop assistant"),
            question_3="first_pet",
            answer_hash_3=make_password("bingo"),
        )
        self.client.force_login(self.staff)

    def _start_admin_mfa(self, *, code=334567, path="/admin/"):
        with (
            patch("home.staff_mfa.secrets.randbelow", return_value=code - 100000),
            patch("home.staff_mfa.send_staff_security_code.delay"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            response = self.client.get(path)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/admin/mfa/")
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        return code

    def test_admin_setup_gate_logs_staff_out_until_recovery_questions_are_registered(self):
        self.staff.staff_security_questions.delete()

        response = self.client.get("/admin/")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/auth/login/?next=%2Fadmin%2F")
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertIn("no-store", response["Cache-Control"])

    def test_admin_requires_single_use_email_mfa_and_sets_no_store_headers(self):
        code = self._start_admin_mfa()
        session = self.client.session
        challenge_id = session["admin_mfa_challenge_id"]
        challenge = StaffSecurityChallenge.objects.get(pk=challenge_id)

        prompt = self.client.get("/admin/mfa/")
        self.assertEqual(prompt.status_code, 200)
        self.assertIn("no-store", prompt["Cache-Control"])

        verified = self.client.post("/admin/mfa/", {"code": str(code)})
        self.assertEqual(verified.status_code, 302)
        self.assertEqual(verified["Location"], "/admin/")
        self.assertIn("no-store", verified["Cache-Control"])
        challenge.refresh_from_db()
        self.assertIsNotNone(challenge.consumed_at)
        self.assertEqual(self.client.session["admin_mfa_user_id"], str(self.staff.pk))

        index = self.client.get("/admin/")
        self.assertEqual(index.status_code, 200)
        self.assertIn("no-store", index["Cache-Control"])

        reused = self.client.post("/admin/mfa/", {"code": str(code)})
        self.assertEqual(reused.status_code, 200)
        self.assertNotEqual(
            self.client.session["admin_mfa_challenge_id"],
            str(challenge.pk),
        )

    def test_expired_staff_challenge_cleanup_preserves_recent_or_live_challenges(self):
        from home.tasks import purge_expired_staff_security_challenges

        now = timezone.now()
        old = StaffSecurityChallenge.objects.create(
            user=self.staff,
            purpose=StaffSecurityChallenge.Purpose.LOGIN_MFA,
            otp_hash=make_password("123456"),
            created_at=now - timedelta(days=40),
            expires_at=now - timedelta(days=31),
        )
        recently_expired = StaffSecurityChallenge.objects.create(
            user=self.staff,
            purpose=StaffSecurityChallenge.Purpose.LOGIN_MFA,
            otp_hash=make_password("234567"),
            expires_at=now - timedelta(days=1),
        )
        live = StaffSecurityChallenge.objects.create(
            user=self.staff,
            purpose=StaffSecurityChallenge.Purpose.LOGIN_MFA,
            otp_hash=make_password("345678"),
            expires_at=now + timedelta(minutes=5),
        )

        result = purge_expired_staff_security_challenges.run(batch_size=10)

        self.assertEqual(result, {"deleted": 1})
        self.assertFalse(StaffSecurityChallenge.objects.filter(pk=old.pk).exists())
        self.assertTrue(StaffSecurityChallenge.objects.filter(pk=recently_expired.pk).exists())
        self.assertTrue(StaffSecurityChallenge.objects.filter(pk=live.pk).exists())

    def test_global_admin_password_change_revokes_api_sessions_and_rechecks_mfa(self):
        self.staff.password_changed_at = timezone.now() - timedelta(days=8)
        self.staff.save(update_fields=["password_changed_at"])
        old_token_version = self.staff.token_version

        started = self._start_admin_mfa(path="/admin/password_change/")
        verified = self.client.post("/admin/mfa/", {"code": str(started)})
        self.assertEqual(verified.status_code, 302)

        password_page = self.client.get("/admin/password_change/")
        self.assertEqual(password_page.status_code, 200)
        self.assertIn("no-store", password_page["Cache-Control"])

        rejected = self.client.post("/admin/password_change/", {
            "old_password": "Wrong!OldPassword84",
            "new_password1": "New!AdminPassword5901",
            "new_password2": "New!AdminPassword5901",
        })
        self.assertEqual(rejected.status_code, 200)
        self.staff.refresh_from_db()
        self.assertTrue(self.staff.check_password("Existing!Password84"))
        self.assertEqual(self.staff.token_version, old_token_version)

        changed = self.client.post("/admin/password_change/", {
            "old_password": "Existing!Password84",
            "new_password1": "New!AdminPassword5901",
            "new_password2": "New!AdminPassword5901",
        })
        self.assertEqual(changed.status_code, 302)
        self.assertEqual(changed["Location"], "/admin/")
        self.staff.refresh_from_db()
        self.assertTrue(self.staff.check_password("New!AdminPassword5901"))
        self.assertEqual(self.staff.token_version, old_token_version + 1)
        self.assertGreater(self.staff.password_changed_at, timezone.now() - timedelta(minutes=1))
        session = self.client.session
        self.assertNotIn("admin_mfa_authenticated_at", session)

        next_admin_request = self.client.get("/admin/")
        self.assertEqual(next_admin_request.status_code, 302)
        self.assertEqual(next_admin_request["Location"], "/admin/mfa/")
        self.assertIn("no-store", next_admin_request["Cache-Control"])
