from django.db import migrations, models
from django.db.models import Q

class Migration(migrations.Migration):
    dependencies = [("support", "0004_supportfinancialaction_queue_index")]
    operations = [migrations.AddConstraint(model_name="supportcase", constraint=models.UniqueConstraint(fields=["booking", "case_type"], condition=Q(case_type="DISPUTE", status__in=["OPEN", "UNDER_REVIEW"]), name="support_one_active_dispute_per_booking"))]
