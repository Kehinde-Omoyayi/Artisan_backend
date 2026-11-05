"""The manager overview has to offer the way in.

The page is masked until the manager asks for it, which is deliberate. What these
tests guard is the other half of that promise: the mask must always carry a
visible, labelled control that loads the analytics, and pressing it must really
land on the loaded page. A blank white box where the button should be is the
failure this file exists to catch.
"""
from django.test import Client, TestCase
from django.urls import reverse

from frontend.tests.test_manager_console import _patched_clients


class OverviewApi:
    """Answers every endpoint the signed-in shell asks for; only the overview matters."""

    def __init__(self, *, manager=True):
        self.gets = []
        self.manager = manager

    def get(self, path, params=None):
        self.gets.append((path, dict(params or {})))
        if path == "/support/cases/capabilities/":
            return 200, {"is_superuser": self.manager, "is_support_staff": self.manager,
                         "can_review_refunds": self.manager, "can_manage_staff": self.manager,
                         "can_review_promotions": self.manager}, {}
        if path == "/manager/overview/":
            return 200, {
                "generated_at": "2026-10-31T09:15:00Z",
                # Every figure the dashboard prints, so the template never meets a
                # missing key (the API always sends the whole set).
                "kpis": {key: 7 for key in (
                    "service_requests_30d", "emergency_requests_30d", "service_requests_total",
                    "bookings_30d", "bookings_active", "bookings_total",
                    "paid_volume_30d_kobo", "paid_payments_30d", "platform_fees_30d_kobo",
                    "payouts_30d_kobo", "payouts_pending_count",
                    "refunds_30d_kobo", "refunds_pending_count", "refunds_unknown_count",
                    "customers_total", "customers_active", "customers_new_30d",
                    "artisans_active", "artisans_new_30d", "artisans_total",
                    "artisans_pending_verification", "categories_total", "services_active",
                    "support_cases_open", "support_cases_review", "active_escrow_kobo",
                    "messages_30d", "support_disputes_open", "risk_customers_flagged",
                    "user_security_alerts",
                )},
                "snapshot_schedule": {"frequency": "DAILY"},
            }, {}
        return 200, {}, {}

    def post(self, path, data=None, files=None, idempotency_key=None, headers=None):
        return 200, {}, {}


class ManagerOverviewRevealTests(TestCase):
    def setUp(self):
        self.client = Client()
        session = self.client.session
        session["api_access"] = "test-access-token"
        session["api_refresh"] = "test-refresh-token"
        session["api_user"] = {"id": "1", "email": "manager@example.com", "first_name": "Console",
                               "last_name": "Manager", "is_staff": True, "is_superuser": True,
                               "role": "staff"}
        session["api_role"] = "staff"
        session.save()

    def test_the_masked_overview_shows_a_labelled_control_to_load_the_analytics(self):
        api = OverviewApi()
        with _patched_clients(api):
            response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("manager-privacy-controls", body)
        self.assertIn("Reveal analytics", body, "the privacy bar has no visible reveal control")
        self.assertIn("Load analytics", body, "the protected card offers no way to load the analytics")

    def test_the_reveal_control_posts_the_manager_scope_and_returns_to_this_page(self):
        api = OverviewApi()
        with _patched_clients(api):
            body = self.client.get(reverse("home")).content.decode()

        self.assertIn('name="scope" value="manager"', body)
        self.assertIn(f'name="next" value="{reverse("home")}"', body)

    def test_pressing_load_analytics_opens_the_loaded_overview_once(self):
        api = OverviewApi()
        with _patched_clients(api):
            response = self.client.post(
                reverse("frontend-manager-values-reveal"),
                {"scope": "manager", "next": reverse("home")},
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertNotIn("manager-load-state", body, "the protected placeholder is still on screen")
        self.assertIn("manager-kpi-grid", body, "no analytics were rendered after the reveal")
        self.assertEqual(response["Cache-Control"], "no-store, no-cache, must-revalidate, private")

        # The grant is single use: the next plain load is protected again.
        with _patched_clients(OverviewApi()):
            again = self.client.get(reverse("home")).content.decode()
        self.assertIn("manager-load-state", again)

    def test_a_non_manager_never_gets_the_reveal_grant(self):
        session = self.client.session
        session["api_user"] = {"id": "2", "email": "customer@example.com", "first_name": "C",
                               "last_name": "U", "is_staff": False, "is_superuser": False,
                               "role": "customer"}
        session["api_role"] = "customer"
        session.save()

        with _patched_clients(OverviewApi(manager=False)):
            response = self.client.post(reverse("frontend-manager-values-reveal"),
                                        {"scope": "manager", "next": reverse("home")})

        # Either the step-up gate (302) or a flat refusal (403) is acceptable;
        # what matters is that no grant is issued.
        self.assertIn(response.status_code, (302, 403))
        self.assertNotIn("manager_values_reveal_once", self.client.session)
