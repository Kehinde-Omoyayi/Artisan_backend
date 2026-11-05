from django.test import SimpleTestCase
from frontend.forms import CustomerProfileForm, ArtisanProfileForm
from frontend.profile_catalogue import institutions, DEFAULTS


class FrontendProfileCatalogueTests(SimpleTestCase):
    def test_empty_profiles_have_full_default_chain(self):
        for cls in [CustomerProfileForm, ArtisanProfileForm]:
            form = cls()
            for key, value in DEFAULTS.items():
                self.assertEqual(form.initial[key], value)

    def test_saved_and_bound_values_are_not_defaulted(self):
        for cls in [CustomerProfileForm, ArtisanProfileForm]:
            form = cls(
                initial=dict(
                    country="Nigeria",
                    state="Lagos",
                    area="Ikeja",
                    nearest_institution="Saved College",
                )
            )
            self.assertEqual(form.initial["area"], "Ikeja")
            self.assertEqual(form.initial["nearest_institution"], "Saved College")
            bound = cls(
                data={"country": "", "state": "", "area": "", "nearest_institution": ""}
            )
            self.assertNotIn("nearest_institution", bound.initial)

    def payload(self):
        return dict(
            first_name="Demo",
            last_name="Profile",
            gender="N",
            country="Nigeria",
            state="Ogun",
            area="Abeokuta",
            nearest_institution=DEFAULTS["nearest_institution"],
            nearest_institution_abbreviation="TAMPERED",
        )

    def test_names_are_saved_and_abbreviation_is_canonical(self):
        for cls in [CustomerProfileForm, ArtisanProfileForm]:
            form = cls(data=self.payload())
            self.assertTrue(form.is_valid(), form.errors)
            self.assertEqual(
                form.cleaned_data["nearest_institution_abbreviation"], "FUNAAB"
            )
            self.assertEqual(
                form.cleaned_data["nearest_institution"],
                DEFAULTS["nearest_institution"],
            )

    def test_institution_selection_aligns_parent_values(self):
        data = self.payload()
        data.update(state="Lagos", area="Ikeja")
        form = CustomerProfileForm(data=data)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["state"], "Ogun")
        self.assertEqual(form.cleaned_data["area"], "Abeokuta")

    def test_static_country_shard_contains_sourced_funaab(self):
        rows = institutions("Nigeria")
        self.assertTrue(
            any(
                item["name"] == DEFAULTS["nearest_institution"]
                and item["abbreviations"] == ["FUNAAB"]
                for item in rows
            )
        )
