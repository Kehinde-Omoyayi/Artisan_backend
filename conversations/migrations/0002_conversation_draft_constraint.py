from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0001_initial"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="conversation",
            name="conversation_customer_artisan_unique",
        ),
        migrations.AddConstraint(
            model_name="conversation",
            constraint=models.UniqueConstraint(
                condition=Q(service_request__isnull=True),
                fields=("customer", "artisan"),
                name="conversation_one_draft_per_pair",
            ),
        ),
    ]
