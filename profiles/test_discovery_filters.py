from django.contrib.auth.models import Group
from django.core.cache import cache
from django.db import connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework.test import APIClient

from home.models import ArtisanProfile, ArtisanService, CustomUser, Institution, Locations, Service, ServiceCategory
from profiles.discovery_cache import (
    DISCOVERY_CACHE_TTL,
    current_discovery_generation,
)
from profiles.serializers import ArtisanProfileSerializer
from services.models import ArtisanVerification
from support.permissions import SUPPORT_AGENT


TEST_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "discovery-tests",
    }
}


@override_settings(CACHES=TEST_CACHES)
class DiscoveryFilterTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.url = reverse("artisan-discovery-list")
        self.ikeja = Locations.objects.create(country="Nigeria", state="Lagos", area="Ikeja")
        self.yaba = Locations.objects.create(country="Nigeria", state="Lagos", area="Yaba")
        self.abeokuta = Locations.objects.create(country="Nigeria", state="Ogun", area="Abeokuta")
        self.home_repair = ServiceCategory.objects.create(name="Home Repair")
        self.plumbing = Service.objects.create(
            category=self.home_repair,
            name="Pipe Repair",
            description="Repairs and installation.",
            is_active=True,
        )

    def make_artisan(self, email, *, business_name, base, service_locations, bio="Public biography"):
        user = CustomUser.objects.create_user(
            email=email,
            first_name=business_name.split()[0],
            last_name="Maker",
            password="StrongPassword123!",
            is_active=True,
        )
        artisan = ArtisanProfile.objects.create(
            user=user,
            business_name=business_name,
            bio=bio,
            country=base.country,
            state=base.state,
            area=base.area,
            profile_status="ACTIVE",
        )
        ArtisanVerification.objects.create(
            artisan=artisan,
            verification_status="VERIFIED",
            verification_method="NIN",
        )
        for location in service_locations:
            ArtisanService.objects.create(
                artisan=artisan,
                service=self.plumbing,
                location=location,
                starting_price="12000.00",
            )
        return artisan

    @staticmethod
    def result_ids(response):
        data = response.data
        rows = data.get("results", data) if isinstance(data, dict) else data
        return {str(row["id"]) for row in rows}

    def test_location_filters_match_service_coverage_and_keep_registered_base_distinct(self):
        local = self.make_artisan(
            "local@example.test", business_name="Local Plumber", base=self.abeokuta, service_locations=[self.ikeja]
        )
        anywhere = self.make_artisan(
            "anywhere@example.test", business_name="Anywhere Plumbing", base=self.abeokuta, service_locations=[None]
        )
        registered_only = self.make_artisan(
            "registered@example.test", business_name="Registered Base Only", base=self.ikeja, service_locations=[self.yaba]
        )

        response = self.client.get(self.url, {
            "country": "Nigeria", "state": "Lagos", "area": "Ikeja", "limit": 60,
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.result_ids(response), {str(local.id), str(anywhere.id)})
        rows = {str(row["id"]): row for row in response.data["results"]}
        self.assertTrue(rows[str(anywhere.id)]["available_anywhere"])
        self.assertEqual(rows[str(anywhere.id)]["area"], "Abeokuta")
        self.assertNotIn(str(registered_only.id), rows)

    def test_institution_filter_uses_stable_database_identity_and_existing_location(self):
        local = self.make_artisan(
            "institution-local@example.test", business_name="Ikeja Trades", base=self.ikeja, service_locations=[self.ikeja]
        )
        anywhere = self.make_artisan(
            "institution-anywhere@example.test", business_name="Any Area Trades", base=self.abeokuta, service_locations=[None]
        )
        other = self.make_artisan(
            "institution-other@example.test", business_name="Yaba Trades", base=self.yaba, service_locations=[self.yaba]
        )
        institution = Institution.objects.create(
            name="Example Technical Institute", campus="Main", abbreviation="ETI", location=self.ikeja
        )
        catalogue = self.client.get(reverse("institution-list"))
        self.assertEqual(catalogue.status_code, 200)
        self.assertEqual(str(catalogue.data[0]["id"]), str(institution.pk))
        public_locations = self.client.get(reverse("location-list-create"))
        self.assertEqual(public_locations.status_code, 200)

        response = self.client.get(self.url, {"institution_id": str(institution.pk), "limit": 60})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.result_ids(response), {str(local.id), str(anywhere.id)})
        invalid = self.client.get(self.url, {"institution_id": "not-an-institution"})
        self.assertEqual(invalid.status_code, 400)
        self.assertNotIn(str(other.id), self.result_ids(response))

    def test_anywhere_filter_has_explicit_true_and_false_semantics(self):
        local = self.make_artisan(
            "availability-local@example.test", business_name="Local Service", base=self.ikeja, service_locations=[self.ikeja]
        )
        anywhere = self.make_artisan(
            "availability-anywhere@example.test", business_name="Anywhere Service", base=self.abeokuta, service_locations=[None]
        )
        mixed = self.make_artisan(
            "availability-mixed@example.test", business_name="Mixed Service", base=self.ikeja, service_locations=[self.ikeja, None]
        )

        anywhere_response = self.client.get(self.url, {"available_anywhere": "true"})
        local_response = self.client.get(self.url, {"available_anywhere": "false"})
        local_search = self.client.get(self.url, {"area": "Ikeja"})
        anywhere_search = self.client.get(self.url, {"area": "Ikeja", "available_anywhere": "false"})

        self.assertEqual(self.result_ids(anywhere_response), {str(anywhere.id), str(mixed.id)})
        self.assertEqual(self.result_ids(local_response), {str(local.id)})
        self.assertEqual(self.result_ids(local_search), {str(local.id), str(anywhere.id), str(mixed.id)})
        self.assertEqual(self.result_ids(anywhere_search), {str(local.id)})
        self.assertEqual(self.client.get(self.url, {"available_anywhere": "maybe"}).status_code, 400)

    def test_category_service_search_and_pagination_are_server_side(self):
        artisan_a = self.make_artisan(
            "search-a@example.test", business_name="Pipe Artisan", base=self.ikeja, service_locations=[self.ikeja]
        )
        artisan_b = self.make_artisan(
            "search-b@example.test", business_name="Pipe Specialist", base=self.yaba, service_locations=[self.yaba]
        )
        q_response = self.client.get(self.url, {"q": "Pipe Artisan"})
        category_response = self.client.get(self.url, {"category_name": "Home Repair"})
        service_response = self.client.get(self.url, {"service_name": "Pipe Repair"})
        page = self.client.get(self.url, {"category_name": "Home Repair", "limit": 1, "offset": 1})

        self.assertEqual(self.result_ids(q_response), {str(artisan_a.id)})
        self.assertEqual(self.result_ids(category_response), {str(artisan_a.id), str(artisan_b.id)})
        self.assertEqual(self.result_ids(service_response), {str(artisan_a.id), str(artisan_b.id)})
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.data["count"], 2)
        self.assertEqual(len(page.data["results"]), 1)

    def test_search_is_partial_and_matches_places_services_and_multi_word_phrases(self):
        artisan = self.make_artisan(
            "partial-search@example.test",
            business_name="Hagioz Craftworks",
            base=self.abeokuta,
            service_locations=[None],
        )

        for term in ("HAGIOZ", "hagioz", "hagi", "craftworks", "Abeokuta", "abeok", "Ogun", "Nigeria"):
            with self.subTest(term=term):
                response = self.client.get(self.url, {"q": term})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.result_ids(response), {str(artisan.id)}, msg=f"no match for {term!r}")

        # Service name/category fragments match even when the service has no coverage row.
        for term in ("pipe", "repair", "home rep"):
            with self.subTest(term=term):
                response = self.client.get(self.url, {"q": term})
                self.assertEqual(self.result_ids(response), {str(artisan.id)}, msg=f"no match for {term!r}")

        # Multi-word phrases require every token to match somewhere on the artisan.
        self.assertEqual(self.result_ids(self.client.get(self.url, {"q": "hagioz abeokuta"})), {str(artisan.id)})
        self.assertEqual(self.result_ids(self.client.get(self.url, {"q": "hagioz abuja"})), set())

        # Catalogue filters are forgiving substrings too.
        self.assertEqual(
            self.result_ids(self.client.get(self.url, {"category_name": "home rep"})),
            {str(artisan.id)},
        )
        self.assertEqual(
            self.result_ids(self.client.get(self.url, {"service_name": "pipe"})),
            {str(artisan.id)},
        )

    def test_public_list_excludes_links_and_biography_but_detail_exposes_links(self):
        artisan = self.make_artisan(
            "links@example.test", business_name="Linked Artisan", base=self.ikeja, service_locations=[self.ikeja]
        )
        artisan.social_media_1_platform = "INSTAGRAM"
        artisan.social_media_1_url = "https://www.instagram.com/linkedartisan/"
        artisan.social_media_2_platform = "YOUTUBE"
        artisan.social_media_2_url = "https://youtu.be/examplechannel"
        artisan.portfolio_url = "https://portfolio.example.com/work"
        artisan.save()

        listing = self.client.get(self.url)
        listed = listing.data["results"][0]
        self.assertNotIn("bio", listed)
        self.assertNotIn("social_media_1_url", listed)
        self.assertNotIn("social_media_2_url", listed)
        self.assertNotIn("portfolio_url", listed)

        detail = self.client.get(reverse("artisan-discovery-detail", args=[artisan.pk]))
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.data["social_media_1_platform_label"], "Instagram")
        self.assertEqual(detail.data["social_media_1_url"], artisan.social_media_1_url)
        self.assertEqual(detail.data["portfolio_url"], artisan.portfolio_url)
        self.assertNotIn("email", detail.data)
        self.assertNotIn("phone_number", detail.data)

    def test_social_and_portfolio_url_validation_rejects_unsafe_or_mismatched_urls(self):
        artisan = self.make_artisan(
            "validation@example.test", business_name="Validation Artisan", base=self.ikeja, service_locations=[self.ikeja]
        )
        valid = ArtisanProfileSerializer(
            artisan,
            data={
                "social_media_1_platform": "INSTAGRAM",
                "social_media_1_url": "https://instagram.com/validationartisan/",
                "portfolio_url": "https://example.com/portfolio",
            },
            partial=True,
        )
        self.assertTrue(valid.is_valid(), valid.errors)

        for payload in (
            {"social_media_1_platform": "INSTAGRAM", "social_media_1_url": "https://example.com/profile"},
            {"social_media_1_platform": "X", "social_media_1_url": "javascript:alert(1)"},
            {"portfolio_url": "http://127.0.0.1/admin"},
            {"portfolio_url": "data:text/html,unsafe"},
            {"portfolio_url": "https://example.com:0/path"},
        ):
            with self.subTest(payload=payload):
                serializer = ArtisanProfileSerializer(artisan, data=payload, partial=True)
                self.assertFalse(serializer.is_valid())

    def test_support_review_endpoint_is_restricted_and_contains_no_private_contact_fields(self):
        artisan = self.make_artisan(
            "review-links@example.test", business_name="Review Links", base=self.ikeja, service_locations=[self.ikeja]
        )
        artisan.social_media_1_platform = "TIKTOK"
        artisan.social_media_1_url = "https://www.tiktok.com/@reviewlinks"
        artisan.save()
        url = reverse("artisan-professional-links-review", args=[artisan.pk])

        self.assertIn(self.client.get(url).status_code, {401, 403})
        viewer = CustomUser.objects.create_user(
            email="support-agent@example.test", first_name="Support", last_name="Agent",
            password="StrongPassword123!", is_active=True, is_staff=True,
        )
        viewer.groups.add(Group.objects.create(name=SUPPORT_AGENT))
        self.client.force_authenticate(user=viewer)
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["social_media_1_url"], artisan.social_media_1_url)
        self.assertNotIn("email", response.data)
        self.assertNotIn("phone_number", response.data)

    def test_cache_hit_avoids_repeat_database_work_and_ttl_is_five_minutes(self):
        artisan = self.make_artisan(
            "cache@example.test", business_name="Cached Artisan", base=self.ikeja, service_locations=[self.ikeja]
        )
        first = self.client.get(self.url, {"country": "Nigeria", "state": "Lagos", "area": "Ikeja"})
        self.assertIn(str(artisan.pk), self.result_ids(first))
        with CaptureQueriesContext(connection) as queries:
            second = self.client.get(self.url, {"country": "Nigeria", "state": "Lagos", "area": "Ikeja"})
        self.assertEqual(second.status_code, 200)
        self.assertEqual(len(queries), 0)
        self.assertEqual(DISCOVERY_CACHE_TTL, 300)
        self.client.get(self.url, {"available_anywhere": "true"})
        with CaptureQueriesContext(connection) as normalized_alias_queries:
            alias_response = self.client.get(self.url, {"available_anywhere": "1"})
        self.assertEqual(alias_response.status_code, 200)
        self.assertEqual(len(normalized_alias_queries), 0)
        different_page = self.client.get(self.url, {"country": "Nigeria", "state": "Lagos", "area": "Ikeja", "limit": 1})
        self.assertEqual(different_page.status_code, 200)


@override_settings(CACHES=TEST_CACHES)
class DiscoveryCacheInvalidationTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        cache.clear()
        self.location = Locations.objects.create(country="Nigeria", state="Lagos", area="Ikeja")
        category = ServiceCategory.objects.create(name="Repairs")
        service = Service.objects.create(category=category, name="General Repair")
        user = CustomUser.objects.create_user(
            email="cache-invalidation@example.test", first_name="Cached", last_name="Artisan",
            password="StrongPassword123!", is_active=True,
        )
        self.artisan = ArtisanProfile.objects.create(
            user=user, business_name="Before Update", profile_status="ACTIVE",
            country="Nigeria", state="Lagos", area="Ikeja",
        )
        ArtisanVerification.objects.create(
            artisan=self.artisan, verification_status="VERIFIED", verification_method="NIN"
        )
        ArtisanService.objects.create(artisan=self.artisan, service=service, location=self.location)
        self.client = APIClient()

    def test_relevant_profile_change_advances_generation_after_commit(self):
        url = reverse("artisan-discovery-list")
        self.client.get(url)
        generation = current_discovery_generation()
        with transaction.atomic():
            self.artisan.business_name = "After Update"
            self.artisan.save()
            self.assertEqual(current_discovery_generation(), generation)
        self.assertGreater(current_discovery_generation(), generation)