from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("conversations", "0013_backfill_offer_durations"),
    ]

    operations = [
        # The itemised pricing form is the only pricing surface. These free-text
        # fields were required by the old quote form and are gone from the
        # interface, the serializer and the validation, so they are removed here
        # as well instead of lingering as writable dead weight.
        migrations.RemoveField(model_name="offer", name="materials_details"),
        migrations.RemoveField(model_name="offer", name="payment_terms"),
        migrations.RemoveField(model_name="offer", name="exclusions"),
        migrations.RemoveField(model_name="offer", name="workmanship_terms"),
        migrations.RemoveField(model_name="offer", name="scope"),
        migrations.RemoveField(model_name="offer", name="notes"),
        migrations.AlterField(
            model_name="offer",
            name="expires_at",
            field=models.DateTimeField(),
        ),
    ]
