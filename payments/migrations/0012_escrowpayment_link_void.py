from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("payments", "0011_artisanpayoutaccount_payout_change_locked_until"),
    ]

    operations = [
        migrations.AddField(
            model_name="escrowpayment",
            name="link_voided_at",
            field=models.DateTimeField(null=True, blank=True),
        ),
        migrations.AddField(
            model_name="escrowpayment",
            name="link_void_reason",
            field=models.CharField(blank=True, max_length=200),
        ),
    ]
