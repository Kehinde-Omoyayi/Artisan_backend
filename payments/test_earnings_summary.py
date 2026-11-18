import uuid
from datetime import timedelta

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from booking.models import Booking
from home.models import ArtisanProfile, CustomUser, CustomerProfile, Locations, ServiceCategory
from services.models import ServiceRequest
from .models import EscrowPayment, EscrowRelease


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "earnings-summary-tests"}})
class ArtisanEarningsSummaryAPITests(TestCase):
    def setUp(self):
        self.customer_user = CustomUser.objects.create_user(
            email="earnings-customer@example.com",
            first_name="Earnings",
            last_name="Customer",
            password="StrongPassword123!",
            is_active=True,
        )
        self.customer = CustomerProfile.objects.create(user=self.customer_user)
        self.artisan_user = CustomUser.objects.create_user(
            email="earnings-artisan@example.com",
            first_name="Earnings",
            last_name="Artisan",
            password="StrongPassword123!",
            is_active=True,
        )
        self.artisan = ArtisanProfile.objects.create(user=self.artisan_user, profile_status="ACTIVE")
        self.other_artisan_user = CustomUser.objects.create_user(
            email="other-earnings-artisan@example.com",
            first_name="Other",
            last_name="Artisan",
            password="StrongPassword123!",
            is_active=True,
        )
        self.other_artisan = ArtisanProfile.objects.create(user=self.other_artisan_user, profile_status="ACTIVE")
        self.category = ServiceCategory.objects.create(name="Electrical")
        self.location = Locations.objects.create(country="Nigeria", state="Lagos", area="Yaba")
        self.api = APIClient()
        self.api.force_authenticate(self.artisan_user)

    def _create_release(self, *, artisan, amount_kobo, status, completed_at=None):
        service_request = ServiceRequest.objects.create(
            requester=self.customer_user,
            category=self.category,
            location=self.location,
            preferred_date=timezone.localdate(),
            description="Electrical repair for payout-summary test.",
        )
        now = timezone.now()
        booking = Booking.objects.create(
            request=service_request,
            artisan=artisan,
            customer=self.customer,
            scheduled_time_start=now,
            scheduled_time_end=now + timedelta(hours=1),
            price_agreed="30000.00",
            status=Booking.Status.COMPLETED,
        )
        commission_kobo = 5_000
        service_amount_kobo = amount_kobo + commission_kobo
        payment = EscrowPayment.objects.create(
            booking=booking,
            customer=self.customer_user,
            artisan=artisan,
            reference=f"EARN-{uuid.uuid4().hex}",
            service_amount_kobo=service_amount_kobo,
            customer_fee_kobo=15_000,
            total_amount_kobo=service_amount_kobo + 15_000,
            platform_commission_kobo=commission_kobo,
            artisan_payout_kobo=amount_kobo,
            status=(EscrowPayment.Status.RELEASED if status == EscrowRelease.Status.SUCCESSFUL else EscrowPayment.Status.RELEASE_PENDING),
        )
        return EscrowRelease.objects.create(
            payment=payment,
            reference=f"EARN-RELEASE-{uuid.uuid4().hex}",
            amount_kobo=amount_kobo,
            status=status,
            completed_at=completed_at,
        )

    def test_summary_counts_only_successful_payouts_belonging_to_current_artisan(self):
        now = timezone.now()
        current_month = timezone.localtime(now)
        previous_month = current_month.replace(day=1, hour=12, minute=0, second=0, microsecond=0) - timedelta(days=1)

        self._create_release(
            artisan=self.artisan,
            amount_kobo=1_234_567,
            status=EscrowRelease.Status.SUCCESSFUL,
            completed_at=now,
        )
        self._create_release(
            artisan=self.artisan,
            amount_kobo=25_000,
            status=EscrowRelease.Status.SUCCESSFUL,
            completed_at=now,
        )
        self._create_release(
            artisan=self.artisan,
            amount_kobo=765_432,
            status=EscrowRelease.Status.SUCCESSFUL,
            completed_at=previous_month,
        )
        self._create_release(
            artisan=self.artisan,
            amount_kobo=888_000,
            status=EscrowRelease.Status.PENDING,
        )
        self._create_release(
            artisan=self.artisan,
            amount_kobo=999_000,
            status=EscrowRelease.Status.FAILED,
        )
        self._create_release(
            artisan=self.other_artisan,
            amount_kobo=500_000,
            status=EscrowRelease.Status.SUCCESSFUL,
            completed_at=now,
        )

        response = self.api.get(reverse("artisan-earnings-summary"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["currency"], "NGN")
        self.assertEqual(response.data["this_month"]["amount_kobo"], 1_259_567)
        self.assertEqual(response.data["this_month"]["payout_count"], 2)
        self.assertEqual(response.data["lifetime"]["amount_kobo"], 2_024_999)
        self.assertEqual(response.data["lifetime"]["payout_count"], 3)

    def test_customer_without_artisan_profile_cannot_read_earnings_summary(self):
        self.api.force_authenticate(self.customer_user)

        response = self.api.get(reverse("artisan-earnings-summary"))

        self.assertEqual(response.status_code, 403)
