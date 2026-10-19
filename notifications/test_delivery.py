"""Notification delivery: durable writes, deep links, idempotency and failure isolation."""
from unittest.mock import patch

from django.test import TestCase, override_settings

from core.models import OutboxEvent
from home.models import CustomUser
from notifications.events import schedule_email
from notifications.links import absolute_url
from notifications.models import InAppNotification


@override_settings(SITE_URL="https://market.example.com")
class ScheduleEmailTests(TestCase):
    def setUp(self):
        self.user = CustomUser.objects.create_user(email="ada@example.com", password="StrongPassword123!", first_name="Ada", last_name="Obi", is_active=True)

    def test_writes_in_app_notification_and_outbox_email_with_absolute_link(self):
        schedule_email(event_key="test:1", recipient=self.user, subject="Subject", message="Body", action_url="/bookings/abc/")
        notification = InAppNotification.objects.get(recipient=self.user, event_key="test:1")
        self.assertEqual(notification.data["action_url"], "/bookings/abc/")
        outbox = OutboxEvent.objects.get(event_key="test:1")
        self.assertEqual(outbox.payload["action_url"], "https://market.example.com/bookings/abc/")
        self.assertEqual(outbox.payload["recipient_name"], "Ada")
        self.assertEqual(outbox.payload["recipient_email"], "ada@example.com")

    def test_repeating_the_same_business_event_creates_one_notification_and_one_email(self):
        for _ in range(3):
            schedule_email(event_key="test:dup", recipient=self.user, subject="S", message="M")
        self.assertEqual(InAppNotification.objects.filter(event_key="test:dup").count(), 1)
        self.assertEqual(OutboxEvent.objects.filter(event_key="test:dup").count(), 1)

    def test_a_notification_failure_never_breaks_the_business_operation(self):
        with patch("notifications.events.create_in_app_notification", side_effect=RuntimeError("db down")):
            schedule_email(event_key="test:fail", recipient=self.user, subject="S", message="M")  # must not raise
        self.assertFalse(OutboxEvent.objects.filter(event_key="test:fail").exists())

    def test_notification_rolls_back_with_the_business_transaction(self):
        from django.db import transaction
        try:
            with transaction.atomic():
                schedule_email(event_key="test:rollback", recipient=self.user, subject="S", message="M")
                raise RuntimeError("business failure")
        except RuntimeError:
            pass
        self.assertFalse(InAppNotification.objects.filter(event_key="test:rollback").exists())
        self.assertFalse(OutboxEvent.objects.filter(event_key="test:rollback").exists())

    def test_users_without_email_still_get_an_in_app_notification(self):
        no_email = CustomUser.objects.create_user(email="noemail@example.com", password="StrongPassword123!", first_name="N", last_name="E", is_active=True)
        CustomUser.objects.filter(pk=no_email.pk).update(email="")
        no_email.refresh_from_db()
        schedule_email(event_key="test:noemail", recipient=no_email, subject="S", message="M")
        self.assertTrue(InAppNotification.objects.filter(event_key="test:noemail").exists())
        self.assertFalse(OutboxEvent.objects.filter(event_key="test:noemail").exists())


class AbsoluteUrlTests(TestCase):
    @override_settings(SITE_URL="")
    def test_no_site_url_means_no_absolute_link(self):
        self.assertIsNone(absolute_url("/bookings/1/"))

    @override_settings(SITE_URL="https://market.example.com/")
    def test_links_are_always_prefixed_with_the_configured_origin(self):
        self.assertEqual(absolute_url("/bookings/1/"), "https://market.example.com/bookings/1/")
        self.assertIsNone(absolute_url("https://evil.example/"))
        self.assertIsNone(absolute_url("javascript:alert(1)"))
        self.assertIsNone(absolute_url(None))
