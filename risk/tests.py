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

    def make_payment(self, booking, *, status="PAYMENT_PENDING"):
        """A payment row in the state the customer can still cancel."""
        from decimal import Decimal

        from payments.models import EscrowPayment

        service_kobo = int(Decimal("25000.00") * 100)
        fee_kobo = 15000
        commission = int(service_kobo * Decimal("0.1000"))
        return EscrowPayment.objects.create(
            booking=booking,
            customer=self.user,
            artisan=booking.artisan,
            reference=f"PAY-{EscrowPayment.objects.count()}-{booking.pk}",
            service_amount_kobo=service_kobo,
            customer_fee_kobo=fee_kobo,
            total_amount_kobo=service_kobo + fee_kobo,
            platform_commission_kobo=commission,
            artisan_payout_kobo=service_kobo - commission,
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

    def test_booking_cancellation_is_history_and_adds_no_risk_point(self):
        booking = self.make_booking(hours_until_start=48)
        self.client.force_authenticate(self.user)
        response = self.client.post(
            f"/api/v1/booking/{booking.id}/cancel/",
            {"reason": "Changed my plans", "reason_code": "CUSTOMER_CHANGED_MIND"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="risk-booking-cancel",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["cancellation_risk"]["score"], 0)
        self.assertFalse(response.data["cancellation_risk"]["account_suspended"])

        from .services import get_customer_risk_profile

        profile = get_customer_risk_profile(self.customer)
        self.assertEqual(profile.cancellation_points, 0)
        self.assertEqual(profile.event_points, 0)
        self.assertEqual(profile.risk_level, "NORMAL")
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)

    def test_customer_payment_cancellation_adds_exactly_one_point(self):
        booking = self.make_booking(hours_until_start=48)
        payment = self.make_payment(booking)
        self.client.force_authenticate(self.user)
        response = self.client.post(
            f"/api/v1/payments/{payment.id}/cancel/",
            {"reason": "I need to move the date."},
            format="json",
            HTTP_IDEMPOTENCY_KEY="risk-payment-cancel",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["cancellation_risk"]["score"], 1)
        self.assertEqual(response.data["cancellation_risk"]["percentage"], 20)
        self.assertFalse(response.data["cancellation_risk"]["account_suspended"])

        from .models import CustomerPaymentCancellationEvent
        from .services import get_customer_risk_profile

        event = CustomerPaymentCancellationEvent.objects.get(payment=payment)
        self.assertEqual(event.points, 1)
        self.assertEqual(event.initiated_by, "CUSTOMER")
        self.assertEqual(event.payment_reference, payment.reference)
        profile = get_customer_risk_profile(self.customer)
        self.assertEqual(profile.cancellation_points, 1)
        self.assertEqual(profile.event_points, 1)
        self.assertEqual(profile.cancellation_count, 1)

    def test_repeated_requests_never_duplicate_the_point(self):
        booking = self.make_booking(hours_until_start=48)
        payment = self.make_payment(booking)
        self.client.force_authenticate(self.user)
        first = self.client.post(
            f"/api/v1/payments/{payment.id}/cancel/",
            {"reason": "I need to move the date."},
            format="json",
            HTTP_IDEMPOTENCY_KEY="risk-payment-cancel-a",
        )
        replay = self.client.post(
            f"/api/v1/payments/{payment.id}/cancel/",
            {"reason": "I need to move the date."},
            format="json",
            HTTP_IDEMPOTENCY_KEY="risk-payment-cancel-a",
        )
        other = self.client.post(
            f"/api/v1/payments/{payment.id}/cancel/",
            {"reason": "I need to move the date."},
            format="json",
            HTTP_IDEMPOTENCY_KEY="risk-payment-cancel-b",
        )
        self.assertEqual(first.status_code, 200, first.data)
        self.assertIn(replay.status_code, (200, 201))
        self.assertEqual(other.status_code, 400)

        from .models import CustomerPaymentCancellationEvent
        from .services import get_customer_risk_profile

        self.assertEqual(
            CustomerPaymentCancellationEvent.objects.filter(payment=payment).count(), 1
        )
        self.assertEqual(get_customer_risk_profile(self.customer).cancellation_points, 1)

    def test_non_customer_payment_cancellations_are_never_scored(self):
        from .services import get_customer_risk_profile, record_customer_payment_cancellation

        booking = self.make_booking(hours_until_start=48)
        payment = self.make_payment(booking)
        record_customer_payment_cancellation(
            payment=payment, initiated_by="PROVIDER", reason_text="Provider expired the link."
        )
        self.assertEqual(get_customer_risk_profile(self.customer).cancellation_points, 0)

        payment_two = self.make_payment(self.make_booking(hours_until_start=48))
        record_customer_payment_cancellation(
            payment=payment_two, initiated_by="ADMIN", reason_text="Closed by support."
        )
        self.assertEqual(get_customer_risk_profile(self.customer).cancellation_points, 0)
        self.assertEqual(
            get_customer_risk_profile(self.customer).cancellation_count, 2
        )

    def test_five_points_never_blocks_the_account(self):
        from .services import get_customer_risk_profile

        for idx in range(5):
            payment = self.make_payment(self.make_booking(hours_until_start=48))
            self.client.force_authenticate(self.user)
            response = self.client.post(
                f"/api/v1/payments/{payment.id}/cancel/",
                {"reason": "Could not complete the payment."},
                format="json",
                HTTP_IDEMPOTENCY_KEY=f"risk-payment-cancel-{idx}",
            )
            self.assertEqual(response.status_code, 200, response.data)

        profile = get_customer_risk_profile(self.customer)
        self.assertEqual(profile.cancellation_points, 5)
        self.assertEqual(profile.event_points, 5)
        self.assertEqual(profile.risk_level, "REVIEW")
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)
        self.assertEqual(self.user.security_status, "NORMAL")
        self.assertFalse(CustomerNetworkBlock.objects.exists())

    def test_held_payment_must_use_the_refund_process(self):
        booking = self.make_booking(hours_until_start=48)
        payment = self.make_payment(booking, status="HELD")
        self.client.force_authenticate(self.user)
        response = self.client.post(
            f"/api/v1/payments/{payment.id}/cancel/",
            {"reason": "Change of mind."},
            format="json",
            HTTP_IDEMPOTENCY_KEY="risk-payment-cancel-held",
        )
        self.assertEqual(response.status_code, 400)
        payment.refresh_from_db()
        self.assertEqual(payment.status, "HELD")

    def test_only_the_payer_can_cancel_a_payment(self):
        booking = self.make_booking(hours_until_start=48)
        payment = self.make_payment(booking)
        stranger = CustomUser.objects.create_user(
            email="stranger-risk@example.com",
            first_name="Stranger",
            last_name="Risk",
            password="StrongPassword123!",
            is_active=True,
        )
        self.client.force_authenticate(stranger)
        response = self.client.post(
            f"/api/v1/payments/{payment.id}/cancel/",
            {"reason": "Not mine."},
            format="json",
            HTTP_IDEMPOTENCY_KEY="risk-payment-cancel-stranger",
        )
        self.assertEqual(response.status_code, 403)
        payment.refresh_from_db()
        self.assertEqual(payment.status, "PAYMENT_PENDING")


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
