from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory

from home.models import ArtisanProfile, CustomUser, CustomerProfile, Locations, Service, ServiceCategory
from services.models import ServiceRequest
from booking.models import Booking
from .models import Review
from .serializers import ReviewSerializer


class ReviewRulesTests(TestCase):
    def setUp(self):
        self.user = CustomUser.objects.create_user(
            email="customer@example.com",
            first_name="Customer",
            last_name="User",
            password="StrongPassword123!",
            is_active=True,
        )
        artisan_user = CustomUser.objects.create_user(
            email="artisan@example.com",
            first_name="Artisan",
            last_name="User",
            password="StrongPassword123!",
            is_active=True,
        )
        self.customer = CustomerProfile.objects.create(user=self.user)
        self.artisan = ArtisanProfile.objects.create(user=artisan_user, profile_status="ACTIVE")
        category = ServiceCategory.objects.create(name="Cleaning")
        Service.objects.create(category=category, name="Home Cleaning")
        location = Locations.objects.create(country="Nigeria", state="Ogun", area="Alabata")
        request = ServiceRequest.objects.create(
            requester=self.user,
            category=category,
            location=location,
            preferred_date=(timezone.now() + timedelta(days=1)).date(),
            description="Clean the home.",
        )
        self.booking = Booking.objects.create(
            request=request,
            artisan=self.artisan,
            customer=self.customer,
            scheduled_time_start=timezone.now() + timedelta(days=1),
            scheduled_time_end=timezone.now() + timedelta(days=1, hours=2),
            price_agreed="15000.00",
            status=Booking.Status.COMPLETED,
        )
        request_factory = APIRequestFactory()
        self.request = request_factory.post("/api/v1/reviews/", {})
        self.request.user = self.user

    def test_review_accepts_completed_booking_status_enum(self):
        serializer = ReviewSerializer(
            data={"booking": self.booking.pk, "rating": 5, "comment": "Good work."},
            context={"request": self.request},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_duplicate_review_is_rejected(self):
        Review.objects.create(
            booking=self.booking,
            customer=self.user,
            artisan=self.artisan,
            rating=5,
            comment="Good work.",
        )
        serializer = ReviewSerializer(
            data={"booking": self.booking.pk, "rating": 4, "comment": "Another review."},
            context={"request": self.request},
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("booking", serializer.errors)


class PlatformFeedbackTests(TestCase):
    """The improvement note is ours, and it is never mixed with the rating."""

    def setUp(self):
        self.user = CustomUser.objects.create_user(
            email="feedback@example.com", first_name="Kehinde", last_name="Omoyayi",
            password="StrongPassword123!", is_active=True,
        )

    def test_a_signed_in_user_can_send_one_note(self):
        from rest_framework.test import APIClient
        from .models import PlatformFeedback

        client = APIClient()
        client.force_authenticate(user=self.user)
        response = client.post(
            "/api/v1/reviews/platform-feedback/",
            {"note": "The offer screen scrolls past the accept button.", "page": "review"},
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        saved = PlatformFeedback.objects.get(user=self.user)
        self.assertIn("accept button", saved.note)
        self.assertEqual(saved.page, "review")

    def test_an_empty_note_is_refused(self):
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(user=self.user)
        response = client.post(
            "/api/v1/reviews/platform-feedback/", {"note": "   "}, format="json"
        )

        self.assertEqual(response.status_code, 400)

    def test_an_anonymous_note_is_refused(self):
        from rest_framework.test import APIClient

        response = APIClient().post(
            "/api/v1/reviews/platform-feedback/", {"note": "hello"}, format="json"
        )

        self.assertIn(response.status_code, (401, 403))
