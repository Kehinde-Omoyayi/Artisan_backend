from django.test import TestCase, TransactionTestCase
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from home.models import CustomUser, ArtisanProfile, CustomerProfile
from profiles.serializers import ArtisanProfileSerializer, CustomerProfileSerializer

NAME = "Federal University of Agriculture, Abeokuta"


class TextInstitutionTests(TestCase):
    def setUp(self):
        self.user = CustomUser.objects.create_user(
            email="snapshot@example.invalid",
            first_name="Demo",
            last_name="Profile",
            password=None,
        )

    def test_both_profile_types_save_text_without_catalogue_rows(self):
        for model, serializer in [
            (ArtisanProfile, ArtisanProfileSerializer),
            (CustomerProfile, CustomerProfileSerializer),
        ]:
            with self.subTest(model=model.__name__):
                profile = model.objects.create(user=self.user)
                field = model._meta.get_field("nearest_institution")
                self.assertFalse(field.is_relation)
                data = dict(
                    country="Nigeria",
                    state="Ogun",
                    area="Abeokuta",
                    nearest_institution=NAME,
                    nearest_institution_abbreviation="FUNAAB",
                )
                form = serializer(profile, data=data, partial=True)
                self.assertTrue(form.is_valid(), form.errors)
                form.save()
                profile.refresh_from_db()
                self.assertEqual(profile.nearest_institution, NAME)
                self.assertEqual(
                    serializer(profile).data["nearest_institution_name"], NAME
                )

    def test_parent_change_clears_stale_selection(self):
        profile = CustomerProfile.objects.create(
            user=self.user,
            country="Nigeria",
            state="Ogun",
            area="Abeokuta",
            nearest_institution=NAME,
            nearest_institution_abbreviation="FUNAAB",
        )
        form = CustomerProfileSerializer(
            profile, data={"state": "Lagos", "area": "Ikeja"}, partial=True
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        profile.refresh_from_db()
        self.assertEqual(profile.nearest_institution, "")
        self.assertEqual(profile.nearest_institution_abbreviation, "")

    def test_uuid_and_missing_location_are_rejected(self):
        profile = CustomerProfile.objects.create(user=self.user)
        for value in ["01832a11-5053-411f-9055-68519e6b3c51", NAME]:
            form = CustomerProfileSerializer(
                profile, data={"nearest_institution": value}, partial=True
            )
            self.assertFalse(form.is_valid())
            self.assertIn("nearest_institution", form.errors)

    def test_null_clears_text_and_abbreviation(self):
        profile = CustomerProfile.objects.create(
            user=self.user,
            nearest_institution=NAME,
            nearest_institution_abbreviation="FUNAAB",
        )
        form = CustomerProfileSerializer(
            profile, data={"nearest_institution": None}, partial=True
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        profile.refresh_from_db()
        self.assertEqual(profile.nearest_institution, "")
        self.assertEqual(profile.nearest_institution_abbreviation, "")


class ProfileSnapshotMigrationTests(TransactionTestCase):
    def test_forward_and_reverse_preserve_existing_choice(self):
        old_target = [("home", "0015_artisanprofile_nearest_institution")]
        new_target = [("home", "0016_profile_institution_text_snapshots")]
        executor = MigrationExecutor(connection)
        executor.migrate(old_target)
        old = executor.loader.project_state(old_target).apps
        try:
            location = old.get_model("home", "Locations").objects.create(
                country="Nigeria", state="Ogun", area="Abeokuta"
            )
            institution = old.get_model("home", "Institution").objects.create(
                name=NAME, abbreviation="FUNAAB", location=location
            )
            ids = []
            for kind in ["ArtisanProfile", "CustomerProfile"]:
                user = old.get_model("home", "CustomUser").objects.create(
                    email=kind + "@example.invalid",
                    first_name="Demo",
                    last_name="Profile",
                )
                profile = old.get_model("home", kind).objects.create(
                    user=user,
                    country="Nigeria",
                    state="Ogun",
                    area="Abeokuta",
                    nearest_institution=institution,
                )
                ids.append((kind, profile.pk))
            executor = MigrationExecutor(connection)
            executor.migrate(new_target)
            current = executor.loader.project_state(new_target).apps
            for kind, pk in ids:
                row = current.get_model("home", kind).objects.get(pk=pk)
                self.assertEqual(row.nearest_institution, NAME)
                self.assertEqual(row.nearest_institution_abbreviation, "FUNAAB")
                self.assertEqual(row.legacy_institution_id, institution.pk)
            executor = MigrationExecutor(connection)
            executor.migrate(old_target)
            restored = executor.loader.project_state(old_target).apps
            for kind, pk in ids:
                self.assertEqual(
                    restored.get_model("home", kind)
                    .objects.get(pk=pk)
                    .nearest_institution_id,
                    institution.pk,
                )
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes())
