from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone

from .models import StaffSecurityChallenge
from .security_mail import encrypt_email_code
from .tasks import (
    send_otp_email,
    send_password_reset_email,
    send_staff_security_code,
    send_verification_email,
)


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="security-tests@example.invalid",
)
class BrandedSecurityEmailTests(TestCase):
    def setUp(self):
        mail.outbox.clear()

    def test_verification_reset_and_otp_emails_have_branded_html_and_plain_text(self):
        dummy_code = "654321"
        tasks = (
            (send_verification_email, "Verify Your Artisan Platform Account"),
            (send_password_reset_email, "Password Reset Verification Code"),
            (send_otp_email, "Your Artisan Platform Verification Code"),
        )
        for task, expected_subject in tasks:
            with self.subTest(task=task.name):
                mail.outbox.clear()
                task.run("Ada", "ada@example.test", encrypt_email_code(dummy_code))
                self.assertEqual(len(mail.outbox), 1)
                message = mail.outbox[0]
                self.assertEqual(message.subject, expected_subject)
                self.assertIn(dummy_code, message.body)
                self.assertIn("expires in 5 minutes", message.body)
                self.assertNotIn("<html", message.body.lower())
                self.assertTrue(message.alternatives)
                alternative = message.alternatives[0]
                html = alternative.content if hasattr(alternative, "content") else alternative[0]
                mimetype = alternative.mimetype if hasattr(alternative, "mimetype") else alternative[1]
                self.assertEqual(mimetype, "text/html")
                self.assertIn("Artisan Market", html)
                self.assertIn("#faf7f1", html)
                self.assertIn("#4c3025", html)
                self.assertIn("EMAIL VERIFICATION CODE" if task is send_verification_email else dummy_code, html)
                self.assertIn(dummy_code, html)
                self.assertIn("single use", html)

    @override_settings(SITE_URL="https://market.example.test")
    def test_staff_security_code_keeps_the_optional_secure_action_link(self):
        staff = get_user_model().objects.create_user(
            email="staff@example.test",
            first_name="Staff",
            last_name="Member",
            password=None,
            is_staff=True,
            is_active=True,
        )
        challenge = StaffSecurityChallenge.objects.create(
            user=staff,
            purpose=StaffSecurityChallenge.Purpose.LOGIN_MFA,
            otp_hash="not-a-real-provider-or-secret-hash",
            expires_at=timezone.now() + timedelta(minutes=10),
        )

        result = send_staff_security_code.run(str(challenge.pk), encrypt_email_code("987654"))

        self.assertEqual(result, {"sent": True, "purpose": "staff sign-in"})
        message = mail.outbox[0]
        self.assertIn("987654", message.body)
        html = message.alternatives[0].content
        self.assertIn("Confirm your staff sign-in", html)
        self.assertIn("https://market.example.test/auth/staff-mfa/", html)
        self.assertIn("#4c3025", html)

    def test_security_email_html_escapes_recipient_data(self):
        mail.outbox.clear()
        send_verification_email.run(
            "Ada <script>alert(1)</script>",
            "ada@example.test",
            encrypt_email_code("123456"),
        )
        html = mail.outbox[0].alternatives[0].content
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>", html)
