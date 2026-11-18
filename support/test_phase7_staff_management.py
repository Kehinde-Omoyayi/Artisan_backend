"""Phase 7 — customer support staff management.

A superuser (or a support administrator) can create a support account or promote
an existing one, and can take someone off the team again. The rules that matter:

* ``is_staff`` plus support groups and nothing else — never a superuser;
* an existing account keeps every unrelated group and permission;
* a new account gets no usable password: the only way in is the existing staff
  password-reset flow;
* nobody can promote or remove themselves;
* removal strips the support groups only;
* every grant and removal is recorded.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework.throttling import SimpleRateThrottle

from home.models import ArtisanProfile, CustomerProfile, StaffPasswordResetToken

from .models import SupportStaffAssignment
from .permissions import SUPPORT_AGENT, SUPPORT_ADMIN, SUPPORT_SUPERVISOR

User = get_user_model()


class SupportStaffManagementTests(TestCase):
    _TEST_THROTTLE_RATES = {
        "anon": "10000/hour",
        "user": "10000/hour",
        "support_risk": "1000/hour",
        "support_create": "1000/hour",
    }

    def setUp(self):
        super().setUp()
        cache.clear()
        patcher = __import__("unittest.mock", fromlist=["patch"]).patch.dict(
            SimpleRateThrottle.THROTTLE_RATES, self._TEST_THROTTLE_RATES
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = APIClient()

        self.superuser = User.objects.create_user(
            email="root@example.com",
            first_name="Root",
            last_name="Admin",
            password="Str0ngPass!23",
            is_active=True,
            is_staff=True,
            is_superuser=True,
        )
        self.existing = User.objects.create_user(
            email="existing@example.com",
            first_name="Existing",
            last_name="Person",
            password="Str0ngPass!23",
            is_active=True,
            email_verified=True,
        )
        CustomerProfile.objects.create(
            user=self.existing, country="Nigeria", state="Lagos", area="Ikeja"
        )
        self.plain_customer = User.objects.create_user(
            email="plain.customer@example.com",
            first_name="Plain",
            last_name="Customer",
            password="Str0ngPass!23",
            is_active=True,
            email_verified=True,
        )
        CustomerProfile.objects.create(
            user=self.plain_customer, country="Nigeria", state="Lagos", area="Ikeja"
        )
        self.artisan_user = User.objects.create_user(
            email="artisan@example.com",
            first_name="Artisan",
            last_name="Person",
            password="Str0ngPass!23",
            is_active=True,
            email_verified=True,
        )
        ArtisanProfile.objects.create(
            user=self.artisan_user,
            business_name="Artisan Co",
            country="Nigeria",
            state="Lagos",
            area="Ikeja",
        )

    def auth(self, user):
        self.client.force_authenticate(user=user)

    def staff_url(self):
        return reverse("support-staff-list")

    def assign(self, **payload):
        return self.client.post(self.staff_url(), payload, format="json")

    # ------------------------------------------------------------ create / assign

    def test_a_superuser_can_create_a_support_account(self):
        self.auth(self.superuser)
        response = self.assign(
            email="new.agent@example.com",
            first_name="New",
            last_name="Agent",
            roles=[SUPPORT_AGENT],
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        created = User.objects.get(email="new.agent@example.com")
        self.assertTrue(created.is_staff)
        self.assertTrue(created.is_active)
        self.assertFalse(created.is_superuser)
        self.assertEqual(
            list(created.groups.values_list("name", flat=True)), [SUPPORT_AGENT]
        )
        # No usable password: entry is only through the reset flow.
        self.assertFalse(created.has_usable_password())

    def test_a_superuser_can_promote_an_existing_account(self):
        self.auth(self.superuser)
        response = self.assign(email="existing@example.com", roles=[SUPPORT_AGENT])

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.existing.refresh_from_db()
        self.assertTrue(self.existing.is_staff)
        self.assertFalse(self.existing.is_superuser)
        self.assertTrue(self.existing.groups.filter(name=SUPPORT_AGENT).exists())
        # The customer profile is untouched: promotion adds, never replaces.
        self.assertTrue(CustomerProfile.objects.filter(user=self.existing).exists())

    def test_promotion_keeps_unrelated_permissions(self):
        unrelated = Group.objects.create(name="Warehouse team")
        unrelated.permissions.add(Permission.objects.first())
        self.existing.groups.add(unrelated)
        before = set(self.existing.groups.values_list("name", flat=True))

        self.auth(self.superuser)
        self.assign(email="existing@example.com", roles=[SUPPORT_AGENT])

        self.existing.refresh_from_db()
        self.assertTrue(before <= set(self.existing.groups.values_list("name", flat=True)))
        self.assertTrue(self.existing.groups.filter(name="Warehouse team").exists())
        self.assertTrue(self.existing.groups.filter(name=SUPPORT_AGENT).exists())

    def test_a_new_account_is_never_a_superuser_and_never_gets_a_password(self):
        self.auth(self.superuser)
        before = User.objects.filter(is_superuser=True).count()
        self.assign(email="another.agent@example.com", roles=[SUPPORT_ADMIN])

        self.assertEqual(User.objects.filter(is_superuser=True).count(), before)
        created = User.objects.get(email="another.agent@example.com")
        self.assertFalse(created.is_superuser)
        self.assertFalse(created.has_usable_password())
        self.assertEqual(StaffPasswordResetToken.objects.filter(user=created).count(), 0)

    def test_an_existing_superuser_cannot_be_reassigned(self):
        second_admin = User.objects.create_user(
            email="other.root@example.com",
            first_name="Other",
            last_name="Root",
            password="Str0ngPass!23",
            is_active=True,
            is_staff=True,
            is_superuser=True,
        )
        self.auth(self.superuser)
        response = self.assign(email=second_admin.email, roles=[SUPPORT_AGENT])
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    # ------------------------------------------------------------ who may assign

    def test_a_customer_cannot_assign_support_staff(self):
        self.auth(self.plain_customer)
        response = self.assign(email="nope@example.com", roles=[SUPPORT_AGENT])
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_an_artisan_cannot_assign_support_staff(self):
        self.auth(self.artisan_user)
        response = self.assign(email="nope@example.com", roles=[SUPPORT_AGENT])
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_support_agent_cannot_assign_support_staff(self):
        agent = User.objects.create_user(
            email="agent.only@example.com",
            first_name="Agent",
            last_name="Only",
            password="Str0ngPass!23",
            is_active=True,
            is_staff=True,
        )
        agent.groups.add(Group.objects.get_or_create(name=SUPPORT_AGENT)[0])
        self.auth(agent)
        response = self.assign(email="nope@example.com", roles=[SUPPORT_SUPERVISOR])
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_support_admin_can_assign_support_staff(self):
        admin = User.objects.create_user(
            email="support.admin@example.com",
            first_name="Support",
            last_name="Admin",
            password="Str0ngPass!23",
            is_active=True,
            is_staff=True,
        )
        admin.groups.add(Group.objects.get_or_create(name=SUPPORT_ADMIN)[0])
        self.auth(admin)
        response = self.assign(email="by.admin@example.com", roles=[SUPPORT_AGENT])
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_at_least_one_role_is_required(self):
        self.auth(self.superuser)
        response = self.assign(email="noroles@example.com", roles=[])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_an_unknown_role_is_refused(self):
        self.auth(self.superuser)
        response = self.assign(email="badrole@example.com", roles=["SUPERUSER"])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    # ------------------------------------------------------------ audit + listing

    def test_the_assignment_is_recorded_with_who_made_it(self):
        self.auth(self.superuser)
        self.assign(email="recorded@example.com", roles=[SUPPORT_AGENT])

        assignment = SupportStaffAssignment.objects.get(
            user__email="recorded@example.com"
        )
        self.assertEqual(assignment.assigned_by_id, self.superuser.id)
        self.assertEqual(assignment.roles, [SUPPORT_AGENT])
        self.assertTrue(assignment.created_account)
        self.assertIsNone(assignment.removed_at)

    def test_the_staff_list_carries_roles_and_the_assigned_date(self):
        self.auth(self.superuser)
        self.assign(email="listed@example.com", roles=[SUPPORT_AGENT, SUPPORT_FINANCE]
                    if False else [SUPPORT_AGENT])

        response = self.client.get(self.staff_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        row = next(item for item in response.data if item["email"] == "listed@example.com")
        self.assertEqual(row["roles"], [SUPPORT_AGENT])
        self.assertIsNotNone(row.get("assigned_at"))
        self.assertEqual(row.get("assigned_by"), "Root Admin")

    def test_the_assignment_audit_trail_is_staff_only(self):
        self.auth(self.plain_customer)
        response = self.client.get(reverse("support-staff-assignments"))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        self.auth(self.superuser)
        response = self.client.get(reverse("support-staff-assignments"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    # ------------------------------------------------------------ removal

    def test_a_support_member_can_be_removed(self):
        self.auth(self.superuser)
        created = self.assign(email="removable@example.com", roles=[SUPPORT_AGENT])
        user_id = created.data["id"]

        response = self.client.post(
            reverse("support-staff-remove", args=[user_id]),
            {"reason": "Left the team"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        removed = User.objects.get(pk=user_id)
        self.assertFalse(removed.groups.filter(name=SUPPORT_AGENT).exists())
        self.assertFalse(removed.is_staff)
        self.assertFalse(removed.is_superuser)
        assignment = SupportStaffAssignment.objects.get(user_id=user_id)
        self.assertIsNotNone(assignment.removed_at)
        self.assertEqual(assignment.removal_reason, "Left the team")

    def test_removal_never_strips_unrelated_groups(self):
        self.auth(self.superuser)
        created = self.assign(email="keep.groups@example.com", roles=[SUPPORT_AGENT])
        user_id = created.data["id"]
        target = User.objects.get(pk=user_id)
        target.groups.add(Group.objects.get_or_create(name="Warehouse team")[0])

        self.client.post(
            reverse("support-staff-remove", args=[user_id]), {}, format="json"
        )

        target.refresh_from_db()
        self.assertTrue(target.groups.filter(name="Warehouse team").exists())
        self.assertFalse(target.groups.filter(name=SUPPORT_AGENT).exists())

    def test_nobody_can_remove_their_own_support_access(self):
        admin = User.objects.create_user(
            email="self.admin@example.com",
            first_name="Self",
            last_name="Admin",
            password="Str0ngPass!23",
            is_active=True,
            is_staff=True,
        )
        admin.groups.add(Group.objects.get_or_create(name=SUPPORT_ADMIN)[0])
        self.auth(admin)
        response = self.client.post(
            reverse("support-staff-remove", args=[admin.id]), {}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_non_staff_account_cannot_be_removed(self):
        self.auth(self.superuser)
        response = self.client.post(
            reverse("support-staff-remove", args=[self.plain_customer.id]),
            {},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_superuser_cannot_be_removed_here(self):
        other_root = User.objects.create_user(
            email="root2@example.com",
            first_name="Root2",
            last_name="Admin",
            password="Str0ngPass!23",
            is_active=True,
            is_staff=True,
            is_superuser=True,
        )
        self.auth(self.superuser)
        response = self.client.post(
            reverse("support-staff-remove", args=[other_root.id]), {}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_removal_invalidates_any_pending_staff_reset(self):
        from django.utils import timezone

        from home.models import StaffSecurityChallenge

        self.auth(self.superuser)
        created = self.assign(email="reset.pending@example.com", roles=[SUPPORT_AGENT])
        user_id = created.data["id"]
        target = User.objects.get(pk=user_id)
        challenge = StaffSecurityChallenge.objects.create(
            user=target,
            purpose=StaffSecurityChallenge.Purpose.PASSWORD_RESET,
            otp_hash="y",
            expires_at=timezone.now() + timedelta(hours=1),
        )
        StaffPasswordResetToken.objects.create(
            user=target,
            challenge=challenge,
            token_hash="x",
            expires_at=timezone.now() + timedelta(hours=1),
        )

        self.client.post(
            reverse("support-staff-remove", args=[user_id]), {}, format="json"
        )

        self.assertEqual(
            StaffPasswordResetToken.objects.filter(
                user=target, consumed_at__isnull=True
            ).count(),
            0,
        )
        self.assertEqual(
            StaffSecurityChallenge.objects.filter(
                user=target, consumed_at__isnull=True, invalidated_at__isnull=True
            ).count(),
            0,
        )
