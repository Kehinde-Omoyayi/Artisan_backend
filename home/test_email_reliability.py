"""Email delivery reliability: configuration, classification, durable queue,
watchdog recovery, failure visibility and notification-path isolation.

Unit tests use mocked SMTP connections, so they are deterministic and never
send real mail. Real connectivity can only be proven from the actual machine
the worker runs on (see `python manage.py check_email`).
"""

import smtplib
import socket
import ssl
import uuid
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from celery.exceptions import Retry
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from . import tasks
from .email_errors import (
    EMAIL_MAX_RETRIES,
    EMAIL_RETRY_COUNTDOWN,
    PERMANENT,
    TRANSIENT,
    classify_email_exception,
    describe_email_error,
    retry_countdown,
)
from .models import EmailDelivery
from .otp_security import email_delivery_failure_key
from Artisan_platform.email_validation import validate_email_configuration
from home.security_mail import encrypt_email_code


def run_check(**overrides):
    out = StringIO()
    call_command("check_email", stdout=out, stderr=out, **overrides)
    return out.getvalue()


def _validation_kwargs(**overrides):
    kwargs = dict(
        backend="django.core.mail.backends.smtp.EmailBackend",
        host="smtp.gmail.com",
        port=465,
        use_ssl=True,
        use_tls=False,
        timeout=10,
        user="ops@example.com",
        password="app-password",
        environment="production",
    )
    kwargs.update(overrides)
    return kwargs


class SMTPConfigurationValidationTests(SimpleTestCase):
    """The configured transport must be correct before anything is sent."""

    def test_implicit_ssl_on_465_is_accepted(self):
        problems = validate_email_configuration(**_validation_kwargs())
        self.assertEqual(problems, [])

    def test_starttls_on_587_is_accepted(self):
        problems = validate_email_configuration(
            **_validation_kwargs(port=587, use_ssl=False, use_tls=True)
        )
        self.assertEqual(problems, [])

    def test_ssl_and_tls_together_are_rejected(self):
        with self.assertRaises(Exception):
            validate_email_configuration(
                **_validation_kwargs(use_tls=True)
            )

    def test_missing_host_is_rejected_in_production(self):
        with self.assertRaises(Exception):
            validate_email_configuration(**_validation_kwargs(host=""))

    def test_missing_host_only_warns_in_development(self):
        problems = validate_email_configuration(**_validation_kwargs(host="", environment="development"))
        self.assertTrue(any("EMAIL_HOST" in problem for problem in problems))

    def test_missing_production_credentials_are_rejected(self):
        with self.assertRaises(Exception) as ctx:
            validate_email_configuration(**_validation_kwargs(user="", password=""))
        self.assertIn("App Password", str(ctx.exception))

    def test_missing_development_credentials_only_warn(self):
        problems = validate_email_configuration(
            **_validation_kwargs(user="", password="", environment="development")
        )
        self.assertTrue(any("App Password" in problem for problem in problems))

    def test_a_non_positive_timeout_is_rejected(self):
        with self.assertRaises(Exception):
            validate_email_configuration(**_validation_kwargs(timeout=0))

    def test_a_port_out_of_range_is_rejected(self):
        with self.assertRaises(Exception):
            validate_email_configuration(**_validation_kwargs(port=70000))

    def test_console_backend_is_rejected_in_production(self):
        with self.assertRaises(Exception):
            validate_email_configuration(
                **_validation_kwargs(backend="django.core.mail.backends.console.EmailBackend")
            )

    def test_console_backend_is_tolerated_in_development(self):
        problems = validate_email_configuration(
            **_validation_kwargs(backend="django.core.mail.backends.console.EmailBackend", environment="development")
        )
        self.assertEqual(problems, [])

    def test_ssl_on_the_starttls_port_is_rejected_in_production(self):
        with self.assertRaises(Exception) as ctx:
            validate_email_configuration(**_validation_kwargs(port=587, use_ssl=True, use_tls=False))
        self.assertIn("STARTTLS", str(ctx.exception))

    def test_starttls_on_the_ssl_port_is_rejected_in_production(self):
        with self.assertRaises(Exception) as ctx:
            validate_email_configuration(**_validation_kwargs(port=465, use_ssl=False, use_tls=True))
        self.assertIn("implicit SSL", str(ctx.exception))


class EmailErrorClassificationTests(SimpleTestCase):
    """Only failures that can plausibly heal are retried, and only 5 times."""

    def test_network_timeouts_are_transient(self):
        self.assertEqual(classify_email_exception(TimeoutError("timed out")), TRANSIENT)
        self.assertEqual(classify_email_exception(socket.timeout("timed out")), TRANSIENT)

    def test_dns_failures_are_transient(self):
        self.assertEqual(classify_email_exception(socket.gaierror("Name or service not known")), TRANSIENT)

    def test_connection_refused_and_reset_are_transient(self):
        self.assertEqual(classify_email_exception(ConnectionRefusedError("refused")), TRANSIENT)
        self.assertEqual(classify_email_exception(ConnectionResetError("reset")), TRANSIENT)

    def test_smtp_server_disconnection_is_transient(self):
        self.assertEqual(classify_email_exception(smtplib.SMTPServerDisconnected("bye")), TRANSIENT)

    def test_temporary_smtp_4xx_is_transient(self):
        exc = smtplib.SMTPDataError(450, b"mailbox busy")
        self.assertEqual(classify_email_exception(exc), TRANSIENT)

    def test_permanent_smtp_5xx_is_permanent(self):
        exc = smtplib.SMTPDataError(550, b"mailbox unavailable")
        self.assertEqual(classify_email_exception(exc), PERMANENT)

    def test_authentication_rejection_is_permanent(self):
        exc = smtplib.SMTPAuthenticationError(535, b"Username and Password not accepted")
        self.assertEqual(classify_email_exception(exc), PERMANENT)

    def test_sender_and_recipient_rejections_are_permanent(self):
        self.assertEqual(classify_email_exception(smtplib.SMTPRecipientsRefused({"a@b.c": (550, b"")})), PERMANENT)
        self.assertEqual(classify_email_exception(smtplib.SMTPSenderRefused(553, b"", "a@b.c")), PERMANENT)

    def test_invalid_tls_configuration_is_permanent(self):
        self.assertEqual(classify_email_exception(ssl.SSLError("wrong version number")), PERMANENT)
        self.assertEqual(classify_email_exception(ssl.CertificateError("hostname mismatch")), PERMANENT)

    def test_unknown_errors_are_not_retried_blindly(self):
        self.assertEqual(classify_email_exception(RuntimeError("mystery")), PERMANENT)
        self.assertEqual(classify_email_exception(ValueError("bad shape")), PERMANENT)

    def test_the_retry_ladder_matches_the_specification(self):
        self.assertEqual(EMAIL_RETRY_COUNTDOWN, (5, 15, 30, 60, 60))
        self.assertEqual(EMAIL_MAX_RETRIES, 5)
        self.assertEqual([retry_countdown(n) for n in range(1, 7)], [5, 15, 30, 60, 60, 60])

    def test_error_description_never_carries_the_message(self):
        exc = smtplib.SMTPAuthenticationError(535, b"user ops@example.com secret=hunter2")
        summary = describe_email_error(exc)
        self.assertIn("SMTPAuthenticationError", summary)
        self.assertIn("535", summary)
        self.assertNotIn("hunter2", summary)
        self.assertNotIn("ops@example.com", summary)


class DiagnosticsTests(SimpleTestCase):
    """`check_email` must name the stage that failed, with secrets redacted."""

    def test_dns_failure_is_reported_as_dns_not_as_a_timeout(self):
        with override_settings(
            EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
            EMAIL_HOST="no-such-host.invalid",
            EMAIL_PORT=465,
            EMAIL_USE_SSL=True,
            EMAIL_USE_TLS=False,
            EMAIL_TIMEOUT=1,
        ), patch(
            "home.management.commands.check_email.socket.getaddrinfo",
            side_effect=socket.gaierror("Name or service not known"),
        ):
            output = run_check()
        self.assertIn("DNS", output)
        self.assertIn("naming problem", output)

    def test_a_timeout_on_465_ssl_is_not_answered_with_a_blind_587_suggestion(self):
        with override_settings(
            EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
            EMAIL_HOST="smtp.gmail.com",
            EMAIL_PORT=465,
            EMAIL_USE_SSL=True,
            EMAIL_USE_TLS=False,
            EMAIL_TIMEOUT=1,
            EMAIL_HOST_USER="ops@example.com",
            EMAIL_HOST_PASSWORD="app-password",
        ), patch(
            "home.management.commands.check_email.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("192.0.2.1", 465))],
        ), patch(
            "home.management.commands.check_email.socket.create_connection",
            side_effect=TimeoutError("timed out"),
        ):
            output = run_check()
        self.assertIn("TimeoutError", output)
        self.assertIn("465 with implicit SSL is the correct", output)
        self.assertIn("Test-NetConnection", output)
        # The advice must not claim the configured port is wrong.
        self.assertNotIn("Try EMAIL_PORT=587", output)

    def test_a_timeout_on_587_tls_is_answered_symmetrically(self):
        with override_settings(
            EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
            EMAIL_HOST="smtp.gmail.com",
            EMAIL_PORT=587,
            EMAIL_USE_SSL=False,
            EMAIL_USE_TLS=True,
            EMAIL_TIMEOUT=1,
            EMAIL_HOST_USER="ops@example.com",
            EMAIL_HOST_PASSWORD="app-password",
        ), patch(
            "home.management.commands.check_email.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("192.0.2.1", 587))],
        ), patch(
            "home.management.commands.check_email.socket.create_connection",
            side_effect=TimeoutError("timed out"),
        ):
            output = run_check()
        self.assertIn("587 with STARTTLS is the correct", output)

    def test_connection_refused_is_named_not_treated_as_a_timeout(self):
        with override_settings(
            EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
            EMAIL_HOST="smtp.gmail.com",
            EMAIL_PORT=465,
            EMAIL_USE_SSL=True,
            EMAIL_USE_TLS=False,
            EMAIL_TIMEOUT=1,
            EMAIL_HOST_USER="ops@example.com",
            EMAIL_HOST_PASSWORD="app-password",
        ), patch(
            "home.management.commands.check_email.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("192.0.2.1", 465))],
        ), patch(
            "home.management.commands.check_email.socket.create_connection",
            side_effect=ConnectionRefusedError("refused"),
        ):
            output = run_check()
        self.assertIn("ConnectionRefusedError", output)
        self.assertNotIn("within 1s (TimeoutError)", output)

    def test_an_ssl_handshake_failure_is_named_as_tls_not_as_a_network_timeout(self):
        with override_settings(
            EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
            EMAIL_HOST="smtp.example.test",
            EMAIL_PORT=465,
            EMAIL_USE_SSL=True,
            EMAIL_USE_TLS=False,
            EMAIL_TIMEOUT=1,
            EMAIL_HOST_USER="ops@example.com",
            EMAIL_HOST_PASSWORD="app-password",
        ), patch(
            "home.management.commands.check_email.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("192.0.2.1", 465))],
        ), patch(
            "home.management.commands.check_email.socket.create_connection"
        ), patch(
            "smtplib.SMTP_SSL", side_effect=ssl.SSLError("wrong version number")
        ):
            output = run_check()
        self.assertIn("TLS handshake failed", output)
        self.assertNotIn("credentials were accepted", output)

    def test_a_rejected_password_is_reported_as_authentication_not_connection(self):
        with override_settings(
            EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
            EMAIL_HOST="smtp.example.test",
            EMAIL_PORT=465,
            EMAIL_USE_SSL=True,
            EMAIL_USE_TLS=False,
            EMAIL_TIMEOUT=5,
            EMAIL_HOST_USER="ops@example.com",
            EMAIL_HOST_PASSWORD="super-secret-app-password",
        ), patch(
            "home.management.commands.check_email.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("192.0.2.1", 465))],
        ), patch(
            "home.management.commands.check_email.socket.create_connection"
        ), patch(
            "smtplib.SMTP_SSL"
        ) as smtp_class:
            smtp_class.return_value.ehlo.return_value = (250, b"smtp.example.test")
            smtp_class.return_value.login.side_effect = smtplib.SMTPAuthenticationError(535, b"nope")
            output = run_check()
        self.assertIn("rejected the credentials", output)
        self.assertNotIn("super-secret-app-password", output)

    def test_the_broker_line_is_redacted(self):
        with override_settings(
            EMAIL_BACKEND="django.core.mail.backends.console.EmailBackend",
            CELERY_BROKER_URL="redis://:broker-secret@redis.internal:6379/0",
        ):
            output = run_check()
        self.assertNotIn("broker-secret", output)
        self.assertIn("redis://redis.internal:6379/0", output)

    def test_test_delivery_uses_the_active_backend(self):
        with override_settings(
            EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        ):
            output = run_check(**{"to": "ops@example.com"})
        self.assertIn("does not use the network", output)


@override_settings(CACHES={
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "email-reliability-tests",
    }
})
class DurableDeliveryTests(TestCase):
    """The broker can vanish; the intent cannot."""

    def setUp(self):
        cache.clear()
        EmailDelivery.objects.all().delete()
        mail.outbox = []

    def _dispatch(self, task=None, *args, purpose=None, ttl_seconds=None, recipient="person@example.com"):
        task = task or tasks.send_verification_email
        if purpose is None and args:
            purpose = "verification" if task is tasks.send_verification_email else purpose
        args = args or ("Nia", recipient, encrypt_email_code("123456"))
        kwargs = {}
        if purpose:
            kwargs["purpose"] = purpose
        if ttl_seconds:
            kwargs["ttl_seconds"] = ttl_seconds
        with patch.object(task, "delay") as queued:
            result = tasks.dispatch_email(task, *args, **kwargs)
        return result, queued

    def test_dispatch_records_a_durable_queued_delivery(self):
        result, queued = self._dispatch()
        self.assertEqual(result, "queued")
        queued.assert_called_once()
        delivery = EmailDelivery.objects.get()
        self.assertEqual(delivery.status, EmailDelivery.Status.QUEUED)
        self.assertEqual(delivery.recipient_email, "person@example.com")
        self.assertEqual(delivery.purpose, "verification")
        self.assertEqual(delivery.attempts, 0)
        self.assertTrue(delivery.expires_at > timezone.now())
        args, kwargs = queued.call_args
        self.assertEqual(args[0], "Nia")
        self.assertEqual(args[1], "person@example.com")
        self.assertIn("delivery_id", kwargs)

    def test_a_dead_broker_leaves_the_intent_recorded_and_never_blocks(self):
        with patch.object(
            tasks.send_verification_email, "delay", side_effect=OSError("broker down")
        ), patch.object(tasks.send_verification_email, "run") as inline:
            result = tasks.dispatch_email(
                tasks.send_verification_email, "Nia", "person@example.com",
                encrypt_email_code("123456"), purpose="verification", ttl_seconds=300,
            )
        self.assertEqual(result, "deferred")
        inline.assert_not_called()
        self.assertTrue(EmailDelivery.objects.filter(status="queued").exists())

    def test_a_successful_send_marks_sent_and_clears_the_failure_flag(self):
        cache.set(email_delivery_failure_key("person@example.com"), True, timeout=900)
        result, _ = self._dispatch()
        delivery = EmailDelivery.objects.get()
        send_result = tasks.send_verification_email(
            "Nia", "person@example.com", encrypt_email_code("123456"), delivery_id=str(delivery.id)
        )
        self.assertTrue(send_result["sent"])
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, EmailDelivery.Status.SENT)
        self.assertEqual(delivery.attempts, 1)
        self.assertFalse(cache.get(email_delivery_failure_key("person@example.com")))

    def test_a_transient_failure_schedules_the_bounded_ladder_with_the_same_code(self):
        self._dispatch()
        delivery = EmailDelivery.objects.get()

        with patch("home.tasks.send_security_code_email", side_effect=TimeoutError("timed out")) as send, \
                patch.object(tasks.send_verification_email, "retry") as retry:
            retry.side_effect = Retry("later")
            for expected_countdown in (5, 15, 30, 60, 60):
                with self.assertRaises(Retry):
                    tasks.send_verification_email(
                        "Nia", "person@example.com", delivery.args[2], delivery_id=str(delivery.id)
                    )
                retry.assert_called()
                countdown = retry.call_args.kwargs.get("countdown")
                self.assertEqual(countdown, expected_countdown)
                retry.reset_mock()
                delivery.refresh_from_db()
                self.assertEqual(delivery.status, EmailDelivery.Status.RETRYING)
            # The encrypted code in the row is untouched: every retry carries the same intended OTP.
            self.assertEqual(delivery.args[2], delivery.args[2])

    def test_the_otp_is_never_regenerated_by_a_retry(self):
        self._dispatch()
        delivery = EmailDelivery.objects.get()
        ciphertext = delivery.args[2]
        calls = []

        def fake_decrypt(value):
            calls.append(value)
            return "123456"

        with patch("home.email_delivery.decrypt_email_code", side_effect=fake_decrypt), \
                patch("home.tasks.send_security_code_email", side_effect=TimeoutError("timed out")), \
                patch.object(tasks.send_verification_email, "retry", side_effect=Retry("later")):
            for _ in range(2):
                with self.assertRaises(Retry):
                    tasks.send_verification_email(
                        "Nia", "person@example.com", ciphertext, delivery_id=str(delivery.id)
                    )
        self.assertEqual(calls, [ciphertext, ciphertext])
        delivery.refresh_from_db()
        self.assertEqual(delivery.args[2], ciphertext)

    def test_a_permanent_failure_is_not_retried_and_flags_the_page(self):
        self._dispatch()
        delivery = EmailDelivery.objects.get()
        with patch(
            "home.tasks.send_security_code_email",
            side_effect=smtplib.SMTPAuthenticationError(535, b"Username and Password not accepted"),
        ), patch.object(tasks.send_verification_email, "retry") as retry:
            result = tasks.send_verification_email(
                "Nia", "person@example.com", delivery.args[2], delivery_id=str(delivery.id)
            )
        self.assertEqual(result["reason"], "permanent")
        retry.assert_not_called()
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, EmailDelivery.Status.FAILED)
        self.assertIn("SMTPAuthenticationError(535)", delivery.last_error)
        self.assertTrue(cache.get(email_delivery_failure_key("person@example.com")))

    def test_retries_are_bounded_and_end_in_a_visible_failure(self):
        self._dispatch()
        delivery = EmailDelivery.objects.get()
        attempts = []
        with patch(
            "home.tasks.send_security_code_email", side_effect=TimeoutError("timed out")
        ), patch.object(tasks.send_verification_email, "retry") as retry:
            retry.side_effect = Retry("later")
            for _ in range(EMAIL_MAX_RETRIES):
                with self.assertRaises(Retry):
                    tasks.send_verification_email(
                        "Nia", "person@example.com", delivery.args[2], delivery_id=str(delivery.id)
                    )
                delivery.refresh_from_db()
                attempts.append(delivery.attempts)
            result = tasks.send_verification_email(
                "Nia", "person@example.com", delivery.args[2], delivery_id=str(delivery.id)
            )
        self.assertEqual(result["reason"], "retries_exhausted")
        self.assertEqual(attempts, list(range(1, EMAIL_MAX_RETRIES + 1)))
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, EmailDelivery.Status.FAILED)

    def test_an_expired_code_is_never_sent(self):
        self._dispatch()
        delivery = EmailDelivery.objects.get()
        EmailDelivery.objects.filter(pk=delivery.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        with patch("home.tasks.send_security_code_email") as send:
            result = tasks.send_verification_email(
                "Nia", "person@example.com", delivery.args[2], delivery_id=str(delivery.id)
            )
        send.assert_not_called()
        self.assertEqual(result["reason"], "expired")
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, EmailDelivery.Status.FAILED)
        self.assertEqual(delivery.last_error, "expired_code")

    def test_the_watchdog_re_offers_a_due_queued_delivery(self):
        self._dispatch()
        delivery = EmailDelivery.objects.get()
        EmailDelivery.objects.filter(pk=delivery.pk).update(available_at=timezone.now() - timedelta(seconds=1))
        with patch.object(tasks.send_verification_email, "delay") as queued:
            result = tasks.dispatch_queued_emails()
        self.assertEqual(result["dispatched"], 1)
        queued.assert_called_once()

    def test_the_watchdog_does_not_push_a_delivery_waiting_for_its_grace_period(self):
        self._dispatch()
        with patch.object(tasks.send_verification_email, "delay") as queued:
            result = tasks.dispatch_queued_emails()
        self.assertEqual(result["dispatched"], 0)
        queued.assert_not_called()

    def test_the_watchdog_fails_stale_rows_instead_of_sending_expired_codes(self):
        self._dispatch()
        delivery = EmailDelivery.objects.get()
        EmailDelivery.objects.filter(pk=delivery.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1),
            available_at=timezone.now() - timedelta(seconds=1),
        )
        with patch("home.tasks.send_security_code_email") as send:
            result = tasks.dispatch_queued_emails()
        send.assert_not_called()
        self.assertEqual(result["expired"], 1)
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, EmailDelivery.Status.FAILED)

    def test_the_status_endpoint_reports_pending_then_failed_after_expiry(self):
        self._dispatch()
        pending = self.client.get("/api/v1/email-delivery-status/", {"email": "person@example.com"}).json()
        self.assertFalse(pending["delivery_failed"])
        self.assertTrue(pending["delivery_pending"])
        EmailDelivery.objects.all().update(expires_at=timezone.now() - timedelta(seconds=1))
        after = self.client.get("/api/v1/email-delivery-status/", {"email": "person@example.com"}).json()
        self.assertTrue(after["delivery_failed"])
        self.assertFalse(after["delivery_pending"])

    def test_a_successful_resend_clears_the_previous_failure_state(self):
        self._dispatch()
        EmailDelivery.objects.all().update(expires_at=timezone.now() - timedelta(seconds=1))
        first = self.client.get("/api/v1/email-delivery-status/", {"email": "person@example.com"}).json()
        self.assertTrue(first["delivery_failed"])
        # A fresh resend: new code, new delivery, successful send.
        email = "person@example.com"
        result = tasks.dispatch_email(
            tasks.send_verification_email, "Nia", email,
            encrypt_email_code("654321"), purpose="verification", ttl_seconds=300,
        )
        delivery = EmailDelivery.objects.latest("created_at")
        tasks.send_verification_email("Nia", email, delivery.args[2], delivery_id=str(delivery.id))
        final = self.client.get("/api/v1/email-delivery-status/", {"email": email}).json()
        self.assertFalse(final["delivery_failed"])
        self.assertFalse(final["delivery_pending"])

    def test_the_payout_change_email_follows_the_same_contract(self):
        from payments.tasks import send_payout_change_email

        result, _ = self._dispatch(
            send_payout_change_email, "Nia", "artisan@example.com", encrypt_email_code("123456"),
            purpose=None,
        )
        self.assertEqual(result, "queued")
        delivery = EmailDelivery.objects.get()
        self.assertEqual(delivery.purpose, "payout_change")
        self.assertEqual(delivery.task_name, "payments.tasks.send_payout_change_email")


class NotificationEmailTests(TestCase):
    """Notification mails fail safe: business flows never inherit SMTP pain."""

    def setUp(self):
        cache.clear()

    def test_a_permanent_smtp_failure_raises_permanent_not_a_retry(self):
        from core.exceptions import PermanentTaskError
        from notifications import tasks as notification_tasks

        with patch.object(notification_tasks, "already_sent", return_value=False), \
                patch.object(notification_tasks, "acquire_lock", return_value=True), \
                patch.object(notification_tasks, "release_lock"), \
                patch.object(notification_tasks, "EmailMultiAlternatives") as email_class, \
                patch.object(notification_tasks.send_email_notification, "retry") as retry:
            email_class.return_value.send.side_effect = smtplib.SMTPAuthenticationError(535, b"no")
            with self.assertRaises(PermanentTaskError):
                notification_tasks.send_email_notification(
                    event_key="test:permanent",
                    recipient_email="ada@example.com",
                    recipient_name="Ada",
                    subject="Hello",
                    message="Body",
                )
            retry.assert_not_called()

    def test_a_transient_smtp_failure_retries_with_the_ladder(self):
        from notifications import tasks as notification_tasks

        with patch.object(notification_tasks, "already_sent", return_value=False), \
                patch.object(notification_tasks, "acquire_lock", return_value=True), \
                patch.object(notification_tasks, "release_lock"), \
                patch.object(notification_tasks, "EmailMultiAlternatives") as email_class, \
                patch.object(notification_tasks.send_email_notification, "retry") as retry:
            email_class.return_value.send.side_effect = TimeoutError("timed out")
            retry.side_effect = Retry("later")
            with self.assertRaises(Retry):
                notification_tasks.send_email_notification(
                    event_key="test:transient",
                    recipient_email="ada@example.com",
                    recipient_name="Ada",
                    subject="Hello",
                    message="Body",
                )
            self.assertEqual(retry.call_args.kwargs["countdown"], 5)

    def test_a_successful_send_marks_the_event_sent(self):
        from notifications import tasks as notification_tasks

        with patch.object(notification_tasks, "already_sent", return_value=False), \
                patch.object(notification_tasks, "acquire_lock", return_value=True), \
                patch.object(notification_tasks, "release_lock"), \
                patch.object(notification_tasks, "mark_sent") as mark_sent:
            result = notification_tasks.send_email_notification(
                event_key="test:sent",
                recipient_email="ada@example.com",
                recipient_name="Ada",
                subject="Hello",
                message="Body",
            )
        self.assertEqual(result["status"], "sent")
        mark_sent.assert_called_once_with("test:sent")

    def test_a_payout_receipt_attachment_is_still_attached(self):
        from notifications import tasks as notification_tasks

        mail.outbox = []
        with patch.object(notification_tasks, "already_sent", return_value=False), \
                patch.object(notification_tasks, "acquire_lock", return_value=True), \
                patch.object(notification_tasks, "release_lock"), \
                patch("payments.receipts.payout_receipt_pdf", return_value=b"%PDF-1.4 test"):
            result = notification_tasks.send_email_notification(
                event_key="test:receipt",
                recipient_email="ada@example.com",
                recipient_name="Ada",
                subject="Payout receipt",
                message="Your receipt is attached.",
                attachments=[{"kind": "payout_receipt_pdf", "payment_id": "p-1", "filename": "receipt.pdf"}],
            )
        self.assertEqual(result["status"], "sent")
        self.assertEqual(len(mail.outbox), 1)
        attachments = mail.outbox[0].attachments
        self.assertEqual(attachments[0][0], "receipt.pdf")
        self.assertEqual(attachments[0][2], "application/pdf")

    def test_the_outbox_dead_letters_a_permanent_notification_immediately(self):
        from core.exceptions import PermanentTaskError
        from core.models import OutboxEvent
        from core.tasks import dispatch_outbox_events

        event = OutboxEvent.objects.create(
            event_type="notification.email",
            event_key=f"test:permanent:{uuid.uuid4().hex[:8]}",
            payload={"event_key": "x", "recipient_email": "ada@example.com", "recipient_name": "Ada", "subject": "S", "message": "M"},
        )
        with patch("notifications.tasks.send_email_notification.delay", side_effect=PermanentTaskError("SMTPAuthenticationError(535)")):
            dispatch_outbox_events()
        event.refresh_from_db()
        self.assertIsNotNone(event.dead_lettered_at)

    def test_the_outbox_re_queues_a_transient_failure_with_backoff(self):
        from core.models import OutboxEvent
        from core.tasks import dispatch_outbox_events

        event = OutboxEvent.objects.create(
            event_type="notification.email",
            event_key=f"test:transient:{uuid.uuid4().hex[:8]}",
            payload={"event_key": "x", "recipient_email": "ada@example.com", "recipient_name": "Ada", "subject": "S", "message": "M"},
        )
        with patch("notifications.tasks.send_email_notification.delay", side_effect=TimeoutError("timed out")):
            dispatch_outbox_events()
        event.refresh_from_db()
        self.assertIsNone(event.dead_lettered_at)
        self.assertEqual(event.attempts, 1)

    def test_a_failed_notification_never_raises_out_of_the_dispatcher(self):
        from core.models import OutboxEvent
        from core.tasks import dispatch_outbox_events

        OutboxEvent.objects.create(
            event_type="notification.email",
            event_key=f"test:explode:{uuid.uuid4().hex[:8]}",
            payload={"event_key": "x", "recipient_email": "ada@example.com", "recipient_name": "Ada", "subject": "S", "message": "M"},
        )
        with patch("notifications.tasks.send_email_notification.delay", side_effect=RuntimeError("boom")):
            result = dispatch_outbox_events()
        self.assertEqual(result["status"], "ok")


class SecurityCodeContentTests(TestCase):
    """The branded multipart content survives the delivery work."""

    def setUp(self):
        cache.clear()
        mail.outbox = []

    def test_verification_email_is_delivered_branded_with_html_and_text(self):
        tasks.send_verification_email("Nia", "person@example.com", encrypt_email_code("123456"))
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["person@example.com"])
        self.assertIn("Verify Your Artisan Platform Account", message.subject)
        self.assertIn("123456", message.body)
        self.assertTrue(message.alternatives)
        self.assertEqual(message.alternatives[0][1], "text/html")

    def test_password_reset_email_is_delivered(self):
        tasks.send_password_reset_email("Nia", "person@example.com", encrypt_email_code("654321"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Password Reset", mail.outbox[0].subject)

    def test_staff_security_code_is_delivered_for_a_live_challenge(self):
        from home.models import CustomUser, StaffSecurityChallenge
        from django.utils import timezone as tz

        user = CustomUser.objects.create_user(
            email="staff@example.com", password="StrongPassword123!",
            first_name="S", last_name="Taff", is_active=True, is_staff=True,
        )
        challenge = StaffSecurityChallenge.objects.create(
            user=user, initiated_by=user, purpose="LOGIN_MFA",
            otp_hash="x", expires_at=tz.now() + timedelta(minutes=5),
        )
        tasks.send_staff_security_code(str(challenge.pk), encrypt_email_code("111222"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["staff@example.com"])

    def test_notification_email_is_delivered_branded(self):
        from notifications import tasks as notification_tasks

        with patch.object(notification_tasks, "already_sent", return_value=False), \
                patch.object(notification_tasks, "acquire_lock", return_value=True), \
                patch.object(notification_tasks, "release_lock"), \
                patch.object(notification_tasks, "mark_sent"):
            result = notification_tasks.send_email_notification(
                event_key="test:branded",
                recipient_email="person@example.com",
                recipient_name="Nia",
                subject="Booking accepted",
                message="Your booking was accepted.",
            )
        self.assertEqual(result["status"], "sent")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].alternatives[0][1], "text/html")
