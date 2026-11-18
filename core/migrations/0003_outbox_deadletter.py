from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [("core", "0002_audit_request_metadata")]
    operations = [migrations.AddField(model_name="outboxevent", name="dead_lettered_at", field=models.DateTimeField(blank=True, null=True))]
