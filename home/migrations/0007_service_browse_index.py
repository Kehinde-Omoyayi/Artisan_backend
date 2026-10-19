from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("home", "0006_artisanservice_match_index"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="service",
            index=models.Index(
                fields=["is_active", "category", "name"],
                name="service_active_cat_name_idx",
            ),
        ),
    ]
