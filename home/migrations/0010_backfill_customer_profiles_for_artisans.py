from django.db import migrations


def create_customer_profiles_for_existing_artisans(apps, schema_editor):
    database = schema_editor.connection.alias
    ArtisanProfile = apps.get_model("home", "ArtisanProfile")
    CustomerProfile = apps.get_model("home", "CustomerProfile")
    artisan_user_ids = ArtisanProfile.objects.using(database).values_list("user_id", flat=True)
    existing_customer_user_ids = CustomerProfile.objects.using(database).values_list("user_id", flat=True)
    missing_user_ids = artisan_user_ids.exclude(user_id__in=existing_customer_user_ids)
    CustomerProfile.objects.using(database).bulk_create(
        [CustomerProfile(user_id=user_id) for user_id in missing_user_ids.iterator()],
        ignore_conflicts=True,
        batch_size=1000,
    )


class Migration(migrations.Migration):

    dependencies = [
        ("home", "0009_customerprofile_profile_photo"),
    ]

    operations = [
        migrations.RunPython(
            create_customer_profiles_for_existing_artisans,
            migrations.RunPython.noop,
        ),
    ]
