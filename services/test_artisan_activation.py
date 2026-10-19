from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from home.models import ArtisanProfile, CustomUser
from services.models import ArtisanVerification


TEST_CACHE = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "artisan-activation-tests",
    }
}


@override_settings(CACHES=TEST_CACHE)
class ArtisanActivationApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = CustomUser.objects.create_superuser(
            email="activation-admin@example.test",
            first_name="Support",
            last_name="Admin",
            password="StrongPassword123!",
        )
        self.artisan_user = CustomUser.objects.create_user(
            email="activation-artisan@example.test",
            first_name="Ari",
            last_name="Artisan",
            password="StrongPassword123!",
            is_active=True,
        )
        self.artisan = ArtisanProfile.objects.create(
            user=self.artisan_user,
            profile_status="INACTIVE",
        )
        self.endpoint = f"/api/v1/services/artisan/profile/{self.artisan.pk}/activate/"

    def test_staff_can_activate_a_verified_and_eligible_artisan(self):
        ArtisanVerification.objects.create(
            artisan=self.artisan,
            verification_status="VERIFIED",
            verification_method="NIN",
        )
        self.client.force_authenticate(self.admin)

        response = self.client.post(self.endpoint, {"activate": True}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["profile_status"], "ACTIVE")
        self.artisan.refresh_from_db()
        self.assertEqual(self.artisan.profile_status, "ACTIVE")

    def test_staff_can_deactivate_an_artisan(self):
        self.artisan.profile_status = "ACTIVE"
        self.artisan.save(update_fields=["profile_status"])
        self.client.force_authenticate(self.admin)

        response = self.client.post(self.endpoint, {"activate": False}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["profile_status"], "INACTIVE")
        self.artisan.refresh_from_db()
        self.assertEqual(self.artisan.profile_status, "INACTIVE")

    def test_activation_is_rejected_until_verification_is_complete(self):
        ArtisanVerification.objects.create(
            artisan=self.artisan,
            verification_status="PENDING",
            verification_method="NIN",
        )
        self.client.force_authenticate(self.admin)

        response = self.client.post(self.endpoint, {"activate": True}, format="json")

        self.assertEqual(response.status_code, 409)
        self.artisan.refresh_from_db()
        self.assertEqual(self.artisan.profile_status, "INACTIVE")

    def test_non_staff_cannot_change_activation(self):
        self.client.force_authenticate(self.artisan_user)

        response = self.client.post(self.endpoint, {"activate": False}, format="json")

        self.assertEqual(response.status_code, 403)
        self.artisan.refresh_from_db()
        self.assertEqual(self.artisan.profile_status, "INACTIVE")
