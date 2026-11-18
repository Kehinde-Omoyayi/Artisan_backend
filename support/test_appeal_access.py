"""Phase 5: the protected support/appeal route for blocked customers and artisans.

A blocked account cannot sign in, yet it must still be able to appeal. These
tests prove the appeal route:

* only accepts an email address that belongs to a *verified* account;
* never reveals whether an address exists;
* never returns the one-time code;
* expires, is single use, and locks out after too many attempts;
* attaches the real database user to the case, never a client-supplied id;
* is refused to an unverified signed-in user and to an anonymous caller.
"""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.core.cache import cache
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework.throttling import SimpleRateThrottle

from home.models import CustomerProfile

from .models import SupportAccessGrant, SupportCase

User = get_user_model()


@override_settings(
    SUPPORT_ACCESS_CODE_MINUTES=15,
    SUPPORT_ACCESS_SESSION_HOURS=6,
    SUPPORT_ACCESS_MAX_ATTEMPTS=5,
    SUPPORT_ACCESS_COOLDOWN=0,
    SUPPORT_ACCESS_MAX_REQUESTS_PER_HOUR=50,
)
class ProtectedSupportAppealTests(TestCase):
    #: The appeal route is deliberately throttled. ``SimpleRateThrottle`` binds its
    #: rate table once at import, so ``override_settings`` alone would not move it:
    #: the table itself is patched, and the shared throttle history is cleared so one
    #: test can never spend the next test's budget.
    _TEST_THROTTLE_RATES = {
        "anon": "10000/hour",
        "user": "10000/hour",
        "support_access": "1000/hour",
        "support_create": "1000/hour",
    }

    def setUp(self):
        super().setUp()
        cache.clear()
        patcher = patch.dict(
            SimpleRateThrottle.THROTTLE_RATES, self._TEST_THROTTLE_RATES
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = APIClient()
        self.customer = User.objects.create_user(
            email="blocked.customer@example.com",
            password="Str0ngPass!23",
            first_name="Bola",
            last_name="Ade",
            is_active=True,
            email_verified=True,
        )
        CustomerProfile.objects.get_or_create(user=self.customer)
        self.unverified = User.objects.create_user(
            email="unverified@example.com",
            password="Str0ngPass!23",
            first_name="Ngozi",
            last_name="Eze",
            is_active=True,
            email_verified=False,
        )
        CustomerProfile.objects.get_or_create(user=self.unverified)

    # ------------------------------------------------------------------ helpers

    def _request_code(self, email):
        with patch("support.access._send_code", return_value=True):
            return self.client.post(
                reverse("support-access-request"), {"email": email}, format="json"
            )

    def _request_code_with_known_code(self, email, code_value):
        """Send a code whose value the test knows, without it ever leaving the server."""
        with patch("support.access.secrets.randbelow", return_value=code_value - 100000):
            with patch("support.access._send_code", return_value=True):
                return self.client.post(
                    reverse("support-access-request"), {"email": email}, format="json"
                )

    def _verify(self, email, code):
        return self.client.post(
            reverse("support-access-verify"), {"email": email, "code": code}, format="json"
        )

    def _open_case(self, token=None, **extra):
        headers = {}
        if token:
            headers["HTTP_X_SUPPORT_ACCESS_TOKEN"] = token
        return self.client.post(
            reverse("support-case-list-create"),
            {
                "case_type": "ACCOUNT",
                "subject": "My account is blocked",
                "description": "Please review the block placed on my account after the last payment.",
                "priority": "HIGH",
                **extra,
            },
            format="json",
            **headers,
        )

    # ------------------------------------------------------------------ step 1

    def test_an_unknown_email_gets_the_same_answer_as_a_known_one(self):
        known = self._request_code("blocked.customer@example.com")
        unknown = self._request_code("nobody.at.all@example.com")

        self.assertEqual(known.status_code, status.HTTP_200_OK)
        self.assertEqual(unknown.status_code, status.HTTP_200_OK)
        self.assertEqual(known.data["status"], unknown.data["status"])
        self.assertEqual(known.data["message"], unknown.data["message"])
        self.assertEqual(
            SupportAccessGrant.objects.filter(email="nobody.at.all@example.com").count(), 0
        )

    def test_an_unverified_email_cannot_start_an_appeal(self):
        response = self._request_code("unverified@example.com")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            SupportAccessGrant.objects.filter(email="unverified@example.com").count(), 0
        )

    def test_a_blocked_account_can_start_an_appeal_and_the_code_is_never_returned(self):
        self.customer.security_status = "LOCKED"
        self.customer.save(update_fields=["security_status"])

        response = self._request_code("blocked.customer@example.com")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        grant = SupportAccessGrant.objects.get(email="blocked.customer@example.com")
        self.assertEqual(grant.user_id, self.customer.id)
        body = response.content.decode()
        self.assertNotIn(grant.code_hash, body)
        self.assertNotEqual(grant.code_hash, "")
        # Only the generic wording may mention a code; never its value.
        self.assertNotIn("424242", body)
        self.assertNotIn(grant.code_hash.lower(), body.lower())

    # ------------------------------------------------------------------ step 2

    def test_the_right_code_issues_a_token_and_the_wrong_one_is_refused(self):
        self._request_code_with_known_code("blocked.customer@example.com", 424242)
        bad = self._verify("blocked.customer@example.com", "000000")
        self.assertEqual(bad.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            SupportAccessGrant.objects.get(email="blocked.customer@example.com").attempts, 1
        )

        good = self._verify("blocked.customer@example.com", "424242")
        self.assertEqual(good.status_code, status.HTTP_200_OK)
        self.assertTrue(good.data["access_token"])
        self.assertEqual(good.data["account_type"], "customer")
        grant = SupportAccessGrant.objects.get(email="blocked.customer@example.com")
        self.assertIsNotNone(grant.consumed_at)
        self.assertNotEqual(grant.session_token_hash, good.data["access_token"])

    def test_the_code_is_single_use(self):
        self._request_code_with_known_code("blocked.customer@example.com", 111111)
        first = self._verify("blocked.customer@example.com", "111111")
        self.assertEqual(first.status_code, status.HTTP_200_OK)

        second = self._verify("blocked.customer@example.com", "111111")
        self.assertEqual(second.status_code, status.HTTP_400_BAD_REQUEST)

    def test_an_expired_code_is_refused(self):
        self._request_code_with_known_code("blocked.customer@example.com", 222222)
        grant = SupportAccessGrant.objects.get(email="blocked.customer@example.com")
        grant.code_expires_at = timezone.now() - timedelta(minutes=1)
        grant.save(update_fields=["code_expires_at"])

        response = self._verify("blocked.customer@example.com", "222222")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        grant.refresh_from_db()
        self.assertIsNotNone(grant.consumed_at)

    def test_too_many_wrong_attempts_burn_the_code(self):
        self._request_code_with_known_code("blocked.customer@example.com", 333333)
        for _ in range(5):
            self._verify("blocked.customer@example.com", "999999")

        response = self._verify("blocked.customer@example.com", "333333")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Too many incorrect attempts", str(response.data))

    # ------------------------------------------------------------------ step 3

    def test_a_verified_appeal_is_attached_to_the_real_account(self):
        self._request_code_with_known_code("blocked.customer@example.com", 515151)
        token = self._verify("blocked.customer@example.com", "515151").data["access_token"]

        response = self._open_case(token)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        case = SupportCase.objects.get(id=response.data["id"])
        self.assertEqual(case.opened_by_id, self.customer.id)
        self.assertTrue(
            case.events.filter(action="CASE_OPENED_WITH_VERIFIED_EMAIL").exists()
        )

    def test_a_client_supplied_identity_is_ignored(self):
        self._request_code_with_known_code("blocked.customer@example.com", 616161)
        token = self._verify("blocked.customer@example.com", "616161").data["access_token"]

        response = self._open_case(token, opened_by=self.unverified.id, user=999999)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        case = SupportCase.objects.get(id=response.data["id"])
        self.assertEqual(case.opened_by_id, self.customer.id)

    def test_anonymous_callers_without_a_token_are_refused(self):
        response = self._open_case()
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(SupportCase.objects.count(), 0)

    def test_a_forged_token_is_refused(self):
        response = self._open_case("not-a-real-token")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(SupportCase.objects.count(), 0)

    def test_an_expired_token_is_refused(self):
        self._request_code_with_known_code("blocked.customer@example.com", 717171)
        token = self._verify("blocked.customer@example.com", "717171").data["access_token"]
        grant = SupportAccessGrant.objects.get(email="blocked.customer@example.com")
        grant.session_expires_at = timezone.now() - timedelta(seconds=1)
        grant.save(update_fields=["session_expires_at"])

        self.assertEqual(self._open_case(token).status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(SupportCase.objects.count(), 0)

    def test_a_revoked_token_is_refused(self):
        self._request_code_with_known_code("blocked.customer@example.com", 818181)
        token = self._verify("blocked.customer@example.com", "818181").data["access_token"]
        grant = SupportAccessGrant.objects.get(email="blocked.customer@example.com")
        grant.session_revoked_at = timezone.now()
        grant.save(update_fields=["session_revoked_at"])

        self.assertEqual(self._open_case(token).status_code, status.HTTP_403_FORBIDDEN)

    def test_a_signed_in_but_unverified_user_is_refused(self):
        self.client.force_authenticate(user=self.unverified)
        response = self._open_case()
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("Verify your email", str(response.data))

    def test_a_signed_in_verified_user_opens_the_case_under_their_own_account(self):
        self.client.force_authenticate(user=self.customer)
        response = self._open_case()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            SupportCase.objects.get(id=response.data["id"]).opened_by_id, self.customer.id
        )

    def test_the_audit_trail_is_staff_only(self):
        response = self.client.get(reverse("support-access-grants"))
        self.assertIn(
            response.status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )

    def test_only_a_supervisor_can_read_the_access_audit_trail(self):
        from .permissions import SUPPORT_SUPERVISOR

        from django.contrib.auth.models import Group

        self._request_code_with_known_code("blocked.customer@example.com", 919191)
        self._verify("blocked.customer@example.com", "919191")

        supervisor = User.objects.create_user(
            email="supervisor@example.com",
            first_name="Ada",
            last_name="Supervisor",
            password="Str0ngPass!23",
            is_active=True,
            email_verified=True,
            is_staff=True,
        )
        supervisor.groups.add(Group.objects.get_or_create(name=SUPPORT_SUPERVISOR)[0])
        self.client.force_authenticate(user=supervisor)

        response = self.client.get(reverse("support-access-grants"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        rows = response.data["results"] if isinstance(response.data, dict) else response.data
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["user_email"], "blocked.customer@example.com")
        self.assertNotIn("code_hash", rows[0])
