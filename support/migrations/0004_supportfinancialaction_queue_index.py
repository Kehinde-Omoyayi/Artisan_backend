from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("support", "0003_supportcaseevent_case_created_index"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="supportfinancialaction",
            index=models.Index(
                fields=["status", "-requested_at"],
                name="sup_fin_status_requested_idx",
            ),
        ),
    ]
