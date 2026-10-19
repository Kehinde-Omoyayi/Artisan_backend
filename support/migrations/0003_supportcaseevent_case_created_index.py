from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("support", "0002_supportfinancialaction")]

    operations = [
        migrations.AddIndex(
            model_name="supportcaseevent",
            index=models.Index(
                fields=["case", "created_at"],
                name="support_event_case_created_idx",
            ),
        ),
    ]
