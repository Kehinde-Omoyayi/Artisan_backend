import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import Group
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from core.models import AuditLog
from home.models import StaffPasswordResetToken, StaffSecurityChallenge, StaffSecurityProfile
from home.security_mail import decrypt_staff_code
from home.auth_tokens import issue_marketplace_tokens
from home.password_policy import is_staff_account
from home.security_questions import normalize_security_answer
from support.permissions import SUPPORT_AGENT

User = get_user_model()

TEST_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "staff-security-tests",
    }
}
TEST_HASHERS = ["django.contrib.auth.hashers.PBKDF2PasswordHasher"]


def access_token(user):
    return issue_marketplace_tokens(
        user,
        staff_mfa_verified=is_staff_account(user),
    )["access"]


def answers_payload():
    return [
        {"question_id": "first_school", "answer": "Bright Future School"},
        {"question_id": "first_job", "answer": "shop assistant"},
        {"question_id": "first_pet", "answer": "Bingo"},
    ]


def create_security_profile(user):
    questions = answers_payload()
    hashes = [make_password(normalize_security_answer(row["answer"])) for row in questions]
    return StaffSecurityProfile.objects.create(
        user=user,
        question_1=questions[0]["question_id"], answer_hash_1=hashes[0],
        question_2=questions[1]["question_id"], answer_hash_2=hashes[1],
        question_3=questions[2]["question_id"], answer_hash_3=hashes[2],
    )


@override_settings(CACHES=TEST_CACHES, PASSWORD_HASHERS=TEST_HASHERS)
class StaffAccessBoundaryTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            email="agent@example.test", first_name="Ayo", last_name="Agent",
            password="Existing!Password84", is_active=True, is_staff=True,
        )
        self.staff.groups.add(Group.objects.get_or_create(name=SUPPORT_AGENT)[0])
        create_security_profile(self.staff)
        self.manager = User.objects.create_superuser(
            email="manager@example.test", first_name="Mira", last_name="Manager",
            password="Manager!Password84",
        )
        create_security_profile(self.manager)

    def test_expired_staff_password_blocks_work_but_allows_verified_password_change(self):
        self.staff.password_changed_at = timezone.now() - timedelta(days=8)
        self.staff.save(update_fields=["password_changed_at"])
        token = access_token(self.staff)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

        blocked = client.get("/api/v1/support/cases/capabilities/")
        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(blocked.data["code"], "PASSWORD_CHANGE_REQUIRED")

        changed = client.post("/api/v1/change-password/", {
            "current_password": "Existing!Password84",
            "new_password": "Newer!SecurePassword96",
            "confirm_password": "Newer!SecurePassword96",
        }, format="json")
        self.assertEqual(changed.status_code, 200, changed.data)
        self.staff.refresh_from_db()
        self.assertTrue(self.staff.check_password("Newer!SecurePassword96"))
        self.assertGreater(self.staff.password_changed_at, timezone.now() - timedelta(minutes=1))
        self.assertEqual(self.staff.token_version, 2)

        revoked = client.get("/api/v1/support/cases/capabilities/")
        self.assertEqual(revoked.status_code, 401)

    def test_support_staff_are_denied_unrelated_marketplace_and_manager_endpoints(self):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token(self.staff)}")

        support = client.get("/api/v1/support/cases/capabilities/")
        self.assertEqual(support.status_code, 200)
        self.assertTrue(support.data["is_support_staff"])

        marketplace = client.get("/api/v1/services/get_artisan_services/")
        self.assertEqual(marketplace.status_code, 403)
        self.assertEqual(marketplace.data["code"], "STAFF_ACCESS_RESTRICTED")

        manager = client.get("/api/v1/manager/overview/")
        self.assertEqual(manager.status_code, 403)
        self.assertEqual(manager.data["code"], "STAFF_ACCESS_RESTRICTED")

    def test_manager_analytics_requires_superuser_records_access_and_is_not_cached(self):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token(self.manager)}")
        with patch(
            "home.manager_views.build_manager_overview",
            return_value={"generated_at": timezone.now(), "kpis": {}},
        ):
            response = client.get("/api/v1/manager/overview/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertTrue(AuditLog.objects.filter(
            action="MANAGER_ANALYTICS_ACCESSED",
            resource_type="ManagerAnalytics",
            resource_id="site-overview",
            actor=self.manager,
        ).exists())

    def test_role_change_is_audited_and_revokes_existing_staff_sessions(self):
        original_version = self.staff.token_version
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token(self.manager)}")
        response = client.put(
            f"/api/v1/support/cases/staff/{self.staff.pk}/roles/",
            {"roles": ["CUSTOMER_SUPPORT_SUPERVISOR"]},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.staff.refresh_from_db()
        self.assertEqual(self.staff.token_version, original_version + 1)
        self.assertTrue(AuditLog.objects.filter(
            action="SUPPORT_STAFF_ROLES_UPDATED", resource_id=str(self.staff.pk)
        ).exists())


@override_settings(CACHES=TEST_CACHES, PASSWORD_HASHERS=TEST_HASHERS)
class StaffRecoveryFlowTests(TestCase):
    def setUp(self):
        self.manager = User.objects.create_superuser(
            email="manager@example.test", first_name="Mira", last_name="Manager",
            password="Manager!Password84",
        )
        self.staff = User.objects.create_user(
            email="agent@example.test", first_name="Ayo", last_name="Agent",
            password="Existing!Password84", is_active=True, is_staff=True,
        )
        self.staff.groups.add(Group.objects.get_or_create(name=SUPPORT_AGENT)[0])
        self.manager_client = APIClient()
        self.manager_client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token(self.manager)}")

    def _initiate(self, *, setup=False):
        suffix = "security-setup/" if setup else "initiate/"
        route = f"/api/v1/staff-password-resets/{self.staff.pk}/{suffix}"
        deliver_path = "home.staff_security_views.send_staff_security_code.delay"
        with patch(deliver_path) as deliver:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.manager_client.post(route, {}, format="json")
            self.assertEqual(response.status_code, 202, response.data)
            self.assertEqual(deliver.call_count, 1)
            challenge_id, encrypted_code = deliver.call_args.args
        return StaffSecurityChallenge.objects.get(pk=challenge_id), decrypt_staff_code(encrypted_code)

    def _recovery_payload(self, otp, questions=None):
        return {
            "email": self.staff.email,
            "otp": otp,
            "questions": questions or answers_payload(),
        }

    def test_initial_setup_is_manager_authorized_and_does_not_invent_answers(self):
        reset = self.manager_client.post(
            f"/api/v1/staff-password-resets/{self.staff.pk}/initiate/", {}, format="json"
        )
        self.assertEqual(reset.status_code, 409)
        self.assertEqual(reset.data["code"], "STAFF_SECURITY_SETUP_REQUIRED")

        challenge, otp = self._initiate(setup=True)
        response = APIClient().post(
            "/api/v1/staff-password-resets/security-setup/complete/",
            self._recovery_payload(otp), format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        challenge.refresh_from_db()
        self.assertIsNotNone(challenge.consumed_at)
        profile = StaffSecurityProfile.objects.get(user=self.staff)
        self.assertNotEqual(profile.answer_hash_1, "bright future school")
        self.assertTrue(profile.answer_hash_1.startswith("pbkdf2_sha256$") or profile.answer_hash_1.startswith("argon2$"))

        reused = APIClient().post(
            "/api/v1/staff-password-resets/security-setup/complete/",
            self._recovery_payload(otp), format="json",
        )
        self.assertEqual(reused.status_code, 400)

    def test_staff_reset_requires_otp_and_every_registered_answer_and_is_single_use(self):
        create_security_profile(self.staff)
        challenge, otp = self._initiate()
        wrong_answers = answers_payload()
        wrong_answers[2] = {"question_id": "first_pet", "answer": "not bingo"}
        rejected = APIClient().post(
            "/api/v1/staff-password-resets/verify/",
            self._recovery_payload(otp, wrong_answers), format="json",
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertNotIn("reset_token", rejected.data)
        challenge.refresh_from_db()
        self.assertEqual(challenge.attempts, 1)
        self.assertIsNone(challenge.consumed_at)

        verified = APIClient().post(
            "/api/v1/staff-password-resets/verify/",
            self._recovery_payload(otp), format="json",
        )
        self.assertEqual(verified.status_code, 200, verified.data)
        reset_token = verified.data["reset_token"]
        token_record = StaffPasswordResetToken.objects.get(user=self.staff)
        self.assertNotEqual(token_record.token_hash, reset_token)
        self.assertIsNone(token_record.consumed_at)
        challenge.refresh_from_db()
        self.assertIsNotNone(challenge.consumed_at)

        reused = APIClient().post(
            "/api/v1/staff-password-resets/verify/",
            self._recovery_payload(otp), format="json",
        )
        self.assertEqual(reused.status_code, 400)

        completed = APIClient().post("/api/v1/staff-password-resets/complete/", {
            "reset_token": reset_token,
            "new_password": "Replacement!StaffPassword62",
            "confirm_password": "Replacement!StaffPassword62",
        }, format="json")
        self.assertEqual(completed.status_code, 200, completed.data)
        self.staff.refresh_from_db()
        token_record.refresh_from_db()
        self.assertTrue(self.staff.check_password("Replacement!StaffPassword62"))
        self.assertIsNotNone(self.staff.password_changed_at)
        self.assertEqual(self.staff.token_version, 2)
        self.assertIsNotNone(token_record.consumed_at)

        second_use = APIClient().post("/api/v1/staff-password-resets/complete/", {
            "reset_token": reset_token,
            "new_password": "Another!ReplacementPassword63",
            "confirm_password": "Another!ReplacementPassword63",
        }, format="json")
        self.assertEqual(second_use.status_code, 400)
        self.assertTrue(AuditLog.objects.filter(
            action="STAFF_PASSWORD_RESET_COMPLETED", resource_id=str(self.staff.pk)
        ).exists())
