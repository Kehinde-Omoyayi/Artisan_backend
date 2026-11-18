"""Publishing an offering with the location the frontend dropdowns sent.

The page owns the country/state/area choices; this path used to hand the dict
straight to a ForeignKey and answer 500, so no artisan could ever publish a
service.
"""

from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from home.models import (
    ArtisanProfile,
    ArtisanService,
    CustomUser,
    Locations,
    Service,
    ServiceCategory,
)
from services.models import ArtisanVerification

URL = "/api/v1/services/get_artisan_services/"
ASOKORO = {
    "country": "Nigeria",
    "state": "Abuja Federal Capital Territory",
    "area": "Asokoro",
}


class ArtisanServiceLocationTests(TestCase):
    def setUp(self):
        self.user = CustomUser.objects.create_user(
            email="artisan@example.com",
            first_name="Test",
            last_name="Artisan",
            password="Passw0rd!",
            is_active=True,
        )
        self.artisan = ArtisanProfile.objects.create(
            user=self.user,
            business_name="Test Artisan",
            profile_status="ACTIVE",
            country="Nigeria",
            state="Abuja Federal Capital Territory",
            area="Asokoro",
        )
        ArtisanVerification.objects.create(
            artisan=self.artisan,
            verification_status="VERIFIED",
            verification_method="NIN",
        )
        self.category = ServiceCategory.objects.create(name="Electrical")
        self.service = Service.objects.create(
            category=self.category,
            name="Socket repair",
            description="Replace or repair a wall socket.",
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def publish(self, location=ASOKORO, service=None, available_anywhere=None):
        body = {
            "service_name": (service or self.service).name,
            "starting_price": "5000.00",
            "location": location,
        }
        if available_anywhere is not None:
            body["available_anywhere"] = available_anywhere
        return self.client.post(URL, body, format="json")

    def test_the_place_the_page_sent_is_saved(self):
        response = self.publish()

        self.assertEqual(response.status_code, 201, response.data)
        offering = ArtisanService.objects.get(
            artisan=self.artisan, service=self.service, location__isnull=False
        )
        self.assertEqual(offering.location.area, "Asokoro")
        self.assertEqual(response.data["location"]["country"], "Nigeria")
        self.assertEqual(response.data["location"]["state"], ASOKORO["state"])

    def test_the_same_place_is_one_record_and_a_readable_error(self):
        self.assertEqual(self.publish().status_code, 201)

        again = self.publish()

        self.assertEqual(again.status_code, 400)
        self.assertIn("already offer this service", str(again.data))
        self.assertEqual(Locations.objects.filter(area="Asokoro").count(), 1)

    def test_anywhere_and_a_named_place_coexist(self):
        self.assertEqual(self.publish(location=None).status_code, 201)

        named = self.publish()

        self.assertEqual(named.status_code, 201, named.data)
        self.assertEqual(ArtisanService.objects.filter(artisan=self.artisan).count(), 2)

    def test_an_incomplete_place_is_refused_in_words(self):
        response = self.publish(location={"country": "Nigeria", "area": "Asokoro"})

        self.assertEqual(response.status_code, 400)
        self.assertIn("Country, state and area", str(response.data))
        self.assertFalse(ArtisanService.objects.filter(artisan=self.artisan).exists())

    def test_the_everywhere_promise_rides_on_one_located_row(self):
        response = self.publish(available_anywhere=True)

        self.assertEqual(response.status_code, 201, response.data)
        rows = ArtisanService.objects.filter(
            artisan=self.artisan, service=self.service
        )
        self.assertEqual(rows.count(), 1)
        offering = rows.get()
        self.assertIsNotNone(offering.location)
        self.assertTrue(offering.available_anywhere)
        self.assertTrue(response.data["available_anywhere"])

    def test_a_null_location_row_never_carries_the_flag(self):
        # NULL location already is the anywhere contract; the flag is forced
        # off so the check constraint always holds.
        response = self.publish(location=None, available_anywhere=True)

        self.assertEqual(response.status_code, 201, response.data)
        offering = ArtisanService.objects.get(
            artisan=self.artisan, service=self.service
        )
        self.assertIsNone(offering.location)
        self.assertFalse(offering.available_anywhere)


class EverywhereFlagMatchingTests(TestCase):
    """A flagged offering serves every place, exactly like a NULL row does."""

    def setUp(self):
        self.user = CustomUser.objects.create_user(
            email="flag-artisan@example.com",
            first_name="Flag",
            last_name="Artisan",
            password="Passw0rd!",
            is_active=True,
        )
        self.artisan = ArtisanProfile.objects.create(
            user=self.user,
            business_name="Flag Artisan",
            profile_status="ACTIVE",
            country="Nigeria",
            state="Abuja Federal Capital Territory",
            area="Asokoro",
        )
        ArtisanVerification.objects.create(
            artisan=self.artisan,
            verification_status="VERIFIED",
            verification_method="NIN",
        )
        self.category = ServiceCategory.objects.create(name="Plumbing")
        self.service = Service.objects.create(
            category=self.category,
            name="Pipe repair",
            description="Fix a leaking pipe.",
        )
        self.asokoro = Locations.objects.create(**ASOKORO)
        self.lagos = Locations.objects.create(
            country="Nigeria", state="Lagos", area="Ikeja"
        )
        self.customer = CustomUser.objects.create_user(
            email="flag-customer@example.com",
            first_name="Flag",
            last_name="Customer",
            password="Passw0rd!",
            is_active=True,
        )

    def _request(self, location):
        from services.models import ServiceRequest

        return ServiceRequest.objects.create(
            requester=self.customer,
            service=self.service,
            category=self.category,
            location=location,
            description="Please fix it.",
        )

    def _publish(self, location, *, anywhere_flag):
        return ArtisanService.objects.create(
            artisan=self.artisan,
            service=self.service,
            location=location,
            available_anywhere=anywhere_flag,
        )

    def test_a_flagged_offering_matches_a_request_in_another_place(self):
        self._publish(self.asokoro, anywhere_flag=True)
        request = self._request(self.lagos)

        from services.request_matching import matched_requests_for

        self.assertIn(request, list(matched_requests_for(self.artisan)))

    def test_a_plain_offering_does_not_match_another_place(self):
        self._publish(self.asokoro, anywhere_flag=False)
        request = self._request(self.lagos)

        from services.request_matching import matched_requests_for

        self.assertNotIn(request, list(matched_requests_for(self.artisan)))

    def test_a_null_offering_still_matches_every_place(self):
        ArtisanService.objects.create(
            artisan=self.artisan,
            service=self.service,
            location=None,
        )
        request = self._request(self.lagos)

        from services.request_matching import matched_requests_for

        self.assertIn(request, list(matched_requests_for(self.artisan)))
