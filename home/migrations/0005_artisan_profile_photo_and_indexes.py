import django.db.models
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("home", "0004_alter_customuser_phone_number"),
    ]

    operations = [
        migrations.AddField(
            model_name="artisanprofile",
            name="profile_photo",
            field=models.ImageField(blank=True, null=True, upload_to="artisan_profiles/%Y/%m/"),
        ),
        migrations.AlterField(
            model_name="customuser",
            name="phone_number",
            field=models.CharField(blank=True, max_length=20, null=True, unique=True),
        ),
        migrations.RemoveConstraint(
            model_name="artisanservice",
            name="unique_artisan_service_location",
        ),
        migrations.AddIndex(
            model_name="artisanprofile",
            index=models.Index(fields=["profile_status", "-average_rating"], name="artisan_status_rating_idx"),
        ),
        migrations.AddConstraint(
            model_name="artisanservice",
            constraint=models.UniqueConstraint(
                condition=django.db.models.Q(("location__isnull", True)),
                fields=("artisan", "service"),
                name="unique_artisan_general_service",
            ),
        ),
        migrations.AddConstraint(
            model_name="artisanservice",
            constraint=models.UniqueConstraint(
                condition=django.db.models.Q(("location__isnull", False)),
                fields=("artisan", "service", "location"),
                name="unique_artisan_service_location",
            ),
        ),
    ]
