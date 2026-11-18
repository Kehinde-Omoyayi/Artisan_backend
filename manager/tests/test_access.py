"""Authentication, authorisation and privilege boundaries."""
from django.contrib.auth import get_user_model

from manager.models import ManagerProfile, ManagerRole
from manager.services import auth as auth_service

from .base import ManagerTestCase

User = get_user_model()


class ManagerAccessTests(ManagerTestCase):
    def test_a_customer_cannot_reach_the_console(self):
        self.login(self.customer_user)
        for path in ("/api/v1/manager/analytics/overview/", "/api/v1/manager/config/", "/api/v1/manager/staff/"):
            response = self.client.get(path)
            self.assertIn(response.status_code, (403,), path)

    def test_an_artisan_cannot_reach_the_console(self):
        self.login(self.artisan_user)
        response = self.client.get("/api/v1/manager/analytics/overview/")
        self.assertEqual(response.status_code, 403)

    def test_anonymous_is_rejected(self):
        response = self.client.get("/api/v1/manager/analytics/overview/")
        self.assertIn(response.status_code, (401, 403))

    def test_is_staff_alone_does_not_make_a_manager(self):
        staff = User.objects.create_user(email="staff@example.com", password="Passw0rd!234",
                                         first_name="Plain", last_name="Staff", is_staff=True, is_active=True)
        self.login(staff)
        # A staff account without a manager role gets no console access...
        self.assertFalse(auth_service.has_perm(staff, "analytics.financial"))
        # ...but the support agent profile the platform provisions is what grants support work.
        response = self.client.get("/api/v1/manager/financial/")
        self.assertEqual(response.status_code, 403)

    def test_a_non_superuser_manager_with_a_role_reaches_the_console_api(self):
        """The staff-access boundary must not lock granular manager roles out.

        ``home.authentication`` restricts staff accounts to their own surface; a
        manager role is a legitimate surface, and every manager endpoint still
        checks its own permission code.
        """
        account = User.objects.create_user(
            email="finance.manager@example.com", password="Passw0rd!234",
            first_name="Finance", last_name="Manager", is_staff=True, is_active=True,
        )
        role, _ = ManagerRole.objects.get_or_create(
            slug="finance-manager", defaults={"name": "Finance manager", "permissions": []})
        role.permissions = ["analytics.view", "analytics.financial"]
        role.save()
        ManagerProfile.objects.update_or_create(user=account, defaults={"role": role})
        self.login(account)

        self.assertEqual(self.client.get("/api/v1/manager/financial/").status_code, 200)
        # A permission outside the role is still refused.
        self.assertEqual(self.client.get("/api/v1/manager/staff/").status_code, 403)

    def test_superuser_is_provisioned_as_super_administrator(self):
        self.login_superuser()
        profile = auth_service.ensure_profile(self.superuser)
        self.assertIsNotNone(profile)
        self.assertEqual(profile.role.slug, "super-admin")
        self.assertIn("config.approve", auth_service.effective_permissions(self.superuser))

    def test_a_suspended_manager_loses_access_immediately(self):
        self.login(self.finance)
        self.assertEqual(self.client.get("/api/v1/manager/financial/").status_code, 200)
        profile = ManagerProfile.objects.get(user=self.finance)
        profile.is_suspended = True
        profile.save(update_fields=["is_suspended"])
        self.assertEqual(self.client.get("/api/v1/manager/financial/").status_code, 403)

    def test_a_role_without_the_permission_is_refused(self):
        role = ManagerRole.objects.create(slug="marketing-only", name="Marketing only",
                                          permissions=["campaign.view"])
        user = User.objects.create_user(email="mkt@example.com", password="Passw0rd!234",
                                        first_name="Mia", last_name="Marketing", is_staff=True, is_active=True)
        ManagerProfile.objects.create(user=user, role=role)
        self.login(user)
        self.assertEqual(self.client.get("/api/v1/manager/campaigns/").status_code, 200)
        self.assertEqual(self.client.get("/api/v1/manager/financial/").status_code, 403)
        self.assertEqual(self.client.get("/api/v1/manager/staff/").status_code, 403)

    def test_session_payload_lists_only_real_permissions(self):
        self.login(self.finance)
        payload = self.client.get("/api/v1/manager/session/").json()
        self.assertTrue(payload["is_manager"])
        self.assertIn("config.finance", payload["permissions"])
        self.assertNotIn("staff.manage", payload["permissions"])


class StepUpTests(ManagerTestCase):
    def test_sensitive_write_is_refused_without_a_step_up_token(self):
        self.login_superuser()
        response = self.client.post("/api/v1/manager/config/fees.commission_rate/change/",
                                    {"value": "0.12", "reason": "test"}, format="json")
        self.assertEqual(response.status_code, 403)

    def test_a_wrong_password_cannot_mint_a_token(self):
        self.login_superuser()
        response = self.client.post("/api/v1/manager/auth/step-up/",
                                    {"scope": "config:finance", "password": "wrong"}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_step_up_without_any_second_factor_is_refused(self):
        self.login_superuser()
        response = self.client.post("/api/v1/manager/auth/step-up/",
                                    {"scope": "config:finance", "password": "Passw0rd!234"}, format="json")
        self.assertEqual(response.status_code, 403)
        self.assertIn("verification", response.json()["detail"].lower())

    def test_a_wrong_email_code_is_rejected_and_the_right_one_works_once(self):
        self.login_superuser()
        auth_service.issue_email_challenge(self.superuser, "STEP_UP", code="654321")
        wrong = self.client.post("/api/v1/manager/auth/step-up/",
                                 {"scope": "config:finance", "password": "Passw0rd!234", "code": "111111"},
                                 format="json")
        self.assertEqual(wrong.status_code, 400)
        good = self.client.post("/api/v1/manager/auth/step-up/",
                                {"scope": "config:finance", "password": "Passw0rd!234", "code": "654321"},
                                format="json")
        self.assertEqual(good.status_code, 200, good.content)
        reused = self.client.post("/api/v1/manager/auth/step-up/",
                                  {"scope": "config:finance", "password": "Passw0rd!234", "code": "654321"},
                                  format="json")
        self.assertEqual(reused.status_code, 400)

    def test_an_expired_email_code_is_rejected(self):
        from datetime import timedelta

        from django.utils import timezone

        self.login_superuser()
        challenge = auth_service.issue_email_challenge(self.superuser, "STEP_UP", code="222222")
        challenge.expires_at = timezone.now() - timedelta(seconds=1)
        challenge.save(update_fields=["expires_at"])
        response = self.client.post("/api/v1/manager/auth/step-up/",
                                    {"scope": "config:finance", "password": "Passw0rd!234", "code": "222222"},
                                    format="json")
        self.assertEqual(response.status_code, 400)

    def test_totp_enrolment_then_step_up_then_single_use(self):
        from unittest import mock

        self.login_superuser()
        enrolled = self.client.post("/api/v1/manager/auth/totp/enroll/", {}, format="json").json()
        code = auth_service.current_totp(enrolled["secret"])
        self.assertEqual(self.client.post("/api/v1/manager/auth/totp/confirm/", {"code": code},
                                          format="json").status_code, 200)

        # Pin the clock so the verification code and the server agree on the step,
        # and the confirmation code above cannot be replayed.
        later = 1_900_000_000.0
        next_code = auth_service.current_totp(enrolled["secret"], at=later)
        with mock.patch("manager.services.auth.time.time", return_value=later):
            grant = self.client.post("/api/v1/manager/auth/step-up/",
                                     {"scope": "config:finance", "password": "Passw0rd!234", "code": next_code},
                                     format="json")
        self.assertEqual(grant.status_code, 200, grant.content)
        token = grant.json()["token"]

        first = self.client.post("/api/v1/manager/config/fees.commission_rate/change/",
                                 {"value": "0.12", "reason": "raise commission", "step_up_token": token},
                                 format="json")
        self.assertEqual(first.status_code, 201, first.content)

        replay = self.client.post("/api/v1/manager/config/fees.commission_rate/change/",
                                  {"value": "0.13", "reason": "replay", "step_up_token": token}, format="json")
        self.assertEqual(replay.status_code, 403)

    def test_an_unreadable_authenticator_secret_is_a_clear_refusal_not_a_crash(self):
        """A rotated encryption key must not turn sign-in into a 500."""
        self.login_superuser()
        enrolled = self.client.post("/api/v1/manager/auth/totp/enroll/", {}, format="json").json()
        auth_service.confirm_totp_enrollment(self.superuser, auth_service.current_totp(enrolled["secret"]))
        profile = self.superuser.manager_profile
        profile.totp_secret_encrypted = "not-a-fernet-token"
        profile.save(update_fields=["totp_secret_encrypted"])

        response = self.client.post("/api/v1/manager/auth/step-up/",
                                    {"scope": "config:finance", "password": "Passw0rd!234",
                                     "code": "123456"}, format="json")
        self.assertEqual(response.status_code, 403, response.content)
        self.assertIn("cannot be read", response.json()["detail"])
        self.assertNotIn("Traceback", response.content.decode())

        confirm = self.client.post("/api/v1/manager/auth/totp/confirm/", {"code": "123456"}, format="json")
        self.assertEqual(confirm.status_code, 403)

    def test_the_step_up_code_reaches_the_manager(self):
        """A manager without an authenticator must be able to receive a code.

        The delivery task is called for real (Celery runs eager in tests); only the
        SMTP hop is captured, because no mail server is part of the test setup.
        """
        from unittest import mock

        from django.core import mail

        from manager.models import ManagerOTPChallenge

        self.approver.manager_profile.email_otp_enabled = True
        self.approver.manager_profile.save(update_fields=["email_otp_enabled"])
        challenge = auth_service.issue_email_challenge(self.approver, "STEP_UP")
        self.assertIsInstance(challenge, ManagerOTPChallenge)
        with mock.patch("home.email_templates.send_security_code_email") as sender:
            from manager import tasks

            result = tasks.send_manager_step_up_code(str(challenge.id), "")
            self.assertTrue(callable(sender))
        # The task refuses an undecryptable payload rather than crashing the worker.
        self.assertFalse(result["sent"])

        code = "424242"
        fresh = auth_service.issue_email_challenge(self.approver, "STEP_UP", code=code)
        from home.security_mail import encrypt_email_code

        with mock.patch("home.email_templates.send_security_code_email") as sender:
            result = tasks.send_manager_step_up_code(str(fresh.id), encrypt_email_code(code))
        self.assertTrue(result["sent"], result)
        self.assertEqual(sender.call_args.kwargs["recipient_email"], self.approver.email)
        self.assertEqual(sender.call_args.kwargs["code"], code)
        self.assertEqual(len(mail.outbox), 0, "the task never falls back to a raw mail()")

        # A consumed challenge is never mailed, even if a stale task runs late.
        auth_service.verify_email_challenge(self.approver, "STEP_UP", code)
        with mock.patch("home.email_templates.send_security_code_email") as sender:
            late = tasks.send_manager_step_up_code(str(fresh.id), encrypt_email_code(code))
        self.assertFalse(late["sent"])
        sender.assert_not_called()

    def test_a_totp_code_cannot_be_reused(self):
        self.login_superuser()
        enrolled = self.client.post("/api/v1/manager/auth/totp/enroll/", {}, format="json").json()
        code = auth_service.current_totp(enrolled["secret"])
        auth_service.confirm_totp_enrollment(self.superuser, code)
        self.assertTrue(auth_service.verify_manager_totp(self.superuser, code) is False)
