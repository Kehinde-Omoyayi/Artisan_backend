from django.test import TestCase, override_settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone
from unittest.mock import patch
from rest_framework import status
from rest_framework.test import APIClient

from booking.models import Booking, BookingStatusHistory
from payments.models import EscrowPayment
from home.models import ArtisanProfile, CustomerProfile, Locations, ServiceCategory
from services.models import ServiceRequest
from core.models import OutboxEvent

from .models import SupportCase, SupportCaseEvent, SupportFinancialAction
from .permissions import SUPPORT_AGENT, SUPPORT_SUPERVISOR
from .services import assign_support_case, close_support_case, create_refund_request, resolve_support_case


class SupportWorkflowTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.customer = User.objects.create_user(
            email="customer@example.com",
            first_name="Customer",
            last_name="User",
            password="StrongPassword123!",
            is_active=True,
        )
        self.staff = User.objects.create_user(
            email="staff@example.com",
            first_name="Staff",
            last_name="User",
            password="StrongPassword123!",
            is_active=True,
            is_staff=True,
        )
        supervisor_group = Group.objects.create(name=SUPPORT_SUPERVISOR)
        self.staff.groups.add(supervisor_group)
        self.case = SupportCase.objects.create(
            opened_by=self.customer,
            case_type=SupportCase.CaseType.PAYMENT,
            priority=SupportCase.Priority.HIGH,
            subject="Payment issue",
            description="Payment needs review.",
        )

    def test_financial_resolution_is_blocked_without_adjustment_workflow(self):
        with self.assertRaises(ValueError):
            resolve_support_case(
                case=self.case,
                staff_user=self.staff,
                resolution_code=SupportCase.ResolutionCode.REFUND,
                resolution_note="Refund customer.",
            )

    def test_staff_assignment_moves_open_case_under_review(self):
        assign_support_case(case=self.case, staff_user=self.staff)
        self.case.refresh_from_db()
        self.assertEqual(self.case.assigned_to_id, self.staff.id)
        self.assertEqual(self.case.status, SupportCase.Status.UNDER_REVIEW)


class SupportCaseMessagingTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.customer_user = User.objects.create_user(
            email="dispute-customer@example.com",
            first_name="Ada",
            last_name="Customer",
            password="StrongPassword123!",
            is_active=True,
        )
        self.artisan_user = User.objects.create_user(
            email="dispute-artisan@example.com",
            first_name="Tunde",
            last_name="Artisan",
            password="StrongPassword123!",
            is_active=True,
        )
        self.outsider = User.objects.create_user(
            email="outsider@example.com",
            first_name="Outside",
            last_name="User",
            password="StrongPassword123!",
            is_active=True,
        )
        self.support_user = User.objects.create_user(
            email="support@example.com",
            first_name="Support",
            last_name="Agent",
            password="StrongPassword123!",
            is_active=True,
            is_staff=True,
        )
        agent_group, _ = Group.objects.get_or_create(name=SUPPORT_AGENT)
        self.support_user.groups.add(agent_group)
        self.supervisor_user = User.objects.create_user(
            email="supervisor@example.com",
            first_name="Support",
            last_name="Supervisor",
            password="StrongPassword123!",
            is_active=True,
            is_staff=True,
        )
        supervisor_group, _ = Group.objects.get_or_create(name=SUPPORT_SUPERVISOR)
        self.supervisor_user.groups.add(supervisor_group)

        self.customer = CustomerProfile.objects.create(user=self.customer_user)
        self.artisan = ArtisanProfile.objects.create(
            user=self.artisan_user,
            profile_status="ACTIVE",
        )
        category = ServiceCategory.objects.create(name="Dispute test category")
        location = Locations.objects.create(
            country="Nigeria",
            state="Lagos",
            area="Yaba",
        )
        service_request = ServiceRequest.objects.create(
            requester=self.customer_user,
            category=category,
            location=location,
            preferred_date=(timezone.now() + timedelta(days=1)).date(),
            description="Complete the repair work.",
        )
        booking = Booking.objects.create(
            request=service_request,
            artisan=self.artisan,
            customer=self.customer,
            scheduled_time_start=timezone.now() + timedelta(days=1),
            scheduled_time_end=timezone.now() + timedelta(days=1, hours=2),
            price_agreed="25000.00",
            status=Booking.Status.DISPUTED,
        )
        BookingStatusHistory.objects.create(
            booking=booking,
            status=Booking.Status.IN_PROGRESS,
            changed_by="ARTISAN",
            changed_by_user=self.artisan_user,
            notes="Artisan started the service.",
        )
        BookingStatusHistory.objects.create(
            booking=booking,
            status=Booking.Status.DISPUTED,
            changed_by="CUSTOMER",
            changed_by_user=self.customer_user,
            notes="Customer opened a dispute.",
        )
        self.case = SupportCase.objects.create(
            opened_by=self.customer_user,
            booking=booking,
            case_type=SupportCase.CaseType.DISPUTE,
            priority=SupportCase.Priority.HIGH,
            subject="Work needs another review",
            description="The booking work does not match the agreed scope.",
        )
        self.client = APIClient()
        self.message_url = reverse("support-case-message", kwargs={"pk": self.case.pk})
        self.events_url = f"/api/v1/support/cases/{self.case.pk}/events/"
        self.detail_url = f"/api/v1/support/cases/{self.case.pk}/"

    def test_support_case_descriptions_require_six_words_and_only_trim_outer_whitespace(self):
        self.client.force_authenticate(self.customer_user)
        create_url = reverse("support-case-list-create")
        base = {
            "case_type": SupportCase.CaseType.REPORT,
            "priority": SupportCase.Priority.MEDIUM,
            "subject": "Review a marketplace issue",
        }

        short = self.client.post(
            create_url,
            {**base, "description": "Please help with this issue."},
            format="json",
        )
        punctuation = self.client.post(
            create_url,
            {**base, "description": " !!! ... --- ??? "},
            format="json",
        )
        self.assertEqual(short.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(punctuation.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("description", short.data["details"])
        self.assertIn("description", punctuation.data["details"])

        original = "  The details here preserve   internal spacing as provided.  "
        created = self.client.post(
            create_url,
            {**base, "description": original},
            format="json",
        )
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.data)
        case = SupportCase.objects.get(pk=created.data["id"])
        self.assertEqual(case.description, original.strip())

    def test_booking_participant_can_send_public_message_and_retry_idempotently(self):
        self.client.force_authenticate(self.customer_user)
        payload = {"message": "I have added the requested booking details."}

        first = self.client.post(
            self.message_url,
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY="support-message-customer-1",
        )
        replay = self.client.post(
            self.message_url,
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY="support-message-customer-1",
        )

        self.assertEqual(first.status_code, status.HTTP_201_CREATED, first.data)
        self.assertEqual(first.data["action"], "CASE_MESSAGE_SENT")
        self.assertEqual(first.data["note"], payload["message"])
        self.assertEqual(first.data["metadata"]["visibility"], "PUBLIC")
        self.assertEqual(replay.status_code, status.HTTP_201_CREATED)
        self.assertEqual(replay["Idempotent-Replayed"], "true")
        self.assertEqual(
            SupportCaseEvent.objects.filter(case=self.case, action="CASE_MESSAGE_SENT").count(),
            1,
        )

    def test_disputed_escrow_remains_eligible_for_the_verified_refund_workflow(self):
        payment = EscrowPayment.objects.create(
            booking=self.case.booking,
            customer=self.customer_user,
            artisan=self.artisan,
            reference="SUPPORT-DISPUTED-REFUND-001",
            provider_transaction_id="flw-disputed-transaction-001",
            service_amount_kobo=1_000_000,
            customer_fee_kobo=15_000,
            total_amount_kobo=1_015_000,
            commission_rate="0.1000",
            platform_commission_kobo=100_000,
            artisan_payout_kobo=900_000,
            status=EscrowPayment.Status.DISPUTED,
        )

        action = create_refund_request(
            case=self.case,
            staff_user=self.support_user,
            payment=payment,
            amount="10150.00",
            reason="Provider-verified payment is being reviewed for a full refund.",
            idempotency_key="disputed-refund-request-001",
        )

        self.assertEqual(action.status, SupportFinancialAction.Status.REQUESTED)
        self.assertEqual(action.action_type, SupportFinancialAction.ActionType.REFUND)
        payment.refresh_from_db()
        self.assertEqual(payment.status, EscrowPayment.Status.DISPUTED)

    def test_artisan_participant_and_support_staff_can_reply(self):
        self.client.force_authenticate(self.artisan_user)
        artisan_reply = self.client.post(
            self.message_url,
            {"message": "I can provide a progress photo for the case."},
            format="json",
            HTTP_IDEMPOTENCY_KEY="support-message-artisan-1",
        )
        self.assertEqual(artisan_reply.status_code, status.HTTP_201_CREATED, artisan_reply.data)
        self.assertEqual(artisan_reply.data["metadata"]["sender_role"], "ARTISAN")

        self.client.force_authenticate(self.support_user)
        staff_reply = self.client.post(
            self.message_url,
            {"message": "Thank you. A support agent is reviewing the evidence."},
            format="json",
            HTTP_IDEMPOTENCY_KEY="support-message-staff-1",
        )
        self.assertEqual(staff_reply.status_code, status.HTTP_201_CREATED, staff_reply.data)
        self.assertEqual(staff_reply.data["metadata"]["sender_role"], "SUPPORT")

    def test_dispute_case_requires_a_related_booking_and_rejects_another_users_booking(self):
        from booking.models import Booking
        from home.models import CustomerProfile

        create_url = reverse("support-case-list-create")
        self.client.force_authenticate(self.customer_user)
        missing_booking = self.client.post(
            create_url,
            {
                "case_type": SupportCase.CaseType.DISPUTE,
                "priority": SupportCase.Priority.HIGH,
                "subject": "Dispute without booking",
                "description": "A dispute must reference the actual booking.",
            },
            format="json",
        )
        self.assertEqual(missing_booking.status_code, status.HTTP_400_BAD_REQUEST)

        outsider_customer = CustomerProfile.objects.create(user=self.outsider)
        unrelated_request = ServiceRequest.objects.create(
            requester=self.outsider,
            category=self.case.booking.request.category,
            location=self.case.booking.request.location,
            preferred_date=(timezone.now() + timedelta(days=2)).date(),
            description="An unrelated customer's booking.",
        )
        unrelated_booking = Booking.objects.create(
            request=unrelated_request,
            artisan=self.artisan,
            customer=outsider_customer,
            scheduled_time_start=timezone.now() + timedelta(days=2),
            scheduled_time_end=timezone.now() + timedelta(days=2, hours=2),
            price_agreed="18000.00",
            status=Booking.Status.IN_PROGRESS,
        )
        cross_participant = self.client.post(
            create_url,
            {
                "booking": str(unrelated_booking.pk),
                "case_type": SupportCase.CaseType.DISPUTE,
                "priority": SupportCase.Priority.HIGH,
                "subject": "Arbitrary booking dispute",
                "description": "The current user is not a participant in this booking.",
            },
            format="json",
        )
        self.assertEqual(cross_participant.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(SupportCase.objects.filter(booking=unrelated_booking).exists())

    def test_only_booking_participants_and_support_staff_can_access_thread(self):
        self.client.force_authenticate(self.outsider)
        self.assertEqual(self.client.get(self.detail_url).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.client.get(self.events_url).status_code, status.HTTP_404_NOT_FOUND)
        denied = self.client.post(
            self.message_url,
            {"message": "This is not my booking."},
            format="json",
            HTTP_IDEMPOTENCY_KEY="support-message-outsider-1",
        )
        self.assertEqual(denied.status_code, status.HTTP_404_NOT_FOUND)

    def test_public_events_are_visible_but_private_staff_notes_are_not(self):
        public_message = SupportCaseEvent.objects.create(
            case=self.case,
            action="CASE_MESSAGE_SENT",
            note="We are checking the case now.",
            performed_by=self.support_user,
            metadata={"visibility": "PUBLIC", "sender_role": "SUPPORT"},
        )
        SupportCaseEvent.objects.create(
            case=self.case,
            action="CASE_NOTE_ADDED",
            note="Internal risk review only.",
            performed_by=self.support_user,
        )
        self.client.force_authenticate(self.customer_user)

        detail = self.client.get(self.detail_url)
        events_response = self.client.get(self.events_url)
        event_rows = events_response.data.get("results", events_response.data)

        self.assertEqual(detail.status_code, status.HTTP_200_OK)
        self.assertTrue(detail.data["can_message"])
        self.assertFalse(detail.data["can_manage"])
        self.assertEqual(events_response.status_code, status.HTTP_200_OK)
        self.assertIn(public_message.id, [row["id"] for row in event_rows])
        self.assertNotIn("Internal risk review only.", [row["note"] for row in event_rows])

    @override_settings(NOTIFICATION_SUPPORT_EMAILS=["support@example.com"])
    @patch("notifications.events._safe_dispatch_outbox")
    def test_participant_message_is_queued_for_configured_support_inbox(self, _dispatch_outbox):
        self.client.force_authenticate(self.customer_user)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                self.message_url,
                {"message": "Please review the additional booking evidence."},
                format="json",
                HTTP_IDEMPOTENCY_KEY="support-message-notification-1",
            )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        support_email = OutboxEvent.objects.get(
            event_type="notification.email",
            payload__recipient_email="support@example.com",
        )
        self.assertIn(self.case.reference, support_email.payload["subject"])
        self.assertIn("additional booking evidence", support_email.payload["message"])

    def test_case_that_is_closed_or_resolved_rejects_new_messages(self):
        self.case.status = SupportCase.Status.RESOLVED
        self.case.save(update_fields=["status", "updated_at"])
        self.client.force_authenticate(self.customer_user)
        detail = self.client.get(self.detail_url)
        self.assertEqual(detail.status_code, status.HTTP_200_OK)
        self.assertFalse(detail.data["can_message"])

        response = self.client.post(
            self.message_url,
            {"message": "I need to add one final detail."},
            format="json",
            HTTP_IDEMPOTENCY_KEY="support-message-resolved-1",
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertFalse(SupportCaseEvent.objects.filter(case=self.case, action="CASE_MESSAGE_SENT").exists())

    def test_capabilities_endpoint_returns_only_the_callers_role_flags(self):
        capabilities_url = reverse("support-capabilities")

        self.client.force_authenticate(self.customer_user)
        customer_response = self.client.get(capabilities_url)
        self.assertEqual(customer_response.status_code, status.HTTP_200_OK)
        self.assertFalse(customer_response.data["is_support_staff"])
        self.assertFalse(customer_response.data["can_approve_refunds"])
        self.assertFalse(customer_response.data["can_execute_refunds"])

        self.client.force_authenticate(self.support_user)
        agent_response = self.client.get(capabilities_url)
        self.assertEqual(agent_response.status_code, status.HTTP_200_OK)
        self.assertTrue(agent_response.data["is_support_staff"])
        self.assertTrue(agent_response.data["can_request_refunds"])
        self.assertTrue(agent_response.data["can_add_private_notes"])
        self.assertFalse(agent_response.data["can_approve_refunds"])
        self.assertFalse(agent_response.data["can_execute_refunds"])

    def test_eligible_payment_list_is_role_scoped_and_hides_provider_data(self):
        payment = EscrowPayment.objects.create(
            booking=self.case.booking,
            customer=self.customer_user,
            artisan=self.artisan,
            reference="SUPPORT-ELIGIBLE-001",
            provider_transaction_id="flw-test-98765",
            service_amount_kobo=1_000_000,
            customer_fee_kobo=15_000,
            total_amount_kobo=1_015_000,
            commission_rate="0.1000",
            platform_commission_kobo=100_000,
            artisan_payout_kobo=900_000,
            status=EscrowPayment.Status.HELD,
            provider_payload={"secret": "not for support queue"},
        )
        eligible_url = reverse(
            "support-case-eligible-payments",
            kwargs={"pk": self.case.pk},
        )

        self.client.force_authenticate(self.customer_user)
        denied = self.client.get(eligible_url)
        self.assertEqual(denied.status_code, status.HTTP_403_FORBIDDEN)

        self.client.force_authenticate(self.support_user)
        response = self.client.get(eligible_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["id"], str(payment.id))
        self.assertEqual(response.data["results"][0]["remaining_amount"], "10150.00")
        self.assertNotIn("provider_transaction_id", response.data["results"][0])
        self.assertNotIn("provider_payload", response.data["results"][0])


    def test_resolving_dispute_restores_booking_and_unfreezes_disputed_escrow(self):
        payment = EscrowPayment.objects.create(
            booking=self.case.booking,
            customer=self.customer_user,
            artisan=self.artisan,
            reference="SUPPORT-RESOLVE-DISPUTE-001",
            provider_transaction_id="flw-resolve-dispute-001",
            service_amount_kobo=1_000_000,
            customer_fee_kobo=15_000,
            total_amount_kobo=1_015_000,
            commission_rate="0.1000",
            platform_commission_kobo=100_000,
            artisan_payout_kobo=900_000,
            status=EscrowPayment.Status.DISPUTED,
            failure_reason="Original dispute reason.",
        )

        resolved = resolve_support_case(
            case=self.case,
            staff_user=self.supervisor_user,
            resolution_code=SupportCase.ResolutionCode.NO_ADJUSTMENT,
            resolution_note="The review found no adjustment is required.",
        )

        self.case.refresh_from_db()
        self.case.booking.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(resolved.status, SupportCase.Status.RESOLVED)
        self.assertEqual(self.case.booking.status, Booking.Status.IN_PROGRESS)
        self.assertIsNone(self.case.booking.finalized_at)
        self.assertEqual(payment.status, EscrowPayment.Status.HELD)
        self.assertEqual(payment.failure_reason, "")
        latest_history = self.case.booking.status_history.latest("created_at")
        self.assertEqual(latest_history.status, Booking.Status.IN_PROGRESS)
        self.assertEqual(latest_history.changed_by, "ADMIN")
        self.assertEqual(latest_history.changed_by_user_id, self.supervisor_user.id)

        with self.assertRaisesMessage(ValueError, "payments must be settled"):
            close_support_case(case=resolved, staff_user=self.supervisor_user)

    def test_closing_resolved_dispute_does_not_finalize_or_leave_booking_disputed(self):
        resolved = resolve_support_case(
            case=self.case,
            staff_user=self.supervisor_user,
            resolution_code=SupportCase.ResolutionCode.NO_ADJUSTMENT,
            resolution_note="The review found no adjustment is required.",
        )
        closed = close_support_case(case=resolved, staff_user=self.supervisor_user)

        self.case.booking.refresh_from_db()
        self.assertEqual(closed.status, SupportCase.Status.CLOSED)
        self.assertEqual(self.case.booking.status, Booking.Status.IN_PROGRESS)
        self.assertIsNone(self.case.booking.finalized_at)

    def test_dispute_cannot_be_resolved_with_an_unverified_refund_code(self):
        with self.assertRaisesMessage(ValueError, "provider-verified"):
            resolve_support_case(
                case=self.case,
                staff_user=self.supervisor_user,
                resolution_code=SupportCase.ResolutionCode.REFUND,
                resolution_note="Refund requested without provider result.",
            )
        self.case.booking.refresh_from_db()
        self.assertEqual(self.case.booking.status, Booking.Status.DISPUTED)
        self.case.refresh_from_db()
        self.assertEqual(self.case.status, SupportCase.Status.OPEN)
