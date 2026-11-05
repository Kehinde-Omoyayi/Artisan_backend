from unittest.mock import patch, Mock
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from frontend.forms import CustomerProfileForm, ArtisanProfileForm
from frontend.profile_catalogue import catalogue_page, DEFAULTS

FUN = "050s1zm26:2352947"


class GlobalCatalogueTests(SimpleTestCase):
    def test_search_with_no_geography(self):
        payload = catalogue_page({"q": "funaab"})
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["results"][0]["source_id"], FUN)

    def test_every_location_filter_is_optional(self):
        for params in [
            {"q": "funaab", "country": "Nigeria"},
            {"q": "funaab", "state": "Ogun"},
            {"q": "funaab", "area": "Abeokuta"},
        ]:
            self.assertEqual(catalogue_page(params)["count"], 1)
        self.assertEqual(
            catalogue_page({"q": "funaab", "country": "Canada"})["count"], 0
        )

    def test_pagination_and_missing_id(self):
        first = catalogue_page({})
        second = catalogue_page({"offset": "50"})
        self.assertEqual(first["count"], 30256)
        self.assertEqual(len(first["results"]), 50)
        self.assertTrue(
            {r["id"] for r in first["results"]}.isdisjoint(
                r["id"] for r in second["results"]
            )
        )
        self.assertEqual(catalogue_page({"id": "missing-source"})["results"], [])
        self.assertEqual(catalogue_page({"offset": "bad"})["offset"], 0)

    @patch(
        "frontend.views.APIClient",
        side_effect=AssertionError("Catalogue must not call backend"),
    )
    def test_frontend_route_is_not_backend_proxy(self, api):
        response = self.client.get(reverse("institution-catalogue"), {"q": "FUNAAB"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["source"], "frontend-json")
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual(
            self.client.post(reverse("institution-catalogue")).status_code, 405
        )

    def payload(self):
        return dict(
            first_name="Demo",
            last_name="Profile",
            gender="N",
            country="",
            state="",
            area="",
            nearest_institution=DEFAULTS["nearest_institution"],
            nearest_institution_source_id=FUN,
        )

    def test_both_profiles_accept_institution_first(self):
        for cls in [CustomerProfileForm, ArtisanProfileForm]:
            form = cls(data=self.payload())
            self.assertTrue(form.is_valid(), form.errors)
            self.assertEqual(form.cleaned_data["country"], "Nigeria")
            self.assertEqual(form.cleaned_data["state"], "Ogun")
            self.assertEqual(form.cleaned_data["area"], "Abeokuta")
            self.assertEqual(
                form.cleaned_data["nearest_institution_abbreviation"], "FUNAAB"
            )

    def test_selection_overrides_conflicting_location(self):
        data = self.payload()
        data.update(country="Canada", state="Ontario", area="Toronto")
        form = CustomerProfileForm(data=data)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["country"], "Nigeria")
        self.assertEqual(form.cleaned_data["area"], "Abeokuta")

    def test_source_key_cannot_be_swapped_for_another_name(self):
        data = self.payload()
        data["nearest_institution"] = "Different university"
        form = CustomerProfileForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertIn("nearest_institution", form.errors)

    def test_source_key_is_internal_not_an_api_field(self):
        from frontend.views import cleaned_payload

        self.assertNotIn(
            "nearest_institution_source_id",
            cleaned_payload(
                {"nearest_institution_source_id": FUN, "nearest_institution": "Example"}
            ),
        )


class DiscoveryInstitutionTests(TestCase):
    def setUp(self):
        self.calls = []

        def get(path, params=None):
            self.calls.append((path, params or {}))
            return 200, {"count": 0, "results": []}, {}

        self.api = Mock()
        self.api.get.side_effect = get

    def request(self, params):
        with patch("frontend.views.APIClient", return_value=self.api), patch(
            "frontend.views._public_promoted_discovery", return_value=[]
        ):
            return self.client.get(reverse("artisans"), params)

    def test_discovery_uses_json_and_translates_source_to_locality(self):
        response = self.request(
            {
                "institution_id": FUN,
                "country": "Canada",
                "state": "Ontario",
                "area": "Toronto",
                "q": "plumb",
            }
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["params"]["country"], "Nigeria")
        self.assertEqual(response.context["params"]["area"], "Abeokuta")
        query = next(
            params for path, params in self.calls if path == "/profile/artisan/details/"
        )
        self.assertEqual(query["q"], "plumb")
        self.assertEqual(query["country"], "Nigeria")
        self.assertNotIn("institution_id", query)
        self.assertFalse(
            any(
                path in ["/services/institutions/", "/services/locations/"]
                for path, _ in self.calls
            )
        )
        self.assertContains(response, "FUNAAB")
        self.assertContains(response, "Search institutions worldwide")

    def test_invalid_source_does_not_broaden_to_all_artisans(self):
        response = self.request({"institution_id": "bad-source-key"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            any(path == "/profile/artisan/details/" for path, _ in self.calls)
        )
        self.assertEqual(response.context["items"], [])

    def test_legacy_uuid_link_keeps_legacy_api_contract(self):
        identifier = "12345678-1234-4321-8321-123456789abc"
        response = self.request({"institution_id": identifier})
        self.assertEqual(response.status_code, 200)
        query = next(
            params for path, params in self.calls if path == "/profile/artisan/details/"
        )
        self.assertEqual(query["institution_id"], identifier)
