from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from home.models import ArtisanProfile, ArtisanService, CustomUser, Locations, Service, ServiceCategory
from services.models import ArtisanVerification


class PublicArtisanDiscoveryTests(TestCase):
    def setUp(self):
        self.artisan_user = CustomUser.objects.create_user(
            email="public-artisan@example.test",
            first_name="Ayo",
            last_name="Artisan",
            password="StrongPassword123!",
            is_active=True,
        )
        self.artisan = ArtisanProfile.objects.create(
            user=self.artisan_user,
            business_name="Ayo Home Repairs",
            bio="Residential repairs and maintenance.",
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
        category = ServiceCategory.objects.create(name="Home Repair")
        service = Service.objects.create(
            category=category,
            name="Plumbing",
            description="Repairs and installation.",
            is_active=True,
        )
        location = Locations.objects.create(
            country="Nigeria",
            state="Lagos",
            area="Ikeja",
        )
        ArtisanService.objects.create(
            artisan=self.artisan,
            service=service,
            location=location,
            starting_price="15000.00",
        )
        self.client = APIClient()

    def test_anonymous_visitors_can_list_and_view_only_public_artisan_details(self):
        listing = self.client.get(reverse("artisan-discovery-list"))
        self.assertEqual(listing.status_code, 200)
        listed = listing.data.get("results", listing.data) if isinstance(listing.data, dict) else listing.data
        self.assertEqual(len(listed), 1)
        self.assertEqual(str(listed[0]["id"]), str(self.artisan.id))

        detail = self.client.get(reverse("artisan-discovery-detail", args=[self.artisan.id]))
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.data["business_name"], "Ayo Home Repairs")
        self.assertEqual(detail.data["area"], "Ikeja")
        self.assertEqual(detail.data["services"][0]["service_name"], "Plumbing")
        self.assertEqual(detail.data["services"][0]["location"]["area"], "Ikeja")

        private_fields = {
            "email",
            "phone_number",
            "account_balance",
            "bank_account",
            "payout_account",
            "bvn",
            "nin",
        }
        self.assertTrue(private_fields.isdisjoint(detail.data))
        self.assertTrue(private_fields.isdisjoint(detail.data["services"][0]))

    def test_direct_messaging_api_remains_authenticated(self):
        response = self.client.post(
            reverse("conversation-direct-artisan", args=[self.artisan.id]),
            {"initial_message": ""},
            format="json",
        )
        self.assertIn(response.status_code, {401, 403})
