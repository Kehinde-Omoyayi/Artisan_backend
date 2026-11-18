"""Catalogue behaviour: the world geography and the institution register.

These rules used to live in the browser app (and were tested there). They moved
with the data, so the search contract and the published endpoints are tested
here, against the same JSON the API serves.
"""

from django.test import SimpleTestCase

from catalogue import sources

FUN = "050s1zm26:2352947"


class CatalogueSearchTests(SimpleTestCase):
    def test_search_with_no_geography(self):
        payload = sources.catalogue_page({"q": "funaab"})
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["results"][0]["source_id"], FUN)

    def test_every_location_filter_is_optional(self):
        for params in [
            {"q": "funaab", "country": "Nigeria"},
            {"q": "funaab", "state": "Ogun"},
            {"q": "funaab", "area": "Abeokuta"},
        ]:
            self.assertEqual(sources.catalogue_page(params)["count"], 1)
        self.assertEqual(sources.catalogue_page({"q": "funaab", "country": "Canada"})["count"], 0)

    def test_pagination_and_missing_id(self):
        first = sources.catalogue_page({})
        second = sources.catalogue_page({"offset": "50"})
        self.assertEqual(first["count"], 30256)
        self.assertEqual(len(first["results"]), 50)
        self.assertTrue(
            {r["id"] for r in first["results"]}.isdisjoint(r["id"] for r in second["results"])
        )
        self.assertEqual(sources.catalogue_page({"id": "missing-source"})["results"], [])
        self.assertEqual(sources.catalogue_page({"offset": "bad"})["offset"], 0)

    def test_nigeria_comes_first_when_no_country_is_chosen(self):
        payload = sources.catalogue_page({})
        self.assertEqual(payload["results"][0]["country"], "Nigeria")

    def test_record_lookup_and_defaults(self):
        record = sources.institution_record(FUN)
        self.assertEqual(record["name"], "Federal University of Agriculture, Abeokuta")
        self.assertEqual(record["abbreviations"], ["FUNAAB"])
        self.assertIsNone(sources.institution_record(""))
        self.assertEqual(sources.DEFAULTS["nearest_institution"], record["name"])

    def test_canonical_location_returns_catalogue_spelling(self):
        self.assertEqual(
            sources.canonical_location("nigeria", "ogun", "abeokuta"),
            {"country": "Nigeria", "state": "Ogun", "area": "Abeokuta"},
        )
        self.assertIsNone(sources.canonical_location("Nigeria", "Ogun", "Nowhere"))


class CatalogueEndpointTests(SimpleTestCase):
    def test_geography_index_is_small_and_complete(self):
        response = self.client.get("/api/v1/catalogue/geography/index/")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertGreaterEqual(len(payload["countries"]), 250)
        self.assertGreaterEqual(len(payload["countries"]["Nigeria"]), 37)
        # The country URL carries a content stamp so republished data changes
        # the URL and a browser holding the old copy fetches the new one.
        self.assertRegex(payload["files"]["Nigeria"], r"/nigeria/\?v=\d+$")

    def test_country_areas_are_served_per_country(self):
        response = self.client.get("/api/v1/catalogue/geography/countries/nigeria/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Abia", response.json())
        self.assertTrue(response.json()["Abia"])
        self.assertEqual(
            self.client.get("/api/v1/catalogue/geography/countries/nope/").status_code, 404
        )

    def test_institutions_endpoint_pages_and_filters(self):
        response = self.client.get("/api/v1/catalogue/institutions/", {"q": "funaab"})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["limit"], 50)
        self.assertEqual(payload["results"][0]["id"], FUN)

    def test_institutions_endpoint_rejects_writes(self):
        self.assertEqual(self.client.post("/api/v1/catalogue/institutions/").status_code, 405)
