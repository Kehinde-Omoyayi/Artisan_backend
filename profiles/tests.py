from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from home.models import ArtisanProfile, ArtisanService, CustomUser, Locations, Service, ServiceCategory
from services.models import ArtisanVerification


class ArtisanDiscoveryDetailTests(TestCase):
    def test_service_offering_exposes_catalogue_service_id_for_request_start(self):
        artisan_user = CustomUser.objects.create_user(
            email="profile-artisan@example.com",
            first_name="Tomi",
            last_name="Artisan",
            password="StrongPassword123!",
            is_active=True,
        )
        artisan = ArtisanProfile.objects.create(
            user=artisan_user,
            business_name="Lagos Spark Electrical",
            profile_status="ACTIVE",
        )
        ArtisanVerification.objects.create(
            artisan=artisan,
            verification_status="VERIFIED",
            verification_method="NIN",
        )
        category = ServiceCategory.objects.create(name="Electrical")
        service = Service.objects.create(
            category=category,
            name="Electrical Repairs",
            description="Residential electrical repair.",
        )
        location = Locations.objects.create(
            country="Nigeria",
            state="Lagos",
            area="Ikeja",
        )
        ArtisanService.objects.create(
            artisan=artisan,
            service=service,
            location=location,
            starting_price="15000.00",
        )
        viewer = CustomUser.objects.create_user(
            email="profile-customer@example.com",
            first_name="Nia",
            last_name="Customer",
            password="StrongPassword123!",
            is_active=True,
        )
        client = APIClient()
        client.force_authenticate(user=viewer)

        response = client.get(reverse("artisan-discovery-detail", args=[artisan.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["services"][0]["service_id"], service.id)
        self.assertEqual(response.data["services"][0]["service_name"], service.name)
