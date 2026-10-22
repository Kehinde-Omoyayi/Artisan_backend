from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("conversations", "0002_conversation_draft_constraint")]
    operations = [
        migrations.AddField(
            model_name="conversationattachment",
            name="scan_status",
            field=models.CharField(
                choices=[
                    ("PENDING", "Pending"),
                    ("CLEAN", "Clean"),
                    ("INFECTED", "Infected"),
                    ("FAILED", "Failed"),
                ],
                db_index=True,
                default="PENDING",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="conversationattachment",
            name="scan_reason",
            field=models.CharField(blank=True, max_length=500),
        ),
    ]
