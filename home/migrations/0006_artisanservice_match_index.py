from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("home", "0005_artisan_profile_photo_and_indexes")]

    operations = [
        migrations.AddIndex(
            model_name="artisanservice",
            index=models.Index(
                fields=["service", "location", "artisan"],
                name="artisan_service_match_idx",
            ),
        ),
    ]
