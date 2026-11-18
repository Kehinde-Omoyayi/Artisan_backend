from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("services", "0003_artisanverification_verified_identity_name_encrypted"),
    ]

    operations = [
        migrations.AddField(
            model_name="servicerequest",
            name="preferred_time_start",
            field=models.TimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="servicerequest",
            name="preferred_time_end",
            field=models.TimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="servicerequest",
            name="preferred_time_note",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="servicerequest",
            name="additional_details",
            field=models.TextField(blank=True, default=""),
        ),
    ]
