from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from home.models import ArtisanProfile, ArtisanService, CustomUser, CustomerProfile, Locations, Service, ServiceCategory
from .models import ArtisanVerification, ServiceRequest


TEST_CACHE = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "artisan-matched-request-tests",
    }
}


@override_settings(CACHES=TEST_CACHE)
class ArtisanMatchedServiceRequestApiTests(TestCase):
    endpoint = "/api/v1/services/artisan/requests/"

    def setUp(self):
        self.client = APIClient()
        self.customer_user = CustomUser.objects.create_user(
            email="requester@example.test",
            first_name="Request",
            last_name="Owner",
            password="StrongPassword123!",
            is_active=True,
        )
        CustomerProfile.objects.create(user=self.customer_user)
        artisan_user = CustomUser.objects.create_user(
            email="artisan@example.test",
            first_name="Ari",
            last_name="Artisan",
            password="StrongPassword123!",
            is_active=True,
        )
        self.artisan = ArtisanProfile.objects.create(
            user=artisan_user,
            business_name="Ari Repairs",
            profile_status="ACTIVE",
        )
        self.verification = ArtisanVerification.objects.create(
            artisan=self.artisan,
            verification_status="VERIFIED",
            verification_method="NIN",
        )
        self.category = ServiceCategory.objects.create(name="Plumbing")
        self.other_category = ServiceCategory.objects.create(name="Painting")
        self.service = Service.objects.create(category=self.category, name="Pipe repair")
        self.other_service = Service.objects.create(category=self.other_category, name="Wall painting")
        self.location = Locations.objects.create(country="Nigeria", state="Lagos", area="Yaba")
        self.other_location = Locations.objects.create(country="Nigeria", state="Lagos", area="Lekki")
        self.offering = ArtisanService.objects.create(
            artisan=self.artisan,
            service=self.service,
            location=self.location,
            starting_price="15000.00",
        )
        self.matching_request = self.make_request(
            category=self.category,
            service=self.service,
            location=self.location,
            description="Repair the leaking sink pipe.",
        )
        self.wrong_area_request = self.make_request(
            category=self.category,
            service=self.service,
            location=self.other_location,
            description="Repair a pipe in another area.",
        )
        self.wrong_category_request = self.make_request(
            category=self.other_category,
            service=self.other_service,
            location=self.location,
            description="Paint a living room.",
        )

    def make_request(self, *, category, service, location, description):
        return ServiceRequest.objects.create(
            requester=self.customer_user,
            category=category,
            service=service,
            location=location,
            preferred_date=(timezone.localdate() + timedelta(days=4)),
            urgency="NORMAL",
            budget="25000.00",
            description=description,
        )

    def get_items(self, response):
        if isinstance(response.data, dict):
            return response.data.get("results", [])
        return response.data

    def test_artisan_gets_only_requests_matching_active_verified_services_and_area(self):
        self.client.force_authenticate(self.artisan.user)

        response = self.client.get(self.endpoint)

        self.assertEqual(response.status_code, 200)
        items = self.get_items(response)
        self.assertEqual([item["id"] for item in items], [self.matching_request.id])
        self.assertEqual(items[0]["category_name"], "Plumbing")
        self.assertEqual(items[0]["service_name"], "Pipe repair")
        self.assertEqual(items[0]["location"], {"country": "Nigeria", "state": "Lagos", "area": "Yaba"})
        self.assertEqual(items[0]["description"], "Repair the leaking sink pipe.")
        self.assertNotIn("requester_id", items[0])
        self.assertNotIn("email", items[0])
        self.assertNotIn("requester@example.test", str(response.data))

    def test_general_service_offering_matches_requests_in_any_area(self):
        self.offering.location = None
        self.offering.save(update_fields=["location"])
        self.client.force_authenticate(self.artisan.user)

        response = self.client.get(self.endpoint)

        self.assertEqual(response.status_code, 200)
        ids = {item["id"] for item in self.get_items(response)}
        self.assertIn(self.matching_request.id, ids)
        self.assertIn(self.wrong_area_request.id, ids)
        self.assertNotIn(self.wrong_category_request.id, ids)

    def test_requests_are_hidden_when_artisan_is_not_verified(self):
        self.verification.verification_status = "PENDING"
        self.verification.save(update_fields=["verification_status"])
        self.client.force_authenticate(self.artisan.user)

        response = self.client.get(self.endpoint)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.get_items(response), [])

    def test_requests_are_hidden_when_artisan_profile_is_inactive(self):
        self.artisan.profile_status = "INACTIVE"
        self.artisan.save(update_fields=["profile_status"])
        self.client.force_authenticate(self.artisan.user)

        response = self.client.get(self.endpoint)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.get_items(response), [])

    def test_requests_are_hidden_when_the_matching_service_is_inactive(self):
        self.service.is_active = False
        self.service.save(update_fields=["is_active"])
        self.client.force_authenticate(self.artisan.user)

        response = self.client.get(self.endpoint)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.get_items(response), [])

    def test_customer_cannot_use_the_artisan_request_inbox(self):
        self.client.force_authenticate(self.customer_user)

        response = self.client.get(self.endpoint)

        self.assertEqual(response.status_code, 403)

    def test_artisan_request_inbox_is_read_only(self):
        self.client.force_authenticate(self.artisan.user)

        response = self.client.post(self.endpoint, {}, format="json")

        self.assertEqual(response.status_code, 405)
