from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0002_payment_reconciliation_indexes"),
    ]

    operations = [
        migrations.AlterField(
            model_name="escrowpayment",
            name="status",
            field=models.CharField(choices=[
                ("PAYMENT_PENDING", "Payment Pending"),
                ("HELD", "Held"),
                ("DISPUTED", "Disputed"),
                ("RELEASE_PENDING", "Release Pending"),
                ("RELEASED", "Released"),
                ("REFUND_PENDING", "Refund Pending"),
                ("REFUNDED", "Refunded"),
                ("FAILED", "Failed"),
            ], db_index=True, default="PAYMENT_PENDING", max_length=30),
        ),
    ]
