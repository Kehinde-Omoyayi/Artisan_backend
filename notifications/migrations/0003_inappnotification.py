from django.db import migrations, models
from django.conf import settings
import django.db.models.deletion

class Migration(migrations.Migration):
    dependencies = [("notifications", "0002_delete_notification")]
    operations = [migrations.CreateModel(name="InAppNotification", fields=[("id", models.BigAutoField(primary_key=True, serialize=False)), ("event_key", models.CharField(max_length=255)), ("title", models.CharField(max_length=255)), ("message", models.TextField(max_length=5000)), ("data", models.JSONField(blank=True, default=dict)), ("read_at", models.DateTimeField(blank=True, null=True)), ("created_at", models.DateTimeField(auto_now_add=True)), ("recipient", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="in_app_notifications", to=settings.AUTH_USER_MODEL))]), migrations.AddConstraint(model_name="inappnotification", constraint=models.UniqueConstraint(fields=["recipient", "event_key"], name="inapp_notification_recipient_event_unique")), migrations.AddIndex(model_name="inappnotification", index=models.Index(fields=["recipient", "read_at", "created_at"], name="inapp_notification_inbox_idx"))]
