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

    def publish(self, location=ASOKORO, service=None):
        return self.client.post(
            URL,
            {
                "service_name": (service or self.service).name,
                "starting_price": "5000.00",
                "location": location,
            },
            format="json",
        )

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
