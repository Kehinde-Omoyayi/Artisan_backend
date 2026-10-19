
from django.template.loader import render_to_string
from django.test import TestCase



class NotificationWorkflowTests(TestCase):
    def test_text_email_template_exists_and_renders(self):
        content = render_to_string("emails/notification.txt", {
            "recipient_name": "Customer",
            "subject": "Payment confirmed",
            "message": "Your payment is confirmed.",
            "action_url": "https://example.com",
            "action_label": "Open account",
        })
        self.assertIn("Payment confirmed", content)
        self.assertIn("Your payment is confirmed.", content)
        self.assertIn("https://example.com", content)


    def test_html_notification_uses_the_shared_cream_and_brown_brand(self):
        content = render_to_string("emails/notification.html", {
            "recipient_name": "Customer",
            "subject": "Payment confirmed",
            "message": "Your payment is confirmed.",
            "action_url": "https://example.com/account/",
            "action_label": "Open account",
        })
        self.assertIn("#faf7f1", content)
        self.assertIn("#4c3025", content)
        self.assertIn("#74432f", content)
        self.assertIn("Open account", content)
        self.assertIn("https://example.com/account/", content)

    def test_required_notification_templates_exist(self):
        from django.template.loader import get_template
        get_template("emails/notification.txt")
        get_template("emails/notification.html")


class NotificationDeletionTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient

        self.user = get_user_model().objects.create_user(
            email="notification-owner@example.com",
            first_name="Notification",
            last_name="Owner",
            password="StrongPassword123!",
            is_active=True,
        )
        self.other_user = get_user_model().objects.create_user(
            email="notification-other@example.com",
            first_name="Other",
            last_name="Account",
            password="StrongPassword123!",
            is_active=True,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def create_notification(self, recipient, event_key):
        from .models import InAppNotification

        return InAppNotification.objects.create(
            recipient=recipient,
            event_key=event_key,
            title="Marketplace update",
            message="An update about your service request.",
        )

    def test_single_delete_is_owned_and_foreign_ids_are_not_found(self):
        from django.urls import reverse
        from .models import InAppNotification

        owned = self.create_notification(self.user, "notification-delete-owned")
        foreign = self.create_notification(self.other_user, "notification-delete-foreign")

        response = self.client.delete(reverse("notification-delete", kwargs={"pk": owned.pk}))
        self.assertEqual(response.status_code, 204)
        self.assertFalse(InAppNotification.objects.filter(pk=owned.pk).exists())

        response = self.client.delete(reverse("notification-delete", kwargs={"pk": foreign.pk}))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(InAppNotification.objects.filter(pk=foreign.pk).exists())

    def test_bulk_delete_only_removes_authenticated_users_notifications(self):
        from django.urls import reverse
        from .models import InAppNotification

        owned_ids = {
            self.create_notification(self.user, "notification-delete-all-1").pk,
            self.create_notification(self.user, "notification-delete-all-2").pk,
        }
        foreign = self.create_notification(self.other_user, "notification-delete-all-foreign")

        response = self.client.delete(reverse("notification-delete-all"))

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["deleted"], 2)
        self.assertFalse(InAppNotification.objects.filter(pk__in=owned_ids).exists())
        self.assertTrue(InAppNotification.objects.filter(pk=foreign.pk).exists())
