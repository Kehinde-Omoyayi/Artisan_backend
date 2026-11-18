"""A verification code that never arrives must be diagnosable in one command.

The failure is invisible from inside the app: the account is created, the code
is stored, and the person waits for a message that is stuck behind a firewall,
a blocked port or a rejected password. These tests pin the checks that name the
cause, and the retry policy that stops a dead mail server from being retried
quietly for a quarter of an hour.
"""

import io
from unittest.mock import patch

from django.core.management import call_command
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings

from . import tasks
from .otp_security import email_delivery_failure_key


def run_check(**overrides):
    out = io.StringIO()
    call_command("check_email", stdout=out, stderr=out, **overrides)
    return out.getvalue()


class CheckEmailCommandTests(SimpleTestCase):
    """`python manage.py check_email` says what is actually wrong."""

    def test_a_backend_that_does_not_use_the_network_is_reported_as_safe(self):
        with override_settings(
            EMAIL_BACKEND="django.core.mail.backends.console.EmailBackend"
        ):
            output = run_check()
        self.assertIn("does not use the network", output)
        self.assertNotIn("FAILED", output)

    def test_the_console_workaround_is_offered(self):
        with override_settings(
            EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
            EMAIL_HOST="127.0.0.1",
            EMAIL_PORT=465,
            EMAIL_USE_SSL=True,
            EMAIL_TIMEOUT=1,
        ):
            output = run_check()
        self.assertIn("console.EmailBackend", output)
        self.assertIn("What to fix", output)

    def test_ssl_on_the_starttls_port_is_called_out(self):
        with override_settings(
            EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
            EMAIL_HOST="127.0.0.1",
            EMAIL_PORT=587,
            EMAIL_USE_SSL=True,
            EMAIL_USE_TLS=False,
            EMAIL_TIMEOUT=1,
        ):
            output = run_check()
        self.assertIn("Port 587 needs STARTTLS", output)

    def test_starttls_on_the_ssl_port_is_called_out(self):
        with override_settings(
            EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
            EMAIL_HOST="127.0.0.1",
            EMAIL_PORT=465,
            EMAIL_USE_SSL=False,
            EMAIL_USE_TLS=True,
            EMAIL_TIMEOUT=1,
        ):
            output = run_check()
        self.assertIn("Port 465 is implicit SSL", output)

    def test_missing_credentials_are_called_out_without_printing_any(self):
        with override_settings(
            EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
            EMAIL_HOST="127.0.0.1",
            EMAIL_PORT=465,
            EMAIL_USE_SSL=True,
            EMAIL_TIMEOUT=1,
            EMAIL_HOST_USER="",
            EMAIL_HOST_PASSWORD="",
        ):
            output = run_check()
        self.assertIn("App Password", output)
        self.assertIn("(none configured)", output)

    def test_a_password_is_never_printed(self):
        with override_settings(
            EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
            EMAIL_HOST="127.0.0.1",
            EMAIL_PORT=465,
            EMAIL_USE_SSL=True,
            EMAIL_TIMEOUT=1,
            EMAIL_HOST_USER="ops@example.com",
            EMAIL_HOST_PASSWORD="super-secret-app-password",
        ):
            output = run_check()
        self.assertNotIn("super-secret-app-password", output)
        self.assertIn("set", output)

    def test_a_dead_server_names_the_step_that_failed(self):
        with override_settings(
            EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
            EMAIL_HOST="127.0.0.1",
            EMAIL_PORT=465,
            EMAIL_USE_SSL=True,
            EMAIL_TIMEOUT=1,
        ), patch(
            "home.management.commands.check_email.socket.create_connection",
            side_effect=TimeoutError("timed out"),
        ):
            output = run_check()
        self.assertIn("FAILED", output)
        self.assertIn("What to fix", output)

    def test_a_successful_route_says_so(self):
        """With a working server and no --to, the check ends clean."""
        with override_settings(
            EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
            EMAIL_HOST="smtp.example.test",
            EMAIL_PORT=465,
            EMAIL_USE_SSL=True,
            EMAIL_TIMEOUT=5,
            EMAIL_HOST_USER="ops@example.com",
            EMAIL_HOST_PASSWORD="secret",
        ), patch(
            "home.management.commands.check_email.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("192.0.2.1", 465))],
        ), patch("home.management.commands.check_email.socket.create_connection"), patch(
            "smtplib.SMTP_SSL"
        ) as smtp_class:
            smtp_class.return_value.ehlo.return_value = (250, b"smtp.example.test")
            smtp_class.return_value.login.return_value = {}
            output = run_check()
        self.assertIn("the credentials were accepted", output)
        self.assertIn("correctly configured", output)

    def test_a_rejected_password_is_explained(self):
        import smtplib

        with override_settings(
            EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
            EMAIL_HOST="smtp.example.test",
            EMAIL_PORT=465,
            EMAIL_USE_SSL=True,
            EMAIL_TIMEOUT=5,
            EMAIL_HOST_USER="ops@example.com",
            EMAIL_HOST_PASSWORD="secret",
        ), patch(
            "home.management.commands.check_email.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("192.0.2.1", 465))],
        ), patch("home.management.commands.check_email.socket.create_connection"), patch(
            "smtplib.SMTP_SSL"
        ) as smtp_class:
            smtp_class.return_value.ehlo.return_value = (250, b"smtp.example.test")
            smtp_class.return_value.login.side_effect = smtplib.SMTPAuthenticationError(
                535, b"Username and Password not accepted"
            )
            output = run_check()
        self.assertIn("FAILED", output)
        self.assertIn("App Password", output)


class EmailRetryPolicyTests(TestCase):
    """Retries ride out a blip; they must not become a silent fifteen-minute wait."""

    def test_the_retry_ladder_is_bounded_and_fits_the_otp_window(self):
        from home.email_errors import EMAIL_MAX_RETRIES, EMAIL_RETRY_COUNTDOWN

        self.assertEqual(EMAIL_RETRY_COUNTDOWN, (5, 15, 30, 60, 60))
        self.assertEqual(EMAIL_MAX_RETRIES, 5)
        self.assertLessEqual(sum(EMAIL_RETRY_COUNTDOWN) + 20, 300)
        self.assertEqual(tasks.send_verification_email.max_retries, EMAIL_MAX_RETRIES)
        self.assertEqual(tasks.send_password_reset_email.max_retries, EMAIL_MAX_RETRIES)
        self.assertEqual(tasks.send_otp_email.max_retries, EMAIL_MAX_RETRIES)
        self.assertEqual(tasks.send_staff_security_code.max_retries, EMAIL_MAX_RETRIES)

    def test_giving_up_is_logged_with_the_recipient(self):
        """Support needs to know who is waiting; the code stays out of the log."""
        with self.assertLogs("home.tasks", level="ERROR") as captured:
            tasks._log_email_gave_up(
                sender=tasks.send_verification_email,
                exception=TimeoutError("timed out"),
                args=("Kehinde", "waiting@example.com", "encrypted-code"),
            )
        line = captured.output[0]
        self.assertIn("email_delivery_abandoned", line)
        self.assertIn("waiting@example.com", line)
        self.assertIn("TimeoutError", line)
        self.assertNotIn("encrypted-code", line)

    def test_unrelated_task_failures_are_left_to_the_default_logging(self):
        with self.assertRaises(AssertionError):
            with self.assertLogs("home.tasks", level="ERROR"):
                tasks._log_email_gave_up(
                    sender=tasks.acquire_due_snapshot,
                    exception=TimeoutError("timed out"),
                    args=(),
                )


@override_settings(CACHES={
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "email-delivery-tests",
    }
})
class EmailDeliveryStatusTests(TestCase):
    """The verification page is told when the code never left the server."""

    def setUp(self):
        cache.clear()

    def _status(self, email):
        response = self.client.get("/api/v1/email-delivery-status/", {"email": email})
        self.assertEqual(response.status_code, 200)
        return response.json()["delivery_failed"]

    def test_a_code_that_was_sent_reports_no_failure(self):
        self.assertFalse(self._status("waiting@example.com"))

    def test_a_failed_delivery_is_reported_back(self):
        tasks._log_email_gave_up(
            sender=tasks.send_verification_email,
            exception=TimeoutError("timed out"),
            args=("Kehinde", "waiting@example.com", "encrypted-code"),
        )
        self.assertTrue(self._status("waiting@example.com"))

    def test_the_flag_is_keyed_and_not_a_readable_address_list(self):
        tasks._log_email_gave_up(
            sender=tasks.send_verification_email,
            exception=TimeoutError("timed out"),
            args=("Kehinde", "waiting@example.com", "encrypted-code"),
        )
        raw = "\n".join(f"{k}" for k in cache._cache.keys()) if hasattr(cache, "_cache") else ""
        self.assertNotIn("waiting@example.com", str(cache.get(email_delivery_failure_key("waiting@example.com"))))
        self.assertNotIn("waiting@example.com", raw)

    def test_another_address_is_unaffected(self):
        tasks._log_email_gave_up(
            sender=tasks.send_verification_email,
            exception=TimeoutError("timed out"),
            args=("Kehinde", "waiting@example.com", "encrypted-code"),
        )
        self.assertFalse(self._status("someone-else@example.com"))

    def test_a_missing_email_is_answered_calmly(self):
        self.assertFalse(self._status(""))


class DispatchEmailFallbackTests(TestCase):
    """No worker running must not mean a code nobody receives.

    `task.delay()` against a broker that is not there used to fall back to an
    inline SMTP attempt -- a mail socket inside a web request. Now the intent
    is recorded durably (`EmailDelivery`) and the watchdog re-offers it; the
    request returns immediately either way.
    """

    def setUp(self):
        cache.clear()
        from home.models import EmailDelivery

        EmailDelivery.objects.all().delete()

    def test_a_live_broker_is_still_used(self):
        with patch.object(tasks.send_verification_email, "delay") as queued:
            result = tasks.dispatch_email(tasks.send_verification_email, "Nia", "a@b.com", "code")
        self.assertEqual(result, "queued")
        queued.assert_called_once()
        args, kwargs = queued.call_args
        self.assertEqual(args[:3], ("Nia", "a@b.com", "code"))
        self.assertIn("delivery_id", kwargs)

    def test_a_dead_broker_never_opens_an_smtp_socket_in_the_request(self):
        from home.models import EmailDelivery

        with patch.object(
            tasks.send_verification_email, "delay", side_effect=OSError("no broker")
        ), patch.object(tasks.send_verification_email, "run") as inline:
            result = tasks.dispatch_email(tasks.send_verification_email, "Nia", "a@b.com", "code")
        self.assertEqual(result, "deferred")
        inline.assert_not_called()
        delivery = EmailDelivery.objects.get()
        self.assertEqual(delivery.status, EmailDelivery.Status.QUEUED)
        self.assertEqual(delivery.recipient_email, "a@b.com")
        self.assertEqual(delivery.args, ["Nia", "a@b.com", "code"])

    def test_the_broker_outage_is_logged_so_it_can_be_fixed(self):
        with self.assertLogs("home.tasks", level="WARNING") as captured:
            with patch.object(
                tasks.send_verification_email, "delay", side_effect=OSError("no broker")
            ):
                tasks.dispatch_email(tasks.send_verification_email, "Nia", "a@b.com", "code")
        self.assertTrue(
            any("email_queue_unavailable" in line for line in captured.output),
            captured.output,
        )

    def test_a_recording_failure_does_not_raise_into_the_request(self):
        from home.models import EmailDelivery

        with patch(
            "home.tasks.create_delivery", side_effect=RuntimeError("db down")
        ), patch.object(tasks.send_verification_email, "delay") as queued:
            result = tasks.dispatch_email(tasks.send_verification_email, "Nia", "a@b.com", "code")
        self.assertEqual(result, "deferred")
        queued.assert_not_called()

    def test_registration_still_answers_201_when_there_is_no_worker(self):
        """The account is created, the intent is recorded, the API answers."""
        from rest_framework.test import APIClient
        from home.models import EmailDelivery

        with patch.object(
            tasks.send_verification_email, "delay", side_effect=OSError("no broker")
        ), patch.object(tasks.send_verification_email, "run") as inline:
            response = APIClient().post(
                "/api/v1/register/",
                {
                    "email": "noworker@example.com",
                    "password": "StrongPassword123!",
                    "password2": "StrongPassword123!",
                    "first_name": "Nia",
                    "last_name": "Okafor",
                    "phone_number": "+2348012345678",
                    "role": "customer",
                },
                format="json",
            )
        self.assertIn(response.status_code, (200, 201), response.data)
        inline.assert_not_called()
        self.assertTrue(
            EmailDelivery.objects.filter(recipient_email="noworker@example.com", status="queued").exists()
        )
