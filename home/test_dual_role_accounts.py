import importlib
from types import SimpleNamespace

from django.apps import apps
from django.db import connection
from django.test import TestCase

from home.models import ArtisanProfile, CustomUser, CustomerProfile


class ArtisanCustomerProfileBackfillTests(TestCase):
    def test_existing_artisans_receive_a_separate_customer_profile(self):
        user = CustomUser.objects.create_user(
            email="pre-dual-role-artisan@example.com",
            first_name="Pre",
            last_name="Existing",
            password="StrongPassword123!",
            is_active=True,
        )
        artisan = ArtisanProfile.objects.create(
            user=user,
            business_name="Existing Artisan",
            profile_status="ACTIVE",
        )
        self.assertFalse(CustomerProfile.objects.filter(user=user).exists())

        migration = importlib.import_module("home.migrations.0010_backfill_customer_profiles_for_artisans")
        migration.create_customer_profiles_for_existing_artisans(
            apps,
            SimpleNamespace(connection=connection),
        )

        customer_profile = CustomerProfile.objects.get(user=user)
        self.assertEqual(customer_profile.user_id, artisan.user_id)
        self.assertEqual(customer_profile.country, "")
        self.assertEqual(customer_profile.state, "")
        self.assertEqual(customer_profile.area, "")
        self.assertTrue(ArtisanProfile.objects.filter(pk=artisan.pk).exists())
