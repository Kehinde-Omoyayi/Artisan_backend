"""Tests for the seeded service catalogue (two-level taxonomy).

These cover the only things that can actually go wrong in production:
  * a re-run duplicating rows or blowing up on Service.name's global unique key;
  * a row drifting out of its category or being left inactive;
  * a name in the catalogue that the model cannot store.
"""

from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from home.models import Service, ServiceCategory
from services.catalogue import service_taxonomy


class ServiceTaxonomyCatalogueTests(TestCase):
    def test_catalogue_fits_the_model(self):
        """Names, descriptions and uniqueness must satisfy the schema before we try to write."""
        names = [name for _, name, _, _ in service_taxonomy.iter_services()]
        self.assertEqual(len(names), len(set(names)), "Service.name is globally unique: duplicate in catalogue")

        category_names = [category["name"] for category in service_taxonomy.CATEGORIES]
        self.assertEqual(len(category_names), len(set(category_names)))

        for category_name in category_names:
            self.assertLessEqual(len(category_name), 100)
        for name in names:
            self.assertLessEqual(len(name), 150)

    def test_seed_is_idempotent(self):
        call_command("seed_service_taxonomy", stdout=StringIO())
        categories = ServiceCategory.objects.count()
        services = Service.objects.count()

        call_command("seed_service_taxonomy", stdout=StringIO())

        self.assertEqual(ServiceCategory.objects.count(), categories)
        self.assertEqual(Service.objects.count(), services)
        self.assertEqual(services, service_taxonomy.summary()["services"])

    def test_seed_repairs_a_drifted_row(self):
        call_command("seed_service_taxonomy", stdout=StringIO())
        service = Service.objects.get(name="Pipe Installation & Replacement")
        service.category_id = "Cleaning"
        service.description = "stale text"
        service.is_active = False
        service.save()

        call_command("seed_service_taxonomy", stdout=StringIO())

        service.refresh_from_db()
        self.assertEqual(service.category_id, "Plumbing")
        self.assertTrue(service.is_active)
        self.assertNotEqual(service.description, "stale text")

    def test_dry_run_writes_nothing(self):
        call_command("seed_service_taxonomy", "--dry-run", stdout=StringIO())
        self.assertEqual(ServiceCategory.objects.count(), 0)
        self.assertEqual(Service.objects.count(), 0)

    def test_tier_filter_seeds_only_that_tier(self):
        call_command("seed_service_taxonomy", "--tier", service_taxonomy.TIER_CORE, stdout=StringIO())
        core = [c for c in service_taxonomy.CATEGORIES if c["tier"] == service_taxonomy.TIER_CORE]
        self.assertEqual(ServiceCategory.objects.count(), len(core))

    def test_prune_disables_without_deleting(self):
        call_command("seed_service_taxonomy", stdout=StringIO())
        Service.objects.create(category_id="Plumbing", name="Legacy Rubbish Row", description="x")

        call_command("seed_service_taxonomy", "--prune", stdout=StringIO())

        stale = Service.objects.get(name="Legacy Rubbish Row")
        self.assertFalse(stale.is_active)
        self.assertTrue(Service.objects.filter(is_active=True).exists())

    def test_public_endpoints_serve_the_catalogue(self):
        call_command("seed_service_taxonomy", stdout=StringIO())

        categories = self.client.get("/api/v1/services/categories/")
        self.assertEqual(categories.status_code, 200)
        self.assertEqual(len(categories.json()), len(service_taxonomy.CATEGORIES))

        plumbing = self.client.get("/api/v1/services/service_cat/", {"category_name": "Plumbing"})
        self.assertEqual(plumbing.status_code, 200)
        payload = plumbing.json()
        rows = payload["results"] if isinstance(payload, dict) else payload
        self.assertEqual(len(rows), 10)
        self.assertTrue(all(row["category_name"] == "Plumbing" for row in rows))
