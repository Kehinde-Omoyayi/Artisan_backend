"""Final integration — Workflow C (payment cancellation → risk) and Workflow D
(booking cancellation and resend), plus the security and edge cases that must
hold across both.

These tests run the whole chain the way a user does: through the HTTP surface,
with the real services, the real notifications and the real audit rows.
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import Group
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework.throttling import SimpleRateThrottle

from booking.models import Booking
from core.models import OutboxEvent
from home.models import ArtisanProfile, CustomUser, CustomerProfile
from notifications.models import InAppNotification
from payments.models import EscrowPayment
from services.models import ServiceRequest

from .models import CustomerPaymentCancellationEvent, CustomerRiskProfile
from .services import (
    get_customer_risk_profile,
    record_customer_payment_cancellation,
    risk_percentage,
)


def make_service_request(requester, *, label="int"):
    from home.models import Locations, ServiceCategory

    category = ServiceCategory.objects.create(name=f"{label} cat {ServiceCategory.objects.count()}")
    location = Locations.objects.create(
        country="Nigeria", state="Lagos", area=f"{label} area {Locations.objects.count()}"
    )
    return ServiceRequest.objects.create(
        requester=requester,
        category=category,
        location=location,
        preferred_date=(timezone.now() + timedelta(days=2)).date(),
        description="Integration test request",
    )


def make_payment(booking, customer_user, artisan, *, index=1, status="PAYMENT_PENDING"):
    service_kobo = int(Decimal("25000.00") * 100)
    fee_kobo = 15000
    return EscrowPayment.objects.create(
        booking=booking,
        customer=customer_user,
        artisan=artisan,
        reference=f"PAY-INT-{index}-{EscrowPayment.objects.count()}",
        service_amount_kobo=service_kobo,
        customer_fee_kobo=fee_kobo,
        total_amount_kobo=service_kobo + fee_kobo,
        platform_commission_kobo=int(service_kobo * Decimal("0.1")),
        artisan_payout_kobo=service_kobo - int(service_kobo * Decimal("0.1")),
        status=status,
    )


class IntegrationTestCase(TestCase):
    _TEST_THROTTLE_RATES = {
        "anon": "10000/hour",
        "user": "10000/hour",
        "payment_cancel": "1000/hour",
        "payment_initialize": "1000/hour",
        "booking_accept": "1000/hour",
        "booking_reject": "1000/hour",
        "booking_cancel": "1000/hour",
        "support_create": "1000/hour",
        "support_access": "1000/hour",
    }

    def setUp(self):
        super().setUp()
        cache.clear()
        patcher = patch.dict(SimpleRateThrottle.THROTTLE_RATES, self._TEST_THROTTLE_RATES)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = APIClient()

        self.customer_user = CustomUser.objects.create_user(
            email="integration.customer@example.com",
            first_name="Ada",
            last_name="Customer",
            password="StrongPassword123!",
            is_active=True,
            email_verified=True,
        )
        self.customer = CustomerProfile.objects.create(user=self.customer_user)
        self.artisan_user = CustomUser.objects.create_user(
            email="integration.artisan@example.com",
            first_name="Bayo",
            last_name="Artisan",
            password="StrongPassword123!",
            is_active=True,
            email_verified=True,
        )
        self.artisan = ArtisanProfile.objects.create(
            user=self.artisan_user, profile_status="ACTIVE"
        )
        from services.models import ArtisanVerification

        ArtisanVerification.objects.create(
            artisan=self.artisan,
            verification_status="VERIFIED",
            verification_method="NIN",
        )

    def auth(self, user):
        self.client.force_authenticate(user=user)

    def make_booking(self, *, status=Booking.Status.ACCEPTED, request_label="int"):
        request = make_service_request(self.customer_user, label=request_label)
        return Booking.objects.create(
            request=request,
            artisan=self.artisan,
            customer=self.customer,
            scheduled_time_start=timezone.now() + timedelta(days=1),
            scheduled_time_end=timezone.now() + timedelta(days=1, hours=2),
            price_agreed=Decimal("25000.00"),
            status=status,
        )

    def risk_endpoint(self):
        self.auth(self.customer_user)
        response = self.client.get("/api/v1/risk/customer/cancellation-risk/")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data


# ---------------------------------------------------------------------------
# Workflow C — payment cancellation → risk
# ---------------------------------------------------------------------------


class PaymentCancellationRiskWorkflowTests(IntegrationTestCase):
    def test_workflow_c_payment_cancellation_scores_exactly_one_point(self):
        booking = self.make_booking()
        payment = make_payment(booking, self.customer_user, self.artisan)

        # 0/5 before anything happens.
        profile = get_customer_risk_profile(self.customer)
        self.assertEqual(profile.cancellation_points, 0)
        self.assertEqual(risk_percentage(profile.cancellation_points), 0)

        # The customer cancels the payment; the payment row moves first, then
        # the cancellation is recorded in the same transaction.
        EscrowPayment.objects.filter(pk=payment.pk).update(
            status=EscrowPayment.Status.CANCELLED
        )
        payment.refresh_from_db()
        record_customer_payment_cancellation(
            payment=payment,
            reason_code="CUSTOMER_CHANGED_MIND",
            reason_text="Could not pay yet.",
            initiated_by=CustomerPaymentCancellationEvent.Initiator.CUSTOMER,
        )

        profile = get_customer_risk_profile(self.customer)
        self.assertEqual(profile.cancellation_points, 1)
        self.assertEqual(risk_percentage(profile.cancellation_points), 20)

        payload = self.risk_endpoint()
        self.assertEqual(payload["cancellation_points"], 1)
        self.assertEqual(payload["score_label"], "1/5")
        self.assertEqual(payload["risk_percentage"], 20)
        self.assertEqual(len(payload["payment_cancellations"]), 1)
        self.assertEqual(
            payload["payment_cancellations"][0]["payment_reference"], payment.reference
        )
        self.assertEqual(payload["payment_cancellations"][0]["initiated_by"], "CUSTOMER")

    def test_workflow_c_repeat_requests_add_nothing(self):
        booking = self.make_booking()
        payment = make_payment(booking, self.customer_user, self.artisan)
        EscrowPayment.objects.filter(pk=payment.pk).update(
            status=EscrowPayment.Status.CANCELLED
        )
        payment.refresh_from_db()

        for _ in range(5):
            record_customer_payment_cancellation(
                payment=payment, reason_text="Retry from a double click."
            )

        profile = get_customer_risk_profile(self.customer)
        self.assertEqual(profile.cancellation_points, 1)
        self.assertEqual(
            CustomerPaymentCancellationEvent.objects.filter(payment=payment).count(), 1
        )
        self.risk_endpoint()

    def test_workflow_c_a_second_payment_adds_a_second_point(self):
        first = self.make_booking(request_label="wfca")
        second = self.make_booking(request_label="wfca")
        first_payment = make_payment(first, self.customer_user, self.artisan, index=1)
        second_payment = make_payment(second, self.customer_user, self.artisan, index=2)
        for payment in (first_payment, second_payment):
            EscrowPayment.objects.filter(pk=payment.pk).update(
                status=EscrowPayment.Status.CANCELLED
            )
            payment.refresh_from_db()
            record_customer_payment_cancellation(payment=payment, reason_text="Changed plans.")

        self.assertEqual(get_customer_risk_profile(self.customer).cancellation_points, 2)
        payload = self.risk_endpoint()
        self.assertEqual(payload["score_label"], "2/5")
        self.assertEqual(payload["risk_percentage"], 40)

    def test_workflow_c_the_customer_is_notified_once(self):
        booking = self.make_booking()
        payment = make_payment(booking, self.customer_user, self.artisan)
        EscrowPayment.objects.filter(pk=payment.pk).update(
            status=EscrowPayment.Status.CANCELLED
        )
        payment.refresh_from_db()

        record_customer_payment_cancellation(payment=payment, reason_text="Changed my mind.")
        record_customer_payment_cancellation(payment=payment, reason_text="Retry.")

        event_keys = list(
            OutboxEvent.objects.filter(
                event_key__startswith="customer-payment-cancellation-risk:"
            ).values_list("event_key", flat=True)
        )
        self.assertEqual(len(event_keys), 1)
        self.assertTrue(
            InAppNotification.objects.filter(
                recipient=self.customer_user,
                event_key__startswith="customer-payment-cancellation-risk:",
            ).exists()
        )

    def test_workflow_c_the_score_never_exceeds_five(self):
        bookings = [self.make_booking(request_label="wfccap") for _ in range(7)]
        for index, booking in enumerate(bookings, start=1):
            payment = make_payment(booking, self.customer_user, self.artisan, index=index)
            EscrowPayment.objects.filter(pk=payment.pk).update(
                status=EscrowPayment.Status.CANCELLED
            )
            payment.refresh_from_db()
            record_customer_payment_cancellation(payment=payment, reason_text="Changed plans.")

        profile = get_customer_risk_profile(self.customer)
        self.assertEqual(profile.cancellation_points, 5)
        payload = self.risk_endpoint()
        self.assertEqual(payload["score_label"], "5/5")
        self.assertEqual(payload["risk_percentage"], 100)

    def test_workflow_c_the_account_is_never_blocked_automatically(self):
        booking = self.make_booking()
        payment = make_payment(booking, self.customer_user, self.artisan)
        EscrowPayment.objects.filter(pk=payment.pk).update(
            status=EscrowPayment.Status.CANCELLED
        )
        payment.refresh_from_db()
        for _ in range(6):
            record_customer_payment_cancellation(payment=payment, reason_text="Changed plans.")

        self.customer_user.refresh_from_db()
        self.assertTrue(self.customer_user.is_active)
        self.assertNotEqual(self.customer_user.security_status, "LOCKED")
        self.assertEqual(CustomerRiskProfile.objects.get(customer=self.customer).cancellation_points, 1)

    def test_a_cancellation_by_the_artisan_or_the_provider_scores_nothing(self):
        booking = self.make_booking()
        payment = make_payment(booking, self.customer_user, self.artisan)
        EscrowPayment.objects.filter(pk=payment.pk).update(
            status=EscrowPayment.Status.CANCELLED
        )
        payment.refresh_from_db()

        for initiator in (
            CustomerPaymentCancellationEvent.Initiator.ARTISAN,
            CustomerPaymentCancellationEvent.Initiator.ADMIN,
            CustomerPaymentCancellationEvent.Initiator.PROVIDER,
            CustomerPaymentCancellationEvent.Initiator.SYSTEM,
        ):
            record_customer_payment_cancellation(
                payment=payment, reason_text="Not the customer's doing.", initiated_by=initiator
            )

        self.assertEqual(get_customer_risk_profile(self.customer).cancellation_points, 0)

    def test_a_failed_payment_that_was_never_recorded_scores_nothing(self):
        booking = self.make_booking()
        payment = make_payment(
            booking, self.customer_user, self.artisan, status=EscrowPayment.Status.FAILED
        )
        # Left FAILED: the cancellation never happened, so nothing is scored.
        self.assertEqual(get_customer_risk_profile(self.customer).cancellation_points, 0)
        self.assertEqual(
            CustomerPaymentCancellationEvent.objects.filter(payment=payment).count(), 0
        )


# ---------------------------------------------------------------------------
# Workflow D — booking cancellation and resend
# ---------------------------------------------------------------------------


class BookingCancellationResendWorkflowTests(IntegrationTestCase):
    def test_workflow_d_booking_cancellation_scores_no_risk_point(self):
        booking = self.make_booking(status=Booking.Status.REQUESTED)

        self.auth(self.customer_user)
        response = self.client.post(
            reverse("booking-cancel", args=[booking.pk]),
            {
                "reason_code": "CUSTOMER_CHANGED_MIND",
                "reason": "CUSTOMER_CHANGED_MIND",
                "reason_text": "Plans changed.",
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="wf-d-cancel-1",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CANCELLED)

        # A booking cancellation is not a payment cancellation: no point.
        self.assertEqual(get_customer_risk_profile(self.customer).cancellation_points, 0)
        self.assertEqual(
            CustomerPaymentCancellationEvent.objects.filter(
                customer=self.customer
            ).count(),
            0,
        )

    def test_workflow_d_resend_restores_the_artisans_decision(self):
        booking = self.make_booking(status=Booking.Status.REQUESTED)

        self.auth(self.artisan_user)
        rejected = self.client.post(
            reverse("booking-reject", args=[booking.pk]),
            {"reason": "I cannot take this date."},
            format="json",
            HTTP_IDEMPOTENCY_KEY="wf-d-reject-1",
        )
        self.assertEqual(rejected.status_code, status.HTTP_200_OK, rejected.data)

        # Decision buttons are gone on the rejected version …
        detail = self.client.get(reverse("booking-detail", args=[booking.pk]))
        codes = [item["code"] for item in detail.data["available_actions"]]
        self.assertNotIn("ACCEPT_BOOKING", codes)
        self.assertNotIn("REJECT_BOOKING", codes)
        self.assertIn("RESEND_BOOKING", codes)

        # … and return on the resent version.
        resent = self.client.post(
            reverse("booking-resend", args=[booking.pk]),
            {"reason": "New date agreed."},
            format="json",
            HTTP_IDEMPOTENCY_KEY="wf-d-resend-1",
        )
        self.assertEqual(resent.status_code, status.HTTP_201_CREATED, resent.data)
        version_id = resent.data["id"]
        self.assertNotEqual(version_id, str(booking.pk))

        detail = self.client.get(reverse("booking-detail", args=[version_id]))
        codes = [item["code"] for item in detail.data["available_actions"]]
        self.assertIn("ACCEPT_BOOKING", codes)
        self.assertIn("REJECT_BOOKING", codes)

        # The artisan accepts the resent version.
        accepted = self.client.post(
            reverse("booking-accept", args=[version_id]),
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY="wf-d-accept-1",
        )
        self.assertEqual(accepted.status_code, status.HTTP_200_OK, accepted.data)
        self.assertEqual(Booking.objects.get(pk=version_id).status, Booking.Status.ACCEPTED)

        # And a second version can be rejected instead.
        second = self.make_booking(status=Booking.Status.REQUESTED, request_label="wfdd")
        rejected_again = self.client.post(
            reverse("booking-reject", args=[second.pk]),
            {"reason": "Still unavailable."},
            format="json",
            HTTP_IDEMPOTENCY_KEY="wf-d-reject-2",
        )
        self.assertEqual(rejected_again.status_code, status.HTTP_200_OK, rejected_again.data)
        self.assertEqual(Booking.objects.get(pk=second.pk).status, Booking.Status.REJECTED)

    def test_workflow_d_the_full_history_is_retained(self):
        booking = self.make_booking(status=Booking.Status.REQUESTED)
        self.auth(self.artisan_user)
        self.client.post(
            reverse("booking-reject", args=[booking.pk]),
            {"reason": "I cannot take this date."},
            format="json",
            HTTP_IDEMPOTENCY_KEY="wf-d-reject-3",
        )
        resent = self.client.post(
            reverse("booking-resend", args=[booking.pk]),
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY="wf-d-resend-2",
        )
        version_id = resent.data["id"]

        response = self.client.get(reverse("booking-history", args=[booking.pk]))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        rows = response.data["results"] if isinstance(response.data, dict) else response.data
        statuses = [row.get("status") for row in rows]
        self.assertIn(Booking.Status.REJECTED, statuses)
        self.assertIn(
            "I cannot take this date.", " ".join(str(row.get("notes") or "") for row in rows)
        )

        # The rejected version is still there, still rejected.
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.REJECTED)
        self.assertEqual(Booking.objects.get(pk=version_id).resend_of_id, booking.pk)

    def test_workflow_d_a_superseded_version_cannot_be_decided(self):
        booking = self.make_booking(status=Booking.Status.REQUESTED)
        Booking.objects.filter(pk=booking.pk).update(superseded_at=timezone.now())
        self.auth(self.artisan_user)

        accepted = self.client.post(
            reverse("booking-accept", args=[booking.pk]),
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY="wf-d-accept-2",
        )
        rejected = self.client.post(
            reverse("booking-reject", args=[booking.pk]),
            {"reason": "Obsolete."},
            format="json",
            HTTP_IDEMPOTENCY_KEY="wf-d-reject-4",
        )
        self.assertEqual(accepted.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(rejected.status_code, status.HTTP_409_CONFLICT)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.REQUESTED)

    def test_the_customer_cannot_accept_or_reject_a_booking(self):
        booking = self.make_booking(status=Booking.Status.REQUESTED)
        self.auth(self.customer_user)
        response = self.client.post(
            reverse("booking-accept", args=[booking.pk]),
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY="wf-d-accept-3",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


# ---------------------------------------------------------------------------
# Security and edge cases
# ---------------------------------------------------------------------------


class SecurityEdgeCaseTests(IntegrationTestCase):
    def test_1_a_customer_cannot_read_another_customers_risk_file(self):
        other_user = CustomUser.objects.create_user(
            email="other.integration@example.com",
            first_name="Other",
            last_name="Customer",
            password="StrongPassword123!",
            is_active=True,
        )
        CustomerProfile.objects.create(user=other_user)
        self.auth(other_user)
        response = self.client.get("/api/v1/risk/customer/cancellation-risk/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["cancellation_points"], 0)
        self.assertEqual(len(response.data["payment_cancellations"]), 0)

    def test_2_an_anonymous_caller_is_refused_the_risk_endpoints(self):
        response = self.client.get("/api/v1/risk/customer/cancellation-risk/")
        self.assertIn(
            response.status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )

    def test_3_a_plain_customer_cannot_search_or_adjust_risk(self):
        self.auth(self.customer_user)
        search = self.client.get("/api/v1/risk/support/customers/", {"search": "ada"})
        adjust = self.client.post(
            f"/api/v1/risk/support/customers/{self.customer.pk}/adjust/",
            {"action": "RESET", "reason": "Let me in."},
            format="json",
        )
        self.assertIn(
            search.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN)
        )
        self.assertIn(
            adjust.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN)
        )

    def test_4_an_adjustment_requires_a_reason(self):
        agent = CustomUser.objects.create_user(
            email="integration.agent@example.com",
            first_name="Support",
            last_name="Agent",
            password="StrongPassword123!",
            is_active=True,
            is_staff=True,
        )
        agent.groups.add(Group.objects.get_or_create(name="CUSTOMER_SUPPORT_AGENT")[0])
        self.auth(agent)
        response = self.client.post(
            f"/api/v1/risk/support/customers/{self.customer.pk}/adjust/",
            {"action": "RESET"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_5_the_score_is_never_written_above_five_or_below_zero(self):
        agent = CustomUser.objects.create_user(
            email="integration.agent2@example.com",
            first_name="Support",
            last_name="Agent",
            password="StrongPassword123!",
            is_active=True,
            is_staff=True,
        )
        agent.groups.add(Group.objects.get_or_create(name="CUSTOMER_SUPPORT_AGENT")[0])
        self.auth(agent)
        too_high = self.client.post(
            f"/api/v1/risk/support/customers/{self.customer.pk}/adjust/",
            {"action": "SET", "points": 99, "reason": "Testing the ceiling."},
            format="json",
        )
        self.assertEqual(too_high.status_code, status.HTTP_400_BAD_REQUEST)

        remove = self.client.post(
            f"/api/v1/risk/support/customers/{self.customer.pk}/adjust/",
            {"action": "REMOVE", "points": 3, "reason": "Goodwill gesture."},
            format="json",
        )
        self.assertEqual(remove.status_code, status.HTTP_200_OK, remove.data)
        self.assertEqual(get_customer_risk_profile(self.customer).cancellation_points, 0)

    def test_6_an_adjustment_never_deletes_the_payment_history(self):
        booking = self.make_booking()
        payment = make_payment(booking, self.customer_user, self.artisan)
        EscrowPayment.objects.filter(pk=payment.pk).update(
            status=EscrowPayment.Status.CANCELLED
        )
        payment.refresh_from_db()
        record_customer_payment_cancellation(payment=payment, reason_text="Changed plans.")

        agent = CustomUser.objects.create_user(
            email="integration.agent3@example.com",
            first_name="Support",
            last_name="Agent",
            password="StrongPassword123!",
            is_active=True,
            is_staff=True,
        )
        agent.groups.add(Group.objects.get_or_create(name="CUSTOMER_SUPPORT_AGENT")[0])
        self.auth(agent)
        self.client.post(
            f"/api/v1/risk/support/customers/{self.customer.pk}/adjust/",
            {"action": "RESET", "reason": "Appeal upheld."},
            format="json",
        )

        self.assertEqual(get_customer_risk_profile(self.customer).cancellation_points, 0)
        self.assertTrue(EscrowPayment.objects.filter(pk=payment.pk).exists())
        self.assertEqual(
            CustomerPaymentCancellationEvent.objects.filter(payment=payment).count(), 1
        )
        payload = self.risk_endpoint()
        self.assertEqual(len(payload["payment_cancellations"]), 1)

    def test_7_the_risk_endpoint_never_exposes_another_customers_email(self):
        other_user = CustomUser.objects.create_user(
            email="third.party@example.com",
            first_name="Third",
            last_name="Party",
            password="StrongPassword123!",
            is_active=True,
        )
        CustomerProfile.objects.create(user=other_user)
        payload = self.risk_endpoint()
        self.assertNotIn("third.party@example.com", str(payload))
        self.assertEqual(payload["cancellation_count"], 0)
        self.assertEqual(payload["payment_cancellations"], [])
        self.assertEqual(payload["adjustments"], [])

    def test_8_an_unverified_email_cannot_open_the_appeal_route(self):
        unverified = CustomUser.objects.create_user(
            email="unverified.integration@example.com",
            first_name="Un",
            last_name="Verified",
            password="StrongPassword123!",
            is_active=True,
            email_verified=False,
        )
        self.auth(unverified)
        response = self.client.post(
            "/api/v1/support/cases/",
            {
                "case_type": "ACCOUNT",
                "priority": "HIGH",
                "subject": "Please help",
                "description": "I need my account reviewed by a human being please.",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_9_a_forged_appeal_token_is_refused(self):
        response = self.client.post(
            "/api/v1/support/cases/",
            {
                "case_type": "ACCOUNT",
                "priority": "HIGH",
                "subject": "Please help",
                "description": "I need my account reviewed by a human being please.",
            },
            format="json",
            HTTP_X_SUPPORT_ACCESS_TOKEN="forged-token",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_10_a_booking_decision_is_rejected_outside_the_allowed_statuses(self):
        booking = self.make_booking(status=Booking.Status.ACCEPTED)
        self.auth(self.artisan_user)
        response = self.client.post(
            reverse("booking-accept", args=[booking.pk]),
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY="edge-10",
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_11_a_duplicate_decision_is_not_applied_twice(self):
        booking = self.make_booking(status=Booking.Status.REQUESTED)
        self.auth(self.artisan_user)
        first = self.client.post(
            reverse("booking-accept", args=[booking.pk]),
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY="edge-11",
        )
        second = self.client.post(
            reverse("booking-accept", args=[booking.pk]),
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY="edge-11",
        )
        self.assertEqual(first.status_code, status.HTTP_200_OK)
        self.assertIn(second.status_code, (status.HTTP_200_OK, status.HTTP_409_CONFLICT))
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.ACCEPTED)

    def test_12_a_non_participant_cannot_see_or_decide_a_booking(self):
        booking = self.make_booking(status=Booking.Status.REQUESTED)
        outsider = CustomUser.objects.create_user(
            email="outsider.integration@example.com",
            first_name="Out",
            last_name="Sider",
            password="StrongPassword123!",
            is_active=True,
        )
        self.auth(outsider)
        self.assertEqual(
            self.client.get(reverse("booking-detail", args=[booking.pk])).status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertEqual(
            self.client.post(
                reverse("booking-accept", args=[booking.pk]),
                {},
                format="json",
                HTTP_IDEMPOTENCY_KEY="edge-12",
            ).status_code,
            status.HTTP_404_NOT_FOUND,
        )

    def test_13_an_artisan_cannot_resend_a_booking_that_is_still_pending(self):
        booking = self.make_booking(status=Booking.Status.REQUESTED)
        self.auth(self.artisan_user)
        response = self.client.post(
            reverse("booking-resend", args=[booking.pk]),
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY="edge-13",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_14_the_customer_can_cancel_a_requested_booking_and_it_stays_cancelled(self):
        booking = self.make_booking(status=Booking.Status.REQUESTED)
        self.auth(self.customer_user)
        first = self.client.post(
            reverse("booking-cancel", args=[booking.pk]),
            {
                "reason_code": "SCHEDULE_CONFLICT",
                "reason": "SCHEDULE_CONFLICT",
                "reason_text": "Date no longer works.",
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="edge-14",
        )
        self.assertEqual(first.status_code, status.HTTP_200_OK, first.data)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CANCELLED)

        again = self.client.post(
            reverse("booking-cancel", args=[booking.pk]),
            {
                "reason_code": "SCHEDULE_CONFLICT",
                "reason": "SCHEDULE_CONFLICT",
                "reason_text": "Date no longer works.",
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="edge-14b",
        )
        self.assertEqual(again.status_code, status.HTTP_409_CONFLICT)

    def test_15_a_cancelled_booking_offers_the_artisan_no_decision(self):
        booking = self.make_booking(status=Booking.Status.CANCELLED)
        self.auth(self.artisan_user)
        detail = self.client.get(reverse("booking-detail", args=[booking.pk]))
        self.assertEqual(detail.status_code, status.HTTP_200_OK)
        codes = [item["code"] for item in detail.data["available_actions"]]
        self.assertNotIn("ACCEPT_BOOKING", codes)
        self.assertNotIn("REJECT_BOOKING", codes)

    def test_16_the_risk_percentage_mapping_is_exact(self):
        booking = self.make_booking()
        expected = {0: 0, 1: 20, 2: 40, 3: 60, 4: 80, 5: 100}
        payments = []
        for index in range(1, 6):
            extra = self.make_booking(request_label="wfcmap")
            payment = make_payment(extra, self.customer_user, self.artisan, index=index)
            EscrowPayment.objects.filter(pk=payment.pk).update(
                status=EscrowPayment.Status.CANCELLED
            )
            payment.refresh_from_db()
            payments.append(payment)

        self.assertEqual(self.risk_endpoint()["risk_percentage"], expected[0])
        for index, payment in enumerate(payments, start=1):
            record_customer_payment_cancellation(payment=payment, reason_text="Changed plans.")
            payload = self.risk_endpoint()
            self.assertEqual(payload["cancellation_points"], index)
            self.assertEqual(payload["score_label"], f"{index}/5")
            self.assertEqual(payload["risk_percentage"], expected[index])

    def test_17_a_risk_file_survives_a_later_cancellation(self):
        booking = self.make_booking()
        payment = make_payment(booking, self.customer_user, self.artisan)
        EscrowPayment.objects.filter(pk=payment.pk).update(
            status=EscrowPayment.Status.CANCELLED
        )
        payment.refresh_from_db()
        record_customer_payment_cancellation(payment=payment, reason_text="Changed plans.")

        agent = CustomUser.objects.create_user(
            email="integration.agent4@example.com",
            first_name="Support",
            last_name="Agent",
            password="StrongPassword123!",
            is_active=True,
            is_staff=True,
        )
        agent.groups.add(Group.objects.get_or_create(name="CUSTOMER_SUPPORT_AGENT")[0])
        self.auth(agent)
        self.client.post(
            f"/api/v1/risk/support/customers/{self.customer.pk}/adjust/",
            {"action": "RESET", "reason": "Appeal upheld."},
            format="json",
        )
        self.assertEqual(get_customer_risk_profile(self.customer).cancellation_points, 0)

        second_booking = self.make_booking(request_label="survive")
        second_payment = make_payment(
            second_booking, self.customer_user, self.artisan, index=9
        )
        EscrowPayment.objects.filter(pk=second_payment.pk).update(
            status=EscrowPayment.Status.CANCELLED
        )
        second_payment.refresh_from_db()
        record_customer_payment_cancellation(
            payment=second_payment, reason_text="Changed plans again."
        )

        payload = self.risk_endpoint()
        self.assertEqual(payload["cancellation_points"], 1)
        self.assertEqual(len(payload["payment_cancellations"]), 2)
        self.assertEqual(len(payload["adjustments"]), 1)

    def test_18_staff_accounts_are_not_blocked_by_the_email_verification_gate(self):
        agent = CustomUser.objects.create_user(
            email="integration.agent5@example.com",
            first_name="Support",
            last_name="Agent",
            password="StrongPassword123!",
            is_active=True,
            is_staff=True,
            email_verified=False,
        )
        agent.groups.add(Group.objects.get_or_create(name="CUSTOMER_SUPPORT_AGENT")[0])
        self.auth(agent)
        response = self.client.post(
            "/api/v1/support/cases/",
            {
                "case_type": "TECHNICAL",
                "priority": "MEDIUM",
                "subject": "Console problem",
                "description": "The support console is showing an error for this customer.",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_19_a_support_staff_account_is_never_created_as_a_superuser(self):
        superuser = CustomUser.objects.create_user(
            email="integration.root@example.com",
            first_name="Root",
            last_name="Admin",
            password="StrongPassword123!",
            is_active=True,
            is_staff=True,
            is_superuser=True,
        )
        self.auth(superuser)
        response = self.client.post(
            "/api/v1/support/cases/staff/",
            {"email": "integration.newagent@example.com", "roles": ["CUSTOMER_SUPPORT_AGENT"]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        created = CustomUser.objects.get(email="integration.newagent@example.com")
        self.assertFalse(created.is_superuser)
        self.assertTrue(created.is_staff)
        self.assertFalse(created.has_usable_password())

    def test_20_a_customer_cannot_make_themselves_support_staff(self):
        self.auth(self.customer_user)
        response = self.client.post(
            "/api/v1/support/cases/staff/",
            {"email": self.customer_user.email, "roles": ["CUSTOMER_SUPPORT_ADMIN"]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.customer_user.refresh_from_db()
        self.assertFalse(self.customer_user.is_staff)
