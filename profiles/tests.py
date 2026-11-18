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


class DiscoveryAnywhereFlagTests(TestCase):
    """Find-artisan filters: a flagged row answers every geography, a legacy
    NULL row keeps its strict never-bypass meaning."""

    def setUp(self):
        artisan_user = CustomUser.objects.create_user(
            email="anywhere-artisan@example.com",
            first_name="Ada",
            last_name="Artisan",
            password="StrongPassword123!",
            is_active=True,
        )
        self.artisan = ArtisanProfile.objects.create(
            user=artisan_user,
            business_name="Everywhere Repairs",
            profile_status="ACTIVE",
        )
        category = ServiceCategory.objects.create(name="Plumbing")
        self.service = Service.objects.create(
            category=category,
            name="Pipe Repair",
            description="Repair leaking pipes.",
        )
        self.ogun = Locations.objects.create(
            country="Nigeria", state="Ogun", area="Abeokuta"
        )
        self.flagged = ArtisanService.objects.create(
            artisan=self.artisan,
            service=self.service,
            location=self.ogun,
            available_anywhere=True,
        )

    def _rows(self, params, available_anywhere):
        from profiles.discovery import _service_filter_queryset

        return list(
            _service_filter_queryset(
                params, None, available_anywhere=available_anywhere
            )
        )

    def test_a_flagged_row_answers_any_location_filter(self):
        rows = self._rows({"state": "Lagos"}, None)

        self.assertIn(self.flagged, rows)

    def test_a_flagged_row_counts_as_an_anywhere_offer(self):
        rows = self._rows({}, True)

        self.assertIn(self.flagged, rows)

    def test_a_plain_located_row_does_not_answer_another_state(self):
        abuja = Locations.objects.create(
            country="Nigeria",
            state="Abuja Federal Capital Territory",
            area="Asokoro",
        )
        plain = ArtisanService.objects.create(
            artisan=self.artisan,
            service=self.service,
            location=abuja,
        )

        rows = self._rows({"state": "Lagos"}, None)

        self.assertNotIn(plain, rows)

    def test_a_legacy_null_row_still_never_bypasses_a_filter(self):
        legacy = ArtisanService.objects.create(
            artisan=self.artisan,
            service=self.service,
            location=None,
        )

        rows = self._rows({"state": "Lagos"}, None)

        self.assertNotIn(legacy, rows)
        self.assertIn(self.flagged, rows)

        anywhere_rows = self._rows({}, True)
        self.assertIn(legacy, anywhere_rows)
        self.assertIn(self.flagged, anywhere_rows)
