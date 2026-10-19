from datetime import timedelta
from unittest.mock import patch

from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from booking.models import Booking
from conversations.models import Conversation, Message
from home.models import (
    ArtisanProfile,
    CustomerProfile,
    CustomUser,
    Locations,
    Service,
    ServiceCategory,
)
from payments.models import (
    EscrowPayment,
    EscrowRelease,
    PaymentLedgerEntry,
    PaymentLedgerLine,
)
from risk.models import CustomerRiskProfile
from services.models import ArtisanVerification, ServiceRequest
from support.models import SupportCase, SupportFinancialAction
from django.test import TestCase, override_settings


ANALYTICS_TEST_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "manager-analytics-tests",
    }
}


@override_settings(CACHES=ANALYTICS_TEST_CACHES)
class ManagerOverviewPermissionTests(TestCase):
    def setUp(self):
        self.url = reverse("manager-overview")
        self.client = APIClient()

    def test_only_active_superusers_can_read_site_wide_manager_analytics(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 401)

        ordinary_user = CustomUser.objects.create_user(
            email="member@example.test",
            first_name="Mira",
            last_name="Member",
            password="SafePass!483",
            is_active=True,
        )
        self.client.force_authenticate(user=ordinary_user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403)

        support_staff = CustomUser.objects.create_user(
            email="support@example.test",
            first_name="Sam",
            last_name="Support",
            password="SafePass!483",
            is_active=True,
            is_staff=True,
        )
        self.client.force_authenticate(user=support_staff)
        self.assertEqual(self.client.get(self.url).status_code, 403)

        manager = CustomUser.objects.create_superuser(
            email="owner@example.test",
            first_name="Ayo",
            last_name="Manager",
            password="SafePass!483",
        )
        self.client.force_authenticate(user=manager)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("kpis", response.data)

    def test_inactive_superuser_is_denied(self):
        manager = CustomUser.objects.create_superuser(
            email="inactive@example.test",
            first_name="Ada",
            last_name="Owner",
            password="SafePass!483",
        )
        manager.is_active = False
        manager.save(update_fields=["is_active"])
        self.client.force_authenticate(user=manager)
        self.assertEqual(self.client.get(self.url).status_code, 403)


@override_settings(CACHES=ANALYTICS_TEST_CACHES)
class ManagerOverviewAggregateTests(TestCase):
    def setUp(self):
        now = timezone.now()
        self.now = now
        self.manager = CustomUser.objects.create_superuser(
            email="platform@example.test",
            first_name="Tola",
            last_name="Platform",
            password="SafePass!483",
        )
        customer_user = CustomUser.objects.create_user(
            email="customer@example.test",
            first_name="Nia",
            last_name="Customer",
            password="SafePass!483",
            is_active=True,
        )
        self.customer = CustomerProfile.objects.create(user=customer_user)
        artisan_user = CustomUser.objects.create_user(
            email="artisan@example.test",
            first_name="Kemi",
            last_name="Artisan",
            password="SafePass!483",
            is_active=True,
        )
        self.artisan = ArtisanProfile.objects.create(
            user=artisan_user,
            business_name="Kemi Repairs",
            profile_status="ACTIVE",
        )
        self.verification = ArtisanVerification.objects.create(
            artisan=self.artisan,
            verification_status="PENDING",
        )
        self.category = ServiceCategory.objects.create(name="Electrical")
        self.service = Service.objects.create(
            category=self.category,
            name="Home wiring",
            is_active=True,
        )
        location = Locations.objects.create(country="Nigeria", state="Lagos", area="Yaba")
        self.request = ServiceRequest.objects.create(
            requester=customer_user,
            category=self.category,
            service=self.service,
            location=location,
            preferred_date=(now + timedelta(days=2)).date(),
            urgency="EMERGENCY",
            description="Repair a damaged wire.",
        )
        self.booking = Booking.objects.create(
            request=self.request,
            artisan=self.artisan,
            customer=self.customer,
            scheduled_time_start=now + timedelta(days=2),
            scheduled_time_end=now + timedelta(days=2, hours=3),
            price_agreed="20000.00",
            status=Booking.Status.DISPUTED,
        )
        self.payment = EscrowPayment.objects.create(
            booking=self.booking,
            customer=customer_user,
            artisan=self.artisan,
            reference="MANAGER-ANALYTICS-PAYMENT",
            service_amount_kobo=2_000_000,
            customer_fee_kobo=15_000,
            total_amount_kobo=2_015_000,
            platform_commission_kobo=200_000,
            artisan_payout_kobo=1_800_000,
            status=EscrowPayment.Status.HELD,
            held_at=now,
            verified_at=now,
            provider_payload={"private_marker": "RAW_PROVIDER_SENTINEL"},
        )
        ledger_entry = PaymentLedgerEntry.objects.create(
            payment=self.payment,
            event_key="manager-analytics:escrow-held",
            event_type="ESCROW_HELD",
            status_before=EscrowPayment.Status.PAYMENT_PENDING,
            status_after=EscrowPayment.Status.HELD,
            amount_kobo=self.payment.total_amount_kobo,
        )
        PaymentLedgerLine.objects.create(
            entry=ledger_entry,
            account="ESCROW_FUNDS",
            direction=PaymentLedgerLine.Direction.DEBIT,
            amount_kobo=2_015_000,
        )
        PaymentLedgerLine.objects.create(
            entry=ledger_entry,
            account="ARTISAN_PAYABLE",
            direction=PaymentLedgerLine.Direction.CREDIT,
            amount_kobo=1_800_000,
        )
        PaymentLedgerLine.objects.create(
            entry=ledger_entry,
            account="PLATFORM_COMMISSION_REVENUE",
            direction=PaymentLedgerLine.Direction.CREDIT,
            amount_kobo=200_000,
        )
        PaymentLedgerLine.objects.create(
            entry=ledger_entry,
            account="PLATFORM_FEE_REVENUE",
            direction=PaymentLedgerLine.Direction.CREDIT,
            amount_kobo=15_000,
        )
        EscrowRelease.objects.create(
            payment=self.payment,
            reference="MANAGER-ANALYTICS-PAYOUT",
            amount_kobo=1_800_000,
            status=EscrowRelease.Status.SUCCESSFUL,
            completed_at=now,
        )
        case = SupportCase.objects.create(
            booking=self.booking,
            opened_by=customer_user,
            case_type=SupportCase.CaseType.DISPUTE,
            status=SupportCase.Status.OPEN,
            priority=SupportCase.Priority.URGENT,
            subject="Please review this booking",
            description="The customer needs support with the booking.",
        )
        SupportFinancialAction.objects.create(
            case=case,
            payment=self.payment,
            action_type=SupportFinancialAction.ActionType.PARTIAL_REFUND,
            amount_kobo=250_000,
            reason="Partial refund after case review.",
            idempotency_key="manager-analytics:refund",
            status=SupportFinancialAction.Status.SUCCEEDED,
            requested_by=self.manager,
            completed_at=now,
        )
        CustomerRiskProfile.objects.create(
            customer=self.customer,
            risk_level="REVIEW",
        )
        conversation = Conversation.objects.create(
            conversation_type=Conversation.ConversationType.SERVICE,
            customer=customer_user,
            artisan=self.artisan,
            service_request=self.request,
        )
        Message.objects.create(
            conversation=conversation,
            sender=customer_user,
            body="Please confirm the appointment time.",
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.manager)

    def test_overview_returns_comprehensive_aggregate_analytics_and_no_provider_payloads(self):
        with patch("home.manager_analytics.timezone.now", return_value=self.now):
            response = self.client.get(reverse("manager-overview"))

        self.assertEqual(response.status_code, 200)
        data = response.data
        kpis = data["kpis"]
        self.assertEqual(kpis["customers_total"], 1)
        self.assertEqual(kpis["customers_active"], 1)
        self.assertEqual(kpis["artisans_active"], 1)
        self.assertEqual(kpis["artisans_pending_verification"], 1)
        self.assertEqual(kpis["services_active"], 1)
        self.assertEqual(kpis["service_requests_30d"], 1)
        self.assertEqual(kpis["emergency_requests_30d"], 1)
        self.assertEqual(kpis["bookings_total"], 1)
        self.assertEqual(kpis["bookings_active"], 1)
        self.assertEqual(kpis["booking_conversion_rate_30d"], 100.0)
        self.assertEqual(kpis["paid_volume_30d_kobo"], 2_000_000)
        self.assertEqual(kpis["customer_collections_30d_kobo"], 2_015_000)
        self.assertEqual(kpis["platform_fees_30d_kobo"], 215_000)
        self.assertEqual(kpis["payouts_30d_kobo"], 1_800_000)
        self.assertEqual(kpis["refunds_30d_kobo"], 250_000)
        self.assertEqual(kpis["active_escrow_kobo"], 2_015_000)
        self.assertEqual(kpis["support_cases_urgent"], 1)
        self.assertEqual(kpis["support_disputes_open"], 1)
        self.assertEqual(kpis["risk_customers_flagged"], 1)
        self.assertEqual(kpis["messages_30d"], 1)
        self.assertEqual(data["top_categories"][0]["name"], "Electrical")
        self.assertEqual(data["top_regions"][0]["name"], "Lagos")
        self.assertEqual(len(data["activity_trend"]), 14)
        self.assertIn("service value", data["financial_note"])
        self.assertNotIn("RAW_PROVIDER_SENTINEL", str(data))
        self.assertNotIn("provider_payload", str(data))
        self.assertNotIn("provider_reference", str(data))
