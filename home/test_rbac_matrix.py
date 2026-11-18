"""RBAC matrix: Super Staff vs ordinary staff, across the stack.

* Super Staff (authoritative Django superuser) — unrestricted at the staff
  boundary, provisioned as Super Administrator, passes the manager-overview
  permission check.
* Finance Manager — financial surface only; no Manager Overview, no staff
  administration, no support workspace; badge counters are self-scoped.
* Support Agent — support surface only; nothing financial, no overview.
* Multi-role — explicitly combined permissions accumulate; nothing else does.

Uses real marketplace JWTs for the boundary, and the manager permission layer
directly for the catalogue. The Manager Console template itself is enforced by
the same permission codes (frontend ``SECTION_PERMISSIONS`` mirrors this file).
"""
import importlib.util
from pathlib import Path
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from manager.models import ManagerProfile, ManagerRole
from manager.permissions import PERMISSIONS, SUPER_ADMIN
from manager.services import auth as auth_service
from notifications.models import InAppNotification
from support.permissions import SUPPORT_AGENT, SUPPORT_FINANCE

from .test_staff_security import access_token, create_security_profile

User = get_user_model()

TEST_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "rbac-matrix-tests",
    }
}
TEST_HASHERS = ["django.contrib.auth.hashers.PBKDF2PasswordHasher"]


def make_staff(email, *, superuser=False):
    account = User.objects.create_user(
        email=email, first_name="Test", last_name="User",
        password="Existing!Password84", is_active=True,
        is_staff=True, is_superuser=superuser,
    )
    create_security_profile(account)
    return account


def assign_role(user, slug, permissions=None):
    if slug == SUPER_ADMIN:
        role, _ = ManagerRole.objects.get_or_create(
            slug=slug, defaults={"name": "Super Administrator", "is_system": True,
                                 "permissions": sorted(PERMISSIONS)},
        )
    else:
        role, _ = ManagerRole.objects.get_or_create(
            slug=slug, defaults={"name": slug.replace("-", " ").title(), "is_system": True,
                                 "permissions": list(permissions or [])},
        )
        if permissions is not None and sorted(role.permissions or []) != sorted(permissions):
            role.permissions = list(permissions)
            role.save(update_fields=["permissions", "updated_at"])
    ManagerProfile.objects.update_or_create(user=user, defaults={"role": role})
    return role


@override_settings(CACHES=TEST_CACHES, PASSWORD_HASHERS=TEST_HASHERS)
class SuperStaffTests(TestCase):
    def setUp(self):
        self.superuser = make_staff("owner@example.test", superuser=True)
        auth_service.ensure_profile(self.superuser)
        self.other = make_staff("other-staff@example.test")

    def _client(self, user):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token(user)}")
        return client

    def test_super_staff_reaches_own_badge_endpoints(self):
        client = self._client(self.superuser)
        self.assertEqual(client.get("/api/v1/notifications/unread-count/").status_code, 200)
        self.assertEqual(client.get("/api/v1/conversations/unread-count/").status_code, 200)

    def test_super_staff_is_never_staff_access_restricted(self):
        client = self._client(self.superuser)
        response = client.get("/api/v1/services/get_artisan_services/")
        # The endpoint still enforces *its own* business rule (an artisan
        # profile is required) — what must never appear is the staff boundary.
        self.assertNotEqual(
            (response.data or {}).get("code") if isinstance(response.data, dict) else None,
            "STAFF_ACCESS_RESTRICTED",
        )

    def test_super_staff_reaches_manager_overview(self):
        client = self._client(self.superuser)
        with patch("home.manager_views.build_manager_overview",
                   return_value={"generated_at": timezone.now(), "kpis": {}}):
            response = client.get("/api/v1/manager/overview/")
        self.assertEqual(response.status_code, 200)

    def test_super_staff_is_provisioned_super_administrator_with_all_permissions(self):
        profile = auth_service.ensure_profile(self.superuser)
        self.assertEqual(profile.role.slug, SUPER_ADMIN)
        self.assertEqual(auth_service.effective_permissions(self.superuser), set(PERMISSIONS))
        self.assertIn("analytics.overview", PERMISSIONS)

    def test_super_staff_sees_only_their_own_unread_notifications(self):
        InAppNotification.objects.create(
            recipient=self.superuser, event_key="test:a", title="Mine", message="m")
        InAppNotification.objects.create(
            recipient=self.other, event_key="test:b", title="Theirs", message="m")
        client = self._client(self.superuser)
        payload = client.get("/api/v1/notifications/unread-count/").json()
        self.assertEqual(payload["unread_count"], 1)


@override_settings(CACHES=TEST_CACHES, PASSWORD_HASHERS=TEST_HASHERS)
class OrdinaryStaffRbacTests(TestCase):
    def setUp(self):
        roles = auth_service.ensure_system_roles()
        self.finance = make_staff("finance@example.test")
        assign_role(self.finance, "finance-manager")
        self.support = make_staff("support@example.test")
        Group.objects.get_or_create(name=SUPPORT_AGENT)
        Group.objects.get_or_create(name=SUPPORT_FINANCE)
        self.support.groups.add(Group.objects.get(name=SUPPORT_AGENT))
        assign_role(self.support, "support-agent")
        self.general = make_staff("general@example.test")
        assign_role(self.general, "general-manager")
        self.nobody = make_staff("plain@example.test")

    def _client(self, user):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token(user)}")
        return client

    def test_finance_staff_reaches_own_badge_endpoints(self):
        client = self._client(self.finance)
        self.assertEqual(client.get("/api/v1/notifications/unread-count/").status_code, 200)
        self.assertEqual(client.get("/api/v1/conversations/unread-count/").status_code, 200)

    def test_support_staff_reaches_own_badge_endpoints(self):
        client = self._client(self.support)
        self.assertEqual(client.get("/api/v1/notifications/unread-count/").status_code, 200)
        self.assertEqual(client.get("/api/v1/conversations/unread-count/").status_code, 200)

    def test_plain_staff_reaches_only_self_scoped_badge_endpoints(self):
        client = self._client(self.nobody)
        self.assertEqual(client.get("/api/v1/notifications/unread-count/").status_code, 200)
        self.assertEqual(client.get("/api/v1/conversations/unread-count/").status_code, 200)
        # ...and nothing else from the marketplace.
        blocked = client.get("/api/v1/services/get_artisan_services/")
        self.assertEqual(blocked.status_code, 403)
        # A staff account with no support role does not get support capabilities.
        capabilities = client.get("/api/v1/support/cases/capabilities/")
        self.assertEqual(capabilities.status_code, 403)
        self.assertEqual(capabilities.data["code"], "STAFF_ACCESS_RESTRICTED")

    def test_finance_manager_denied_manager_overview(self):
        client = self._client(self.finance)
        response = client.get("/api/v1/manager/overview/")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["code"], "STAFF_ACCESS_RESTRICTED")

    def test_support_agent_denied_manager_overview(self):
        client = self._client(self.support)
        response = client.get("/api/v1/manager/overview/")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["code"], "STAFF_ACCESS_RESTRICTED")

    def test_general_manager_reaches_manager_overview(self):
        client = self._client(self.general)
        with patch("home.manager_views.build_manager_overview",
                   return_value={"generated_at": timezone.now(), "kpis": {}}):
            response = client.get("/api/v1/manager/overview/")
        self.assertEqual(response.status_code, 200)

    def test_finance_permissions_are_finance_only(self):
        perms = auth_service.effective_permissions(self.finance)
        # Financial surface, granted.
        for code in ("analytics.financial", "finance.reconcile", "refund.view", "refund.approve"):
            self.assertIn(code, perms)
        # Overview, staff administration, marketing — never granted by finance.
        for code in ("analytics.overview", "staff.view", "staff.manage", "campaign.view",
                     "config.approve", "support.manage"):
            self.assertNotIn(code, perms)

    def test_support_permissions_are_support_only(self):
        perms = auth_service.effective_permissions(self.support)
        self.assertIn("support.view", perms)
        self.assertIn("support.manage", perms)
        for code in ("analytics.financial", "finance.reconcile", "refund.approve",
                     "payout.approve", "analytics.overview", "staff.view"):
            self.assertNotIn(code, perms)

    def test_multi_role_combines_only_explicit_grants(self):
        combined = "finance-plus-support"
        assign_role(self.nobody, combined, permissions=["analytics.financial", "support.view"])
        perms = auth_service.effective_permissions(self.nobody)
        self.assertEqual(perms, {"analytics.financial", "support.view"})
        self.assertTrue(auth_service.has_perm(self.nobody, "analytics.financial"))
        self.assertTrue(auth_service.has_perm(self.nobody, "support.view"))
        self.assertFalse(auth_service.has_perm(self.nobody, "staff.view"))
        self.assertFalse(auth_service.has_perm(self.nobody, "refund.approve"))
        self.assertFalse(auth_service.has_perm(self.nobody, "analytics.overview"))

    def test_support_groups_combine_for_one_account(self):
        # An account holding both support groups: finance refunds + agent queue.
        self.support.groups.add(Group.objects.get(name=SUPPORT_FINANCE))
        roles = sorted(self.support.groups.filter(name__startswith="CUSTOMER_SUPPORT").values_list("name", flat=True))
        self.assertEqual(roles, ["CUSTOMER_SUPPORT_AGENT", "CUSTOMER_SUPPORT_FINANCE"])

    def test_suspended_profile_loses_every_permission_immediately(self):
        profile = ManagerProfile.objects.get(user=self.finance)
        profile.is_suspended = True
        profile.save(update_fields=["is_suspended", "updated_at"])
        self.assertEqual(auth_service.effective_permissions(self.finance), set())
        client = self._client(self.finance)
        self.assertEqual(client.get("/api/v1/manager/financial/").status_code, 403)


@override_settings(CACHES=TEST_CACHES, PASSWORD_HASHERS=TEST_HASHERS)
class AnalyticsOverviewMigrationTests(TestCase):
    """The 0005 data migration grants the new code to exactly two roles, both ways."""

    def _load_migration(self):
        path = Path(__file__).resolve().parent.parent / "manager" / "migrations" / "0005_analytics_overview.py"
        spec = importlib.util.spec_from_file_location("analytics_overview_migration", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_migration_grants_and_revokes_only_intended_roles(self):
        migration = self._load_migration()
        super_admin = ManagerRole.objects.create(
            slug="super-admin", name="Super Administrator", is_system=True, permissions=["analytics.view"])
        general = ManagerRole.objects.create(
            slug="general-manager", name="General Manager", is_system=True, permissions=["analytics.view"])
        finance = ManagerRole.objects.create(
            slug="finance-manager", name="Finance Manager", is_system=True, permissions=["analytics.financial"])
        marketing = ManagerRole.objects.create(
            slug="marketing-manager", name="Marketing Manager", is_system=True, permissions=["campaign.view"])

        migration.grant_analytics_overview(apps, None)

        super_admin.refresh_from_db(); general.refresh_from_db()
        finance.refresh_from_db(); marketing.refresh_from_db()
        self.assertIn("analytics.overview", super_admin.permissions)
        self.assertIn("analytics.overview", general.permissions)
        self.assertNotIn("analytics.overview", finance.permissions)
        self.assertNotIn("analytics.overview", marketing.permissions)

        migration.revoke_analytics_overview(apps, None)
        super_admin.refresh_from_db(); general.refresh_from_db()
        self.assertNotIn("analytics.overview", super_admin.permissions)
        self.assertNotIn("analytics.overview", general.permissions)

    def test_system_role_seed_includes_the_new_code_for_the_two_grantees(self):
        roles = auth_service.ensure_system_roles()
        self.assertIn("analytics.overview", roles[SUPER_ADMIN].permissions)
        self.assertIn("analytics.overview", roles["general-manager"].permissions)
        self.assertNotIn("analytics.overview", roles["finance-manager"].permissions)
        self.assertNotIn("analytics.overview", roles["support-agent"].permissions)
        self.assertNotIn("analytics.overview", roles["system-admin"].permissions)
