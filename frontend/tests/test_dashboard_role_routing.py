"""Who owns the dashboard after sign-in.

Reported fault: an artisan signed in and was shown the **manager** dashboard —
the workspace the previous, staff user had been looking at. The workspace now
follows the signed-in person's own role:

* the manager console needs a staff identity *and* a superuser capability;
* an artisan always lands on the artisan dashboard;
* a customer always lands on the customer profile;
* a leftover "staff" tag, a stale capability answer or a stale token can never
  paint manager chrome (privacy bar, analytics cards, staff navigation) onto an
  artisan's or a customer's page.
"""
from django.test import Client, TestCase
from django.urls import reverse

from frontend.api import APIError
from frontend.tests.test_manager_console import _patched_clients


class RoleAwareApi:
    """Answers the sign-in shell; caps and profiles are set per test."""

    def __init__(self, *, is_superuser=False, is_support_staff=None, artisan_profile=True):
        self.is_superuser = is_superuser
        self.is_support_staff = (
            is_superuser if is_support_staff is None else is_support_staff
        )
        self.artisan_profile = artisan_profile
        self.gets = []

    def get(self, path, params=None):
        self.gets.append((path, dict(params or {})))
        if path == "/support/cases/capabilities/":
            return 200, {
                "is_superuser": self.is_superuser,
                "is_support_staff": self.is_support_staff,
            }, {}
        if path == "/profile/artisan/personal/profile/":
            if not self.artisan_profile:
                raise APIError(404, {"detail": "Not found."})
            return 200, {"first_name": "test", "business_name": "Test Works"}, {}
        if path == "/profile/customer/personal/profile/":
            return 200, {"first_name": "test"}, {}
        return 200, {}, {}

    def post(self, path, data=None, files=None, idempotency_key=None, headers=None):
        return 200, {}, {}


class DashboardRoleRoutingTests(TestCase):
    def setUp(self):
        self.client = Client()

    def sign_in(self, user, *, role):
        session = self.client.session
        session["api_access"] = "test-access-token"
        session["api_refresh"] = "test-refresh-token"
        session["api_user"] = user
        session["api_role"] = role
        session.save()

    def artisan_user(self, **overrides):
        user = {
            "id": "6", "first_name": "test", "last_name": "Artisan",
            "email": "test@example.com", "is_staff": False,
            "is_superuser": False, "role": "artisan",
        }
        user.update(overrides)
        return user

    def manager_user(self):
        return {
            "id": "1", "first_name": "Console", "last_name": "Manager",
            "email": "manager@example.com", "is_staff": True,
            "is_superuser": True, "role": "staff",
        }

    def assert_artisan_workspace(self, body):
        self.assertIn("workspace-metric-grid", body, "the artisan dashboard did not render")
        self.assertIn("Welcome back", body)
        self.assertNotIn("manager-load-state", body, "manager analytics reached an artisan")
        self.assertNotIn("PRIVACY BY DEFAULT", body, "the manager privacy bar reached an artisan")
        self.assertNotIn("manager-privacy-controls", body, "manager controls reached an artisan")
        self.assertIn('data-tour-manager="false"', body)

    def test_an_artisan_never_inherits_the_previous_managers_dashboard(self):
        """The exact report: signed in as an artisan, shown the manager console."""
        self.sign_in(self.artisan_user(), role="artisan")

        # The capability answer still describes the staff session that signed out.
        with _patched_clients(RoleAwareApi(is_superuser=True)):
            response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assert_artisan_workspace(response.content.decode())

    def test_a_leftover_staff_tag_does_not_outlive_the_staff_sign_in(self):
        self.sign_in(self.artisan_user(role=""), role="staff")

        with _patched_clients(RoleAwareApi(is_superuser=True)):
            response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assert_artisan_workspace(response.content.decode())

    def test_an_older_api_that_omits_the_role_still_lands_on_the_artisan_dashboard(self):
        self.sign_in(self.artisan_user(role=""), role="")

        with _patched_clients(RoleAwareApi(is_superuser=True)):
            response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assert_artisan_workspace(response.content.decode())

    def test_a_customer_never_gets_manager_chrome(self):
        self.sign_in(
            {"id": "9", "first_name": "Kehinde", "last_name": "Buyer",
             "email": "buyer@example.com", "is_staff": False,
             "is_superuser": False, "role": "customer"},
            role="customer",
        )

        with _patched_clients(RoleAwareApi(is_superuser=True, artisan_profile=False)):
            response = self.client.get(reverse("dashboard"))

        body = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("manager-load-state", body)
        self.assertNotIn("PRIVACY BY DEFAULT", body)
        self.assertIn('data-tour-manager="false"', body)

    def test_the_manager_still_reaches_the_manager_console(self):
        self.sign_in(self.manager_user(), role="staff")

        with _patched_clients(RoleAwareApi(is_superuser=True)):
            response = self.client.get(reverse("dashboard"))

        body = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn("manager-load-state", body, "the manager lost the protected overview")
        self.assertIn('data-tour-manager="true"', body)

    def test_an_unknown_role_falls_back_to_the_customer_workspace_not_staff(self):
        """No stored role and no profile answer must never open a staff page."""
        self.sign_in({"id": "10", "first_name": "Nameless"}, role="")

        api = RoleAwareApi(is_superuser=False, artisan_profile=False)
        api_404 = api

        def failing_get(path, params=None):
            api_404.gets.append((path, dict(params or {})))
            if path.startswith("/profile/"):
                raise APIError(404, {"detail": "Not found."})
            return 200, {}, {}

        api_404.get = failing_get
        with _patched_clients(api_404):
            response = self.client.get(reverse("dashboard"))

        body = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("manager-load-state", body)
        self.assertNotIn("PRIVACY BY DEFAULT", body)
