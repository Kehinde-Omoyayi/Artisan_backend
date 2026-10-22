"""Preserve names/abbreviations before removing profile catalogue foreign keys.

Shared catalogue tables remain for other apps until their own reference migration.
"""

from django.db import migrations, models


def snapshot(apps, schema_editor):
    alias = schema_editor.connection.alias
    for model_name in ("ArtisanProfile", "CustomerProfile"):
        model = apps.get_model("home", model_name)
        for profile in (
            model.objects.using(alias)
            .select_related("nearest_institution")
            .iterator(chunk_size=500)
        ):
            old = profile.nearest_institution
            if old is not None:
                profile.institution_text = old.name
                profile.nearest_institution_abbreviation = old.abbreviation or next(
                    iter(old.abbreviations or []), ""
                )
                profile.legacy_institution_id = old.pk
                profile.save(
                    using=alias,
                    update_fields=[
                        "institution_text",
                        "nearest_institution_abbreviation",
                        "legacy_institution_id",
                    ],
                )


def restore(apps, schema_editor):
    alias = schema_editor.connection.alias
    institution = apps.get_model("home", "Institution")
    for model_name in ("ArtisanProfile", "CustomerProfile"):
        model = apps.get_model("home", model_name)
        for profile in (
            model.objects.using(alias)
            .exclude(legacy_institution_id=None)
            .iterator(chunk_size=500)
        ):
            if (
                institution.objects.using(alias)
                .filter(pk=profile.legacy_institution_id)
                .exists()
            ):
                profile.nearest_institution_id = profile.legacy_institution_id
                profile.save(using=alias, update_fields=["nearest_institution"])


class Migration(migrations.Migration):
    dependencies = [("home", "0015_artisanprofile_nearest_institution")]
    operations = []
    for model in ("artisanprofile", "customerprofile"):
        operations.extend(
            [
                migrations.AddField(
                    model_name=model,
                    name="institution_text",
                    field=models.CharField(max_length=180, blank=True, default=""),
                ),
                migrations.AddField(
                    model_name=model,
                    name="nearest_institution_abbreviation",
                    field=models.CharField(max_length=160, blank=True, default=""),
                ),
                migrations.AddField(
                    model_name=model,
                    name="legacy_institution_id",
                    field=models.UUIDField(null=True, blank=True, editable=False),
                ),
            ]
        )
    operations.append(migrations.RunPython(snapshot, restore))
    for model in ("artisanprofile", "customerprofile"):
        operations.extend(
            [
                migrations.RemoveField(model_name=model, name="nearest_institution"),
                migrations.RenameField(
                    model_name=model,
                    old_name="institution_text",
                    new_name="nearest_institution",
                ),
            ]
        )
