from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework.request import Request
from django.test.client import RequestFactory

from booking.models import Booking
from home.models import CustomUser, CustomerProfile
from .models import CustomerCancellationEvent, CustomerNetworkBlock
from .network import hash_ip
from .policy import calculate_cancellation_points
from .services import record_customer_cancellation


class CustomerCancellationRiskTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = CustomUser.objects.create_user(
            email="customer-risk@example.com",
            first_name="Risk",
            last_name="Customer",
            password="StrongPassword123!",
            is_active=True,
        )
        self.customer = CustomerProfile.objects.create(user=self.user)

    def make_booking(self, *, hours_until_start=48, status=Booking.Status.ACCEPTED):
        from services.models import ServiceRequest
        from home.models import ServiceCategory, Locations
        from home.models import ArtisanProfile

        artisan_user = CustomUser.objects.create_user(
            email=f"artisan-{CustomUser.objects.count()}@example.com",
            first_name="Artisan",
            last_name="Test",
            password="StrongPassword123!",
            is_active=True,
        )
        artisan = ArtisanProfile.objects.create(user=artisan_user, profile_status="ACTIVE")
        category = ServiceCategory.objects.create(name=f"Category {CustomUser.objects.count()}")
        location = Locations.objects.create(country="Nigeria", state="Ogun", area="Test Area")
        request = ServiceRequest.objects.create(
            requester=self.user,
            category=category,
            location=location,
            preferred_date=(timezone.now() + timedelta(days=2)).date(),
            description="Test request",
        )
        return Booking.objects.create(
            request=request,
            artisan=artisan,
            customer=self.customer,
            scheduled_time_start=timezone.now() + timedelta(hours=hours_until_start),
            scheduled_time_end=timezone.now() + timedelta(hours=hours_until_start + 2),
            price_agreed="10000.00",
            status=status,
        )

    def test_reason_severity_increases_for_accepted_and_last_minute_booking(self):
        booking = self.make_booking(hours_until_start=1)
        points = calculate_cancellation_points(
            booking=booking,
            reason_code="CUSTOMER_CHANGED_MIND",
            recent_customer_cancellations=0,
        )
        self.assertEqual(points, 4)

    def test_legitimate_artisan_or_safety_reason_has_zero_points(self):
        booking = self.make_booking(hours_until_start=1)
        points = calculate_cancellation_points(
            booking=booking,
            reason_code="SAFETY_CONCERN",
            recent_customer_cancellations=5,
        )
        self.assertEqual(points, 0)

    def test_five_points_suspends_account_and_restricts_observed_ip(self):
        for idx in range(3):
            booking = self.make_booking(hours_until_start=48)
            self.client.force_authenticate(self.user)
            response = self.client.post(
                f"/api/v1/booking/{booking.id}/cancel/",
                {
                    "reason": "Changed my plans",
                    "reason_code": "CUSTOMER_CHANGED_MIND",
                },
                format="json",
                REMOTE_ADDR="203.0.113.10",
                HTTP_IDEMPOTENCY_KEY=f"risk-cancel-{idx}",
            )
            self.assertEqual(response.status_code, 200)

        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)
        self.assertEqual(self.user.security_status, "LOCKED")
        self.assertTrue(
            CustomerNetworkBlock.objects.filter(
                ip_hash=hash_ip("203.0.113.10"),
                blocked_until__gt=timezone.now(),
            ).exists()
        )


    def test_normal_customer_cancellation_cannot_singlehandedly_reach_5_5(self):
        booking = self.make_booking(hours_until_start=1)
        points = calculate_cancellation_points(
            booking=booking,
            reason_code="CUSTOMER_CHANGED_MIND",
            recent_customer_cancellations=0,
        )
        self.assertEqual(points, 4)

    def test_weighted_rate_reflects_event_severity_against_terminal_bookings(self):
        booking = self.make_booking(hours_until_start=48)
        # The cancel view moves the booking to CANCELLED before the risk event is recorded.
        booking.status = Booking.Status.CANCELLED
        booking.save(update_fields=["status", "updated_at"])
        request = RequestFactory().post(
            f"/api/v1/booking/{booking.id}/cancel/",
            REMOTE_ADDR="203.0.113.21",
        )
        request = Request(request)
        record_customer_cancellation(
            booking=booking,
            reason_code="CUSTOMER_CHANGED_MIND",
            reason_text="Changed my plans",
            request=request,
            previous_status=Booking.Status.ACCEPTED,
        )
        from .services import get_customer_risk_profile
        profile = get_customer_risk_profile(self.customer)
        # At 48h before an ACCEPTED booking the event is worth 2/5. With one
        # terminal booking, the weighted cancellation rate is therefore 40%.
        self.assertEqual(float(profile.weighted_cancellation_rate), 40.0)
    def test_cancellation_rate_is_weighted_by_severity(self):
        booking = self.make_booking(hours_until_start=48)
        request = RequestFactory().post(
            f"/api/v1/booking/{booking.id}/cancel/",
            REMOTE_ADDR="203.0.113.20",
        )
        request = Request(request)
        record_customer_cancellation(
            booking=booking,
            reason_code="CUSTOMER_CHANGED_MIND",
            reason_text="Changed my plans",
            request=request,
        )
        event = CustomerCancellationEvent.objects.get(booking=booking)
        self.assertEqual(event.points, 2)


class CustomerRiskExposureTests(TestCase):
    def setUp(self):
        self.user = CustomUser.objects.create_user(
            email="profile-risk@example.com",
            first_name="Profile",
            last_name="Risk",
            password="StrongPassword123!",
            is_active=True,
        )
        self.customer = CustomerProfile.objects.create(user=self.user)

    def test_customer_profile_exposes_cancellation_risk_fields(self):
        from profiles.serializers import CustomerProfileSerializer

        data = CustomerProfileSerializer(self.customer).data
        self.assertEqual(data["cancellation_risk_score"], 0)
        self.assertEqual(data["cancellation_risk_max"], 5)
        self.assertEqual(data["cancellation_rate"], 0.0)
        self.assertEqual(data["cancellation_risk_level"], "NORMAL")

    def test_shared_network_is_not_automatically_blocked_when_over_limit(self):
        from home.models import CustomerProfile
        from .network import record_customer_network_observation
        from django.test.client import RequestFactory
        from rest_framework.request import Request
        from .services import _apply_network_blocks

        ip = "203.0.113.90"
        factory = RequestFactory()

        for customer in [self.customer]:
            req = Request(factory.get("/api/v1/booking/", REMOTE_ADDR=ip))
            record_customer_network_observation(customer, req, observation_type="CANCELLATION")
            record_customer_network_observation(customer, req, observation_type="CANCELLATION")

        for idx in range(3):
            user = CustomUser.objects.create_user(
                email=f"shared-{idx}@example.com",
                first_name="Shared",
                last_name="Network",
                password="StrongPassword123!",
                is_active=True,
            )
            customer = CustomerProfile.objects.create(user=user)
            req = Request(factory.get("/api/v1/booking/", REMOTE_ADDR=ip))
            record_customer_network_observation(customer, req, observation_type="CANCELLATION")

        _apply_network_blocks(self.customer, reason="test", suspension_score=5)
        self.assertFalse(CustomerNetworkBlock.objects.filter(ip_hash=hash_ip(ip)).exists())


class NetworkRiskBoundaryTests(TestCase):
    def test_forwarded_ip_is_ignored_unless_proxy_is_trusted(self):
        from django.test.client import RequestFactory
        from .network import get_client_ip
        factory = RequestFactory()
        request = factory.get(
            "/api/v1/login/",
            REMOTE_ADDR="198.51.100.10",
            HTTP_X_FORWARDED_FOR="203.0.113.50",
        )
        self.assertEqual(get_client_ip(request), "198.51.100.10")

    def test_forwarded_ip_is_used_when_proxy_is_trusted(self):
        from django.test.client import RequestFactory
        from django.test import override_settings
        from .network import get_client_ip
        factory = RequestFactory()
        with override_settings(TRUSTED_PROXY_IPS=["198.51.100.0/24"]):
            request = factory.get(
                "/api/v1/login/",
                REMOTE_ADDR="198.51.100.10",
                HTTP_X_FORWARDED_FOR="203.0.113.50, 198.51.100.20",
            )
            self.assertEqual(get_client_ip(request), "203.0.113.50")

    def test_leftmost_forwarded_entry_is_never_trusted(self):
        from django.test.client import RequestFactory
        from django.test import override_settings
        from .network import get_client_ip
        factory = RequestFactory()
        # An attacker prepends a victim's address; the proxy appends the attacker's real address.
        with override_settings(TRUSTED_PROXY_IPS=[], TRUSTED_PROXY_COUNT=1):
            request = factory.get("/", REMOTE_ADDR="10.0.0.5", HTTP_X_FORWARDED_FOR="198.51.100.99, 203.0.113.77")
            self.assertEqual(get_client_ip(request), "203.0.113.77")
        with override_settings(TRUSTED_PROXY_IPS=["10.0.0.0/8"], TRUSTED_PROXY_COUNT=0):
            request = factory.get("/", REMOTE_ADDR="10.0.0.5", HTTP_X_FORWARDED_FOR="198.51.100.99, 203.0.113.77, 10.0.0.9")
            self.assertEqual(get_client_ip(request), "203.0.113.77")

    def test_forwarded_header_is_ignored_without_any_trusted_proxy_configuration(self):
        from django.test.client import RequestFactory
        from django.test import override_settings
        from .network import get_client_ip
        with override_settings(TRUSTED_PROXY_IPS=[], TRUSTED_PROXY_COUNT=0):
            request = RequestFactory().get("/", REMOTE_ADDR="203.0.113.5", HTTP_X_FORWARDED_FOR="198.51.100.99")
            self.assertEqual(get_client_ip(request), "203.0.113.5")

    def test_malformed_forwarded_entries_fall_back_safely(self):
        from django.test.client import RequestFactory
        from django.test import override_settings
        from .network import get_client_ip
        with override_settings(TRUSTED_PROXY_IPS=[], TRUSTED_PROXY_COUNT=1):
            request = RequestFactory().get("/", REMOTE_ADDR="10.0.0.5", HTTP_X_FORWARDED_FOR="not-an-ip, <script>")
            self.assertEqual(get_client_ip(request), "10.0.0.5")
