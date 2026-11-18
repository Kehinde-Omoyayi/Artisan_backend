from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [("core", "0001_initial")]
    operations = [migrations.AddField(model_name="auditlog", name="ip_hash", field=models.CharField(blank=True, max_length=64)), migrations.AddField(model_name="auditlog", name="user_agent_hash", field=models.CharField(blank=True, max_length=64))]
