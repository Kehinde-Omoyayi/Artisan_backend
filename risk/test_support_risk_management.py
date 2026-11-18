"""Phase 4 — customer support and administrator risk management.

Support can read a customer's payment cancellation history, decide an appeal and
correct the score through one audited endpoint. Nothing else about the customer
is writable there, and every change keeps the original history.
"""

from decimal import Decimal

from django.contrib.auth.models import Group
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from booking.models import Booking
from home.models import ArtisanProfile, CustomUser, CustomerProfile
from notifications.models import InAppNotification
from payments.models import EscrowPayment
from support.models import SupportCase

from .models import CustomerPaymentCancellationEvent, CustomerRiskAdjustment
from .services import get_customer_risk_profile


class SupportRiskManagementTests(TestCase):
    def setUp(self):
        self.client = APIClient()

        self.customer_user = CustomUser.objects.create_user(
            email="appeal-customer@example.com",
            first_name="Ada",
            last_name="Appeal",
            password="StrongPassword123!",
            is_active=True,
        )
        self.customer = CustomerProfile.objects.create(user=self.customer_user)

        self.agent = CustomUser.objects.create_user(
            email="agent@example.com",
            first_name="Support",
            last_name="Agent",
            password="StrongPassword123!",
            is_active=True,
            is_staff=True,
        )
        self.agent.groups.add(
            Group.objects.get_or_create(name="CUSTOMER_SUPPORT_AGENT")[0]
        )

        self.finance = CustomUser.objects.create_user(
            email="finance@example.com",
            first_name="Support",
            last_name="Finance",
            password="StrongPassword123!",
            is_active=True,
            is_staff=True,
        )
        self.finance.groups.add(
            Group.objects.get_or_create(name="CUSTOMER_SUPPORT_FINANCE")[0]
        )

        self.artisan_user = CustomUser.objects.create_user(
            email="appeal-artisan@example.com",
            first_name="Artisan",
            last_name="One",
            password="StrongPassword123!",
            is_active=True,
        )
        self.artisan = ArtisanProfile.objects.create(
            user=self.artisan_user, profile_status="ACTIVE"
        )
        self.booking = Booking.objects.create(
            request=_request(self.customer_user),
            artisan=self.artisan,
            customer=self.customer,
            scheduled_time_start=timezone.now() + timezone.timedelta(days=1),
            scheduled_time_end=timezone.now() + timezone.timedelta(days=1, hours=2),
            price_agreed=Decimal("25000.00"),
            status=Booking.Status.ACCEPTED,
        )
        self.payment = _payment(self.booking, self.customer_user, self.artisan)

    def auth(self, user):
        self.client.force_authenticate(user=user)

    def score_one_point(self):
        from .services import record_customer_payment_cancellation

        record_customer_payment_cancellation(
            payment=self.payment, reason_text="Could not pay yet."
        )
        return get_customer_risk_profile(self.customer)

    # 1 -----------------------------------------------------------------
    def test_support_can_read_a_customer_risk_file(self):
        self.score_one_point()
        self.auth(self.agent)
        response = self.client.get(
            f"/api/v1/risk/support/customers/{self.customer.pk}/"
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["cancellation_points"], 1)
        self.assertEqual(response.data["risk_percentage"], 20)
        self.assertEqual(len(response.data["payment_cancellations"]), 1)
        self.assertEqual(
            response.data["payment_cancellations"][0]["payment_reference"],
            self.payment.reference,
        )
        self.assertEqual(
            response.data["payment_cancellations"][0]["booking_reference"],
            str(self.booking.pk),
        )
        self.assertEqual(response.data["customer"]["email"], self.customer_user.email)

    def test_support_can_search_customers(self):
        self.score_one_point()
        self.auth(self.agent)
        response = self.client.get(
            "/api/v1/risk/support/customers/", {"search": "appeal-customer"}
        )
        self.assertEqual(response.status_code, 200, response.data)
        rows = response.data["results"] if isinstance(response.data, dict) else response.data
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["email"], self.customer_user.email)

    # 2 -----------------------------------------------------------------
    def test_support_can_adjust_points_and_the_change_is_audited(self):
        self.score_one_point()
        case = SupportCase.objects.create(
            opened_by=self.customer_user,
            case_type=SupportCase.CaseType.ACCOUNT,
            subject="Risk point appeal",
            description="The payment failed because of a provider outage.",
        )
        self.auth(self.agent)
        response = self.client.post(
            f"/api/v1/risk/support/customers/{self.customer.pk}/adjust/",
            {
                "action": "REMOVE",
                "reason": "Payment cancelled after a provider outage; appeal upheld.",
                "case_id": case.pk,
                "notify_customer": True,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["cancellation_points"], 0)
        self.assertEqual(response.data["risk_percentage"], 0)

        adjustment = CustomerRiskAdjustment.objects.get()
        self.assertEqual(adjustment.action, "REMOVE")
        self.assertEqual(adjustment.previous_points, 1)
        self.assertEqual(adjustment.new_points, 0)
        self.assertEqual(adjustment.delta, -1)
        self.assertEqual(adjustment.actor_id, self.agent.pk)
        self.assertEqual(adjustment.support_case_id, case.pk)
        self.assertIn("provider outage", adjustment.reason)
        self.assertIsNotNone(adjustment.created_at)

    def test_an_adjustment_reason_is_required(self):
        self.score_one_point()
        self.auth(self.agent)
        response = self.client.post(
            f"/api/v1/risk/support/customers/{self.customer.pk}/adjust/",
            {"action": "REMOVE", "reason": "no"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("reason", response.data.get("details", response.data))

    def test_set_requires_a_target_score(self):
        self.auth(self.agent)
        response = self.client.post(
            f"/api/v1/risk/support/customers/{self.customer.pk}/adjust/",
            {"action": "SET", "reason": "Resetting after a long clean record."},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    # 3 -----------------------------------------------------------------
    def test_the_original_history_survives_an_adjustment(self):
        self.score_one_point()
        self.auth(self.agent)
        response = self.client.post(
            f"/api/v1/risk/support/customers/{self.customer.pk}/adjust/",
            {"action": "RESET", "reason": "Appeal upheld; score cleared."},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["cancellation_points"], 0)
        self.assertEqual(CustomerPaymentCancellationEvent.objects.count(), 1)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, EscrowPayment.Status.PAYMENT_PENDING)

    def test_an_adjustment_survives_the_next_cancellation(self):
        self.score_one_point()
        self.auth(self.agent)
        self.client.post(
            f"/api/v1/risk/support/customers/{self.customer.pk}/adjust/",
            {"action": "REMOVE", "reason": "Appeal upheld for the first point."},
            format="json",
        )
        second_booking = Booking.objects.create(
            request=_request(self.customer_user),
            artisan=self.artisan,
            customer=self.customer,
            scheduled_time_start=timezone.now() + timezone.timedelta(days=2),
            scheduled_time_end=timezone.now() + timezone.timedelta(days=2, hours=2),
            price_agreed=Decimal("25000.00"),
            status=Booking.Status.ACCEPTED,
        )
        second = _payment(second_booking, self.customer_user, self.artisan, index=2)
        second.status = EscrowPayment.Status.CANCELLED
        second.save(update_fields=["status", "updated_at"])
        from .services import record_customer_payment_cancellation

        record_customer_payment_cancellation(payment=second)
        profile = get_customer_risk_profile(self.customer)
        # 2 qualifying cancellations minus the one removed on appeal = 1.
        self.assertEqual(profile.cancellation_points, 1)
        self.assertEqual(profile.event_points, 2)
        self.assertEqual(profile.manual_adjustment_points, -1)

    # 4 -----------------------------------------------------------------
    def test_the_customer_is_told_the_outcome(self):
        from core.models import OutboxEvent

        self.score_one_point()
        self.auth(self.agent)
        self.client.post(
            f"/api/v1/risk/support/customers/{self.customer.pk}/adjust/",
            {"action": "REMOVE", "reason": "Appeal upheld; the outage was ours."},
            format="json",
        )
        self.assertTrue(
            InAppNotification.objects.filter(
                recipient=self.customer_user, event_key__startswith="customer-risk-adjustment:"
            ).exists()
        )
        # The email is queued durably by the existing notification system.
        queued = OutboxEvent.objects.filter(
            event_key__startswith="customer-risk-adjustment:"
        )
        self.assertEqual(queued.count(), 1)
        self.assertEqual(
            queued.first().payload["recipient_email"], self.customer_user.email
        )

    # 5 -----------------------------------------------------------------
    def test_unauthorised_roles_cannot_adjust_risk_points(self):
        self.score_one_point()
        for user in (self.finance, self.customer_user, self.artisan_user):
            self.auth(user)
            response = self.client.post(
                f"/api/v1/risk/support/customers/{self.customer.pk}/adjust/",
                {"action": "RESET", "reason": "Trying without permission."},
                format="json",
            )
            self.assertEqual(
                response.status_code, 403, f"{user.email} was not blocked"
            )
        self.assertEqual(CustomerRiskAdjustment.objects.count(), 0)

    def test_a_customer_cannot_read_another_customers_risk_file(self):
        other_user = CustomUser.objects.create_user(
            email="other-customer@example.com",
            first_name="Other",
            last_name="Customer",
            password="StrongPassword123!",
            is_active=True,
        )
        CustomerProfile.objects.create(user=other_user)
        self.auth(other_user)
        response = self.client.get(
            f"/api/v1/risk/support/customers/{self.customer.pk}/"
        )
        self.assertEqual(response.status_code, 403)

    def test_anonymous_calls_are_rejected(self):
        response = self.client.get("/api/v1/risk/support/customers/")
        self.assertEqual(response.status_code, 401)

    def test_the_customer_only_sees_their_own_risk_status(self):
        self.score_one_point()
        self.auth(self.customer_user)
        response = self.client.get("/api/v1/risk/customer/cancellation-risk/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["cancellation_points"], 1)
        self.assertEqual(response.data["score_label"], "1/5")
        self.assertEqual(response.data["risk_percentage"], 20)
        self.assertEqual(len(response.data["adjustments"]), 0)


def _request(requester):
    from services.models import ServiceRequest
    from home.models import Locations, ServiceCategory

    category = ServiceCategory.objects.create(name=f"Category {ServiceRequest.objects.count()}")
    location = Locations.objects.create(
        country="Nigeria", state="Lagos", area=f"Area {Locations.objects.count()}"
    )
    return ServiceRequest.objects.create(
        requester=requester,
        category=category,
        location=location,
        preferred_date=(timezone.now() + timezone.timedelta(days=2)).date(),
        description="Test request",
    )


def _payment(booking, customer_user, artisan, *, index=1, status="PAYMENT_PENDING"):
    service_kobo = int(Decimal("25000.00") * 100)
    fee_kobo = 15000
    commission = int(service_kobo * Decimal("0.1000"))
    return EscrowPayment.objects.create(
        booking=booking,
        customer=customer_user,
        artisan=artisan,
        reference=f"PAY-TEST-{index}-{EscrowPayment.objects.count()}",
        service_amount_kobo=service_kobo,
        customer_fee_kobo=fee_kobo,
        total_amount_kobo=service_kobo + fee_kobo,
        platform_commission_kobo=commission,
        artisan_payout_kobo=service_kobo - commission,
        status=status,
    )
