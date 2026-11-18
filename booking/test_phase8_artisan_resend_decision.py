"""Phase 8 — the artisan's ACCEPT / REJECT decision belongs to the current version.

A rejection closes the decision on *that* version only. When the agreement is
sent again it becomes a brand new version awaiting a fresh decision, and the
artisan must be able to accept or reject it exactly as if it had just arrived.
The rejected version keeps its status, its reason and its history.
"""

from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework.throttling import SimpleRateThrottle

from home.models import (
    ArtisanProfile,
    ArtisanService,
    CustomUser,
    CustomerProfile,
    Locations,
    Service,
    ServiceCategory,
)
from services.models import ArtisanVerification, ServiceRequest

from .models import Booking, BookingStatusHistory


class ArtisanResendDecisionTests(TestCase):
    _TEST_THROTTLE_RATES = {
        "anon": "10000/hour",
        "user": "10000/hour",
        "booking_create": "1000/hour",
        "booking_accept": "1000/hour",
        "booking_reject": "1000/hour",
        "booking_cancel": "1000/hour",
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
        self.customer_user = CustomUser.objects.create_user(
            email="p8-customer@example.com",
            first_name="Temi",
            last_name="Customer",
            password="Passw0rd!",
            is_active=True,
            email_verified=True,
        )
        self.customer = CustomerProfile.objects.create(
            user=self.customer_user, country="Nigeria", state="Lagos", area="Ikeja"
        )
        self.artisan_user = CustomUser.objects.create_user(
            email="p8-artisan@example.com",
            first_name="Bayo",
            last_name="Artisan",
            password="Passw0rd!",
            is_active=True,
            email_verified=True,
        )
        self.artisan = ArtisanProfile.objects.create(
            user=self.artisan_user,
            business_name="Bayo Repairs",
            profile_status="ACTIVE",
            country="Nigeria",
            state="Lagos",
            area="Ikeja",
        )
        ArtisanVerification.objects.create(
            artisan=self.artisan,
            verification_status="VERIFIED",
            verification_method="NIN",
        )
        self.location = Locations.objects.create(
            country="Nigeria", state="Lagos", area="Ikeja"
        )
        self.category = ServiceCategory.objects.create(name="Plumbing")
        self.service = Service.objects.create(
            category=self.category, name="Pipe fitting", description=""
        )
        ArtisanService.objects.create(
            artisan=self.artisan, service=self.service, location=self.location
        )
        self.service_request = ServiceRequest.objects.create(
            requester=self.customer_user,
            category=self.category,
            location=self.location,
            description="The kitchen pipe leaks at the joint under the sink.",
            status=ServiceRequest.Status.OPEN,
            preferred_date=timezone.localdate() + timedelta(days=3),
        )
        self.booking = self.make_booking()

    def make_booking(self, **overrides):
        start = timezone.now() + timedelta(days=2)
        defaults = dict(
            request=self.service_request,
            artisan=self.artisan,
            customer=self.customer,
            scheduled_time_start=start,
            scheduled_time_end=start + timedelta(hours=3),
            price_agreed=Decimal("25000.00"),
            status=Booking.Status.REQUESTED,
        )
        defaults.update(overrides)
        return Booking.objects.create(**defaults)

    def auth(self, user):
        self.client.force_authenticate(user=user)

    def url(self, name, booking=None):
        return reverse(name, args=[(booking or self.booking).pk])

    def actions_for(self, user, booking=None):
        booking = booking or self.booking
        self.auth(user)
        response = self.client.get(self.url("booking-detail", booking))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return [item["code"] for item in response.data.get("available_actions") or []]

    # ------------------------------------------------------------ 1–2: rejection

    def test_a_requested_booking_offers_the_artisan_accept_and_reject(self):
        actions = self.actions_for(self.artisan_user)
        self.assertIn("ACCEPT_BOOKING", actions)
        self.assertIn("REJECT_BOOKING", actions)

    def test_the_artisan_can_reject_a_requested_booking(self):
        self.auth(self.artisan_user)
        response = self.client.post(
            self.url("booking-reject"),
            {"reason": "I cannot take this date."},
            format="json",
            HTTP_IDEMPOTENCY_KEY="p8-reject-1",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, Booking.Status.REJECTED)
        self.assertIsNotNone(self.booking.finalized_at)

    def test_a_rejected_booking_hides_the_decision_but_offers_a_resend(self):
        self.auth(self.artisan_user)
        self.client.post(
            self.url("booking-reject"),
            {"reason": "I cannot take this date."},
            format="json",
            HTTP_IDEMPOTENCY_KEY="p8-reject-2",
        )

        actions = self.actions_for(self.artisan_user)
        self.assertNotIn("ACCEPT_BOOKING", actions)
        self.assertNotIn("REJECT_BOOKING", actions)
        self.assertIn("RESEND_BOOKING", actions)

    # ------------------------------------------------------------ 3: resend

    def test_the_artisan_can_send_a_rejected_booking_again_as_a_new_version(self):
        self.auth(self.artisan_user)
        self.client.post(
            self.url("booking-reject"),
            {"reason": "I cannot take this date."},
            format="json",
            HTTP_IDEMPOTENCY_KEY="p8-reject-3",
        )

        response = self.client.post(
            self.url("booking-resend"),
            {"reason": "New date agreed with the customer."},
            format="json",
            HTTP_IDEMPOTENCY_KEY="p8-resend-1",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        version = Booking.objects.get(pk=response.data["id"])
        self.assertNotEqual(version.pk, self.booking.pk)
        self.assertEqual(version.status, Booking.Status.REQUESTED)
        self.assertEqual(version.resend_count, 1)
        self.assertEqual(version.resend_of_id, self.booking.pk)
        self.assertEqual(version.resent_by_id, self.artisan_user.id)
        self.assertEqual(version.price_agreed, Decimal("25000.00"))
        self.assertGreater(version.scheduled_time_start, timezone.now())

    def test_a_resend_never_reopens_the_rejected_version(self):
        self.auth(self.artisan_user)
        self.client.post(
            self.url("booking-reject"),
            {"reason": "I cannot take this date."},
            format="json",
            HTTP_IDEMPOTENCY_KEY="p8-reject-4",
        )
        self.booking.refresh_from_db()
        rejected_at = self.booking.finalized_at
        self.assertIsNotNone(rejected_at)

        response = self.client.post(
            self.url("booking-resend"), {}, format="json", HTTP_IDEMPOTENCY_KEY="p8-resend-2"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, Booking.Status.REJECTED)
        self.assertEqual(self.booking.finalized_at, rejected_at)
        self.assertEqual(Booking.objects.filter(pk=self.booking.pk).count(), 1)

    def test_the_customer_cannot_resend_the_booking(self):
        self.auth(self.artisan_user)
        self.client.post(
            self.url("booking-reject"),
            {"reason": "Unavailable."},
            format="json",
            HTTP_IDEMPOTENCY_KEY="p8-reject-5",
        )
        self.auth(self.customer_user)
        response = self.client.post(
            self.url("booking-resend"), {}, format="json", HTTP_IDEMPOTENCY_KEY="p8-resend-3"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_booking_that_is_still_awaiting_a_decision_cannot_be_resent(self):
        self.auth(self.artisan_user)
        response = self.client.post(
            self.url("booking-resend"), {}, format="json", HTTP_IDEMPOTENCY_KEY="p8-resend-4"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_an_already_resent_booking_is_not_resent_twice(self):
        self.auth(self.artisan_user)
        self.client.post(
            self.url("booking-reject"),
            {"reason": "Unavailable."},
            format="json",
            HTTP_IDEMPOTENCY_KEY="p8-reject-6",
        )
        first = self.client.post(
            self.url("booking-resend"), {}, format="json", HTTP_IDEMPOTENCY_KEY="p8-resend-5"
        )
        self.assertEqual(first.status_code, status.HTTP_201_CREATED, first.data)

        second = self.client.post(
            self.url("booking-resend"), {}, format="json", HTTP_IDEMPOTENCY_KEY="p8-resend-6"
        )
        self.assertEqual(second.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Booking.objects.filter(request=self.service_request).count(), 2)

    # ------------------------------------------------- 4–6: decide the new version

    def test_the_resent_booking_offers_the_artisan_accept_and_reject_again(self):
        version = self.resend()
        actions = self.actions_for(self.artisan_user, booking=version)
        self.assertIn("ACCEPT_BOOKING", actions)
        self.assertIn("REJECT_BOOKING", actions)

    def test_the_artisan_can_accept_a_resent_booking(self):
        version = self.resend()
        self.auth(self.artisan_user)

        response = self.client.post(
            self.url("booking-accept", version),
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY="p8-accept-1",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        version.refresh_from_db()
        self.assertEqual(version.status, Booking.Status.ACCEPTED)
        self.assertEqual(version.resend_count, 1)
        # The previous rejection is untouched.
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, Booking.Status.REJECTED)

    def test_the_artisan_can_reject_a_resent_booking(self):
        version = self.resend()
        self.auth(self.artisan_user)

        response = self.client.post(
            self.url("booking-reject", version),
            {"reason": "Still not available."},
            format="json",
            HTTP_IDEMPOTENCY_KEY="p8-reject-7",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        version.refresh_from_db()
        self.assertEqual(version.status, Booking.Status.REJECTED)
        self.assertIsNotNone(version.finalized_at)

    def test_a_second_decision_on_the_same_version_is_refused(self):
        version = self.resend()
        self.auth(self.artisan_user)
        self.client.post(
            self.url("booking-accept", version),
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY="p8-accept-2",
        )

        again = self.client.post(
            self.url("booking-reject", version),
            {"reason": "Changed my mind."},
            format="json",
            HTTP_IDEMPOTENCY_KEY="p8-reject-8",
        )
        self.assertEqual(again.status_code, status.HTTP_409_CONFLICT)
        version.refresh_from_db()
        self.assertEqual(version.status, Booking.Status.ACCEPTED)

    # ------------------------------------------------- 7–9: obsolete versions

    def test_a_superseded_version_can_no_longer_be_decided(self):
        version = self.resend()
        Booking.objects.filter(pk=version.pk).update(superseded_at=timezone.now())
        version.refresh_from_db()
        self.assertTrue(version.is_superseded)

        actions = self.actions_for(self.artisan_user, booking=version)
        self.assertNotIn("ACCEPT_BOOKING", actions)
        self.assertNotIn("REJECT_BOOKING", actions)

        self.auth(self.artisan_user)
        accepted = self.client.post(
            self.url("booking-accept", version),
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY="p8-accept-3",
        )
        self.assertEqual(accepted.status_code, status.HTTP_409_CONFLICT)
        rejected = self.client.post(
            self.url("booking-reject", version),
            {"reason": "Too late."},
            format="json",
            HTTP_IDEMPOTENCY_KEY="p8-reject-9",
        )
        self.assertEqual(rejected.status_code, status.HTTP_409_CONFLICT)
        version.refresh_from_db()
        self.assertEqual(version.status, Booking.Status.REQUESTED)

    def test_only_one_live_version_of_a_request_can_await_a_decision(self):
        """The database itself refuses a second live version of the same request."""
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                self.make_booking()

        # A resend is still possible, because the previous version is terminal.
        version = self.resend()
        self.assertEqual(version.status, Booking.Status.REQUESTED)
        self.assertEqual(
            Booking.objects.filter(
                request=self.service_request, status=Booking.Status.REQUESTED
            ).count(),
            1,
        )

    # ------------------------------------------------- 10: history preserved

    def test_the_previous_rejection_history_is_preserved(self):
        self.auth(self.artisan_user)
        self.client.post(
            self.url("booking-reject"),
            {"reason": "I cannot take this date."},
            format="json",
            HTTP_IDEMPOTENCY_KEY="p8-reject-10",
        )
        version = self.resend()

        previous = list(
            BookingStatusHistory.objects.filter(booking=self.booking).values_list(
                "status", flat=True
            )
        )
        self.assertIn(Booking.Status.REJECTED, previous)
        self.assertIn(
            "I cannot take this date.",
            " ".join(
                BookingStatusHistory.objects.filter(booking=self.booking).values_list(
                    "notes", flat=True
                )
            ),
        )
        self.assertTrue(
            BookingStatusHistory.objects.filter(
                booking=version, status=Booking.Status.REQUESTED
            ).exists()
        )

    def test_the_frontend_and_the_api_agree_on_the_artisans_buttons(self):
        """The rendered buttons come from the same list the endpoints enforce."""
        version = self.resend()
        self.auth(self.artisan_user)
        response = self.client.get(self.url("booking-detail", version))
        self.assertEqual(
            [item["code"] for item in response.data["available_actions"]],
            self._server_action_codes(version),
        )

    def _server_action_codes(self, booking):
        from .actions import available_action_codes

        return available_action_codes(booking, self.artisan_user)

    # ------------------------------------------------------------------ helper

    def resend(self):
        self.auth(self.artisan_user)
        self.client.post(
            self.url("booking-reject"),
            {"reason": "I cannot take this date."},
            format="json",
            HTTP_IDEMPOTENCY_KEY=f"p8-reject-{Booking.objects.count()}-{id(self)}",
        )
        response = self.client.post(
            self.url("booking-resend"),
            {"reason": "New date agreed."},
            format="json",
            HTTP_IDEMPOTENCY_KEY=f"p8-resend-{Booking.objects.count()}-{id(self)}",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        return Booking.objects.get(pk=response.data["id"])
