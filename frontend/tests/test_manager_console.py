"""Tests for the Manager Console pages.

The console is presentation only, so these tests drive it the way a browser does
— a signed-in staff session, real URLs, the real templates — with the manager API
replaced by fixtures that mirror the API's response shape. What they pin down:

* every console page renders without a template exception;
* a refused or failed API call surfaces as a message, never as a blank figure;
* a missing manager identity sends the visitor back to their own dashboard;
* sensitive writes ask for step-up **before** anything is posted;
* an export download streams exactly the bytes the API returned.
"""
from unittest import mock

from django.test import Client, TestCase
from django.urls import reverse

from frontend.api import APIError
from frontend.tests import console_payloads as fixtures


class FakeManagerAPI:
    """Stand-in for ``frontend.api.APIClient`` that answers from fixtures."""

    def __init__(self, payloads=None, *, forbidden=(), empty=()):
        self.payloads = payloads if payloads is not None else fixtures.PAYLOADS
        self.forbidden = set(forbidden)
        self.empty = set(empty)
        self.gets = []
        self.posts = []

    def _respond(self, path, *, forbidden_check=True):
        if forbidden_check and path in self.forbidden:
            raise APIError(403, {"detail": "Your role cannot do that."})
        if path in self.empty:
            return 200, {}, {}
        if path not in self.payloads:
            raise AssertionError(f"The console requested an endpoint with no fixture: {path}")
        return 200, self.payloads[path], {}

    def get(self, path, params=None):
        self.gets.append((path, dict(params or {})))
        return self._respond(path)

    def post(self, path, data=None, files=None, idempotency_key=None, headers=None):
        self.posts.append({"path": path, "data": data, "headers": dict(headers or {})})
        return self._respond(path)

    def delete(self, path):
        self.posts.append({"path": path, "data": None, "headers": {}})
        return self._respond(path)

    def get_file(self, path):
        self.gets.append((path, {}))
        if path in self.forbidden:
            raise APIError(403, {"detail": "Not your export."})
        return b"Status,Bookings\r\nACCEPTED,316\r\n", "text/csv", {
            "Content-Disposition": 'attachment; filename="bookings.csv"'
        }


class _patched_clients:
    """Context manager patching every module that holds an ``APIClient`` binding."""

    # Every module that did ``from .api import APIClient`` holds its own binding;
    # patching only ``frontend.api`` would leave the navbar badges (and therefore
    # the real network) untouched.
    TARGETS = (
        "frontend.console_views.APIClient",
        "frontend.api.APIClient",
        "frontend.views.APIClient",
        "frontend.context_processors.APIClient",
    )

    def __init__(self, api):
        self.api = api
        self.patchers = []

    def __enter__(self):
        for target in self.TARGETS:
            patcher = mock.patch(target, return_value=self.api)
            patcher.start()
            self.patchers.append(patcher)
        return self.api

    def __exit__(self, *exc_info):
        for patcher in reversed(self.patchers):
            patcher.stop()
        return False


class ConsoleTestCase(TestCase):
    def setUp(self):
        self.client = Client()

    def sign_in(self, *, manager=True):
        session = self.client.session
        session["api_access"] = "test-access-token"
        session["api_refresh"] = "test-refresh-token"
        session["api_user"] = {"id": "1", "email": "console.manager@example.com",
                               "first_name": "Console", "last_name": "Manager",
                               "is_staff": True, "is_superuser": True, "role": "staff"}
        session["api_role"] = "staff"
        if not manager:
            for key in ("api_access", "api_refresh", "api_user", "api_role"):
                session.pop(key, None)
        session.save()

    def fake_api(self, api):
        """Patch the client everywhere a request could reach for it.

        The console views import their own reference and the shared page shell
        (navbar badges) goes through ``frontend.api`` — both must be answered by
        the fixture client so a page render never opens a real socket.
        """
        return _patched_clients(api)


class ConsolePageRenderingTests(ConsoleTestCase):
    def test_every_console_page_renders_api_payloads(self):
        self.sign_in()
        api = FakeManagerAPI()
        with self.fake_api(api):
            for name, path, marker in fixtures.PAGE_MARKERS:
                with self.subTest(page=name):
                    response = self.client.get(path)
                    self.assertEqual(response.status_code, 200, f"{name} did not render")
                    self.assertContains(response, marker)
                    self.assertEqual(response["Cache-Control"],
                                     "no-store, no-cache, must-revalidate, private")

    def test_no_raw_iso_timestamp_leaks_into_a_console_page(self):
        """Timestamps are for people, not a debugging view.

        The API sends ISO-8601; a template that prints one verbatim looks like a
        bug to whoever reads the page, so every timestamp must be formatted.
        """
        import re

        self.sign_in()
        api = FakeManagerAPI()
        iso = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")
        with self.fake_api(api):
            for name, path, _marker in fixtures.PAGE_MARKERS:
                with self.subTest(page=name):
                    body = self.client.get(path).content.decode()
                    self.assertIsNone(iso.search(body), f"{name} printed a raw ISO timestamp")

    def test_a_page_render_makes_no_outbound_network_call(self):
        """The console renders from the API response only — never from the network
        inside the test, which would mean a badge or a helper bypasses the client."""
        self.sign_in()
        api = FakeManagerAPI()
        with self.fake_api(api), mock.patch(
            "frontend.api._HTTP_SESSION.request", side_effect=AssertionError("outbound HTTP call")
        ):
            self.assertEqual(self.client.get("/console/").status_code, 200)

    def test_a_refused_verification_says_what_is_wrong(self):
        """``code`` is a field here, not transport metadata: the sentence must show."""
        from frontend.api_errors import normalize_api_error

        message = normalize_api_error({
            "code": "INVALID",
            "message": "Validation failed.",
            "detail": "Validation failed.",
            "details": {"code": "That authenticator code is not valid."},
        }, 400)
        self.assertIn("That authenticator code is not valid.", message)

        # A real metadata token is still hidden from the manager.
        token_only = normalize_api_error({"code": "PERMISSION_DENIED", "details": {"code": "PERMISSION_DENIED"}}, 403)
        self.assertNotIn("PERMISSION_DENIED", token_only)

    def test_no_console_page_renders_an_empty_css_value(self):
        """``width: %`` is not CSS: the browser drops it and the bar looks full.

        ``firstof`` prints nothing when every argument is falsy, so a zero-valued
        row used to produce exactly that. Every rendered style value must be a
        number.
        """
        import re

        self.sign_in()
        api = FakeManagerAPI()
        broken = re.compile(r"(?:width|height|flex-basis):\s*(?:%|;|\")")
        with self.fake_api(api):
            for name, path, _marker in fixtures.PAGE_MARKERS:
                with self.subTest(page=name):
                    body = self.client.get(path).content.decode()
                    self.assertIsNone(broken.search(body), f"{name} rendered an empty CSS value")

    def test_a_change_that_rounds_to_zero_is_not_shown_as_no_change(self):
        """₦500 on ₦7m is a real movement; "0.0%" says nothing happened."""
        from frontend.templatetags.site_tags import delta_label

        self.assertEqual(delta_label(0), "0%")
        self.assertEqual(delta_label(0.0), "0%")
        self.assertEqual(delta_label(0.04), "<0.1%")
        self.assertEqual(delta_label(-0.04), "<0.1%")
        self.assertEqual(delta_label(12.34), "12.3%")
        self.assertEqual(delta_label(None), "")

    def test_a_list_named_by_the_api_is_actually_rendered(self):
        """The staff page read "results" while the API sends "staff".

        The page silently claimed there were no manager accounts, which is the
        kind of bug a screenshot hides and an assertion catches.
        """
        self.sign_in()
        api = FakeManagerAPI()
        with self.fake_api(api):
            body = self.client.get("/console/staff/").content.decode()
        for row in fixtures.PAYLOADS["/manager/staff/"]["staff"]:
            self.assertIn(row["email"], body, "every manager row must appear")
        self.assertNotIn("No manager accounts yet", body)
        for role in fixtures.PAYLOADS["/manager/staff/"]["roles"]:
            self.assertIn(role["name"], body, "every role must appear")

    def test_no_page_says_its_lists_are_empty_when_the_api_sent_rows(self):
        """A generic guard: if a fixture list exists, the page must show it."""
        self.sign_in()
        api = FakeManagerAPI()
        empty_claims = (
            "No manager accounts yet", "No roles returned", "No campaigns yet",
            "No saved reports yet", "No jobs yet",
        )
        with self.fake_api(api):
            for name, path, _marker in fixtures.PAGE_MARKERS:
                body = self.client.get(path).content.decode()
                for claim in empty_claims:
                    if claim in body:
                        with self.subTest(page=name, claim=claim):
                            self.fail(f"{name} claimed '{claim}' while the fixtures had rows")

    def test_console_pages_never_ask_the_api_for_another_manager(self):
        """Every read goes through the manager namespace, never a marketplace route."""
        self.sign_in()
        api = FakeManagerAPI()
        with self.fake_api(api):
            self.client.get("/console/")
            self.client.get("/console/financial/")
        for path, _params in api.gets:
            self.assertTrue(path.startswith("/manager/"), path)

    def test_the_dashboard_never_renders_a_figure_the_api_did_not_send(self):
        self.sign_in()
        api = FakeManagerAPI(empty={"/manager/analytics/overview/"})
        with self.fake_api(api):
            response = self.client.get("/console/")
        self.assertEqual(response.status_code, 200)
        # No KPIs, no invented zeros in a metric card, and the page says so.
        self.assertNotContains(response, "Paid booking value")


class ConsoleErrorHandlingTests(ConsoleTestCase):
    def test_a_permission_error_shows_a_message_instead_of_a_number(self):
        self.sign_in()
        api = FakeManagerAPI(forbidden={"/manager/financial/"})
        with self.fake_api(api):
            response = self.client.get("/console/financial/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Your role cannot do that.")
        self.assertNotContains(response, "Captured payments")

    def test_a_missing_manager_identity_returns_the_visitor_to_their_dashboard(self):
        self.sign_in()
        api = FakeManagerAPI(forbidden={"/manager/session/"})
        with self.fake_api(api):
            response = self.client.get("/console/", follow=False)
        self.assertRedirects(response, reverse("dashboard"), fetch_redirect_response=False)

    def test_an_anonymous_visitor_is_sent_to_sign_in(self):
        response = self.client.get("/console/")
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])


class ConsoleWriteTests(ConsoleTestCase):
    def test_sensitive_configuration_change_asks_for_step_up_before_posting(self):
        self.sign_in()
        api = FakeManagerAPI()
        with self.fake_api(api):
            response = self.client.post("/console/config/change/", {
                "key": "fees.customer_service_fee_kobo",
                "category": "sensitive",
                "value": "0",
                "reason": "Test",
            })
        self.assertEqual(api.posts, [], "a sensitive change must not be posted without step-up")
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("console-step-up"), response["Location"])
        self.assertIn("scope=config%3Afinance", response["Location"])

    def test_verifying_a_step_up_replays_the_waiting_change(self):
        """The manager should not retype the form after verifying their identity."""
        self.sign_in()
        api = FakeManagerAPI()
        with self.fake_api(api):
            self.client.post("/console/config/change/", {
                "key": "fees.customer_service_fee_kobo",
                "category": "sensitive",
                "value": "0",
                "reason": "Test",
            })
            self.assertEqual(api.posts, [], "nothing is posted before verification")

            response = self.client.post("/console/step-up/", {
                "scope": "config:finance",
                "password": "secret",
                "code": "123456",
                "next": "/console/config/",
            })
        self.assertEqual(response.status_code, 302)
        posted = [entry["path"] for entry in api.posts]
        self.assertIn("/manager/config/fees.customer_service_fee_kobo/change/", posted,
                      "the parked change must be submitted after verification")
        sent = api.posts[-1]["data"]
        self.assertEqual(sent["value"], "0", "the original values are replayed, not an empty form")
        self.assertEqual(api.posts[-1]["headers"].get("X-Step-Up-Token"), "grant-1",
                         "the replay uses the grant that was just issued")

    def test_a_second_step_up_does_not_replay_a_stale_action(self):
        """Once the action ran, it must not fire again on the next verification."""
        self.sign_in()
        api = FakeManagerAPI()
        with self.fake_api(api):
            self.client.post("/console/config/change/", {
                "key": "fees.customer_service_fee_kobo", "category": "sensitive",
                "value": "0", "reason": "Test",
            })
            self.client.post("/console/step-up/", {
                "scope": "config:finance", "password": "secret", "code": "123456",
                "next": "/console/config/",
            })
            count_after_first = len(api.posts)
            self.client.post("/console/step-up/", {
                "scope": "config:finance", "password": "secret", "code": "123456",
                "next": "/console/config/",
            })
        config_posts = [entry for entry in api.posts if entry["path"].endswith("/change/")]
        self.assertEqual(len(config_posts), 1, "the action is replayed exactly once")
        self.assertGreaterEqual(len(api.posts), count_after_first)

    def test_a_retention_form_without_a_data_class_never_reaches_the_api(self):
        self.sign_in()
        api = FakeManagerAPI()
        with self.fake_api(api):
            response = self.client.post("/console/privacy/retention/", {"days": "30"})
        self.assertEqual(api.posts, [], "an empty data class must not build /retention//")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("console-privacy"))

    def test_a_non_sensitive_change_is_posted_once(self):
        self.sign_in()
        api = FakeManagerAPI(empty={"/manager/config/analytics.max_dashboard_range_days/change/"})
        with self.fake_api(api):
            self.client.post("/console/config/change/", {
                "key": "analytics.max_dashboard_range_days",
                "category": "operational",
                "value": "90",
            })
        self.assertEqual(len(api.posts), 1)
        self.assertEqual(api.posts[0]["data"]["value"], "90")

    def test_campaign_creation_is_one_post_and_lands_in_pending_approval(self):
        self.sign_in()
        api = FakeManagerAPI(payloads={**fixtures.PAYLOADS,
                                       "/manager/campaigns/": fixtures.CAMPAIGNS})
        with self.fake_api(api):
            response = self.client.post("/console/campaigns/create/", {
                "title": "Weekend plumbing demand",
                "body": "Verified plumbers near you are available this weekend.",
                "segment": "all_customers",
                "channels": ["IN_APP", "EMAIL"],
            })
        self.assertRedirects(response, reverse("console-campaigns"), fetch_redirect_response=False)
        self.assertEqual(len(api.posts), 1)
        self.assertEqual(api.posts[0]["data"]["channels"], ["IN_APP", "EMAIL"])

    def test_an_export_download_streams_the_api_file(self):
        self.sign_in()
        api = FakeManagerAPI()
        job_id = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"
        with self.fake_api(api):
            response = self.client.get(f"/console/exports/{job_id}/download/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        self.assertIn("bookings.csv", response["Content-Disposition"])
        self.assertEqual(response.content, b"Status,Bookings\r\nACCEPTED,316\r\n")

    def test_a_refused_export_download_sends_the_manager_back_to_reports(self):
        self.sign_in()
        job_id = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"
        api = FakeManagerAPI(forbidden={f"/manager/exports/{job_id}/download/"})
        with self.fake_api(api):
            response = self.client.get(f"/console/exports/{job_id}/download/")
        self.assertRedirects(response, reverse("console-reports"), fetch_redirect_response=False)
