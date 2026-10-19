from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from home.models import Service, ServiceCategory


class PublicServiceCatalogueTests(TestCase):
    def setUp(self):
        self.category = ServiceCategory.objects.create(name="Home Repair")
        self.active_service = Service.objects.create(
            category=self.category,
            name="Plumbing",
            description="Repairs and installation.",
            is_active=True,
        )
        Service.objects.create(
            category=self.category,
            name="Legacy inactive service",
            description="Not available for discovery.",
            is_active=False,
        )
        self.client = APIClient()

    def test_anonymous_visitors_can_read_only_active_categories_and_services(self):
        categories = self.client.get(reverse("service-category-list"))
        self.assertEqual(categories.status_code, 200)
        self.assertEqual([item["name"] for item in categories.data], ["Home Repair"])

        services = self.client.get(reverse("service-list"))
        self.assertEqual(services.status_code, 200)
        self.assertEqual([item["name"] for item in services.data], ["Plumbing"])
        self.assertEqual(services.data[0]["category_name"], "Home Repair")
        self.assertNotIn("Legacy inactive service", [item["name"] for item in services.data])

    def test_anonymous_category_filter_works_without_opening_account_endpoints(self):
        response = self.client.get(reverse("service-list"), {"category_name": "Home Repair"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["id"] for item in response.data], [self.active_service.id])
