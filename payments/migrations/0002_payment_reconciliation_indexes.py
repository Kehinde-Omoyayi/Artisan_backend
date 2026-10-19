from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0001_initial"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="escrowpayment",
            index=models.Index(fields=["status", "updated_at"], name="payment_status_updated_idx"),
        ),
        migrations.AddIndex(
            model_name="escrowpayment",
            index=models.Index(fields=["provider_transaction_id"], name="payment_provider_tx_idx"),
        ),
        migrations.AddIndex(
            model_name="escrowpayment",
            index=models.Index(fields=["provider_reference"], name="payment_provider_ref_idx"),
        ),
        migrations.AddIndex(
            model_name="escrowrelease",
            index=models.Index(fields=["status", "updated_at"], name="release_status_updated_idx"),
        ),
        migrations.AddIndex(
            model_name="escrowrelease",
            index=models.Index(fields=["provider_transfer_id"], name="release_provider_tx_idx"),
        ),
        migrations.AddIndex(
            model_name="paymentwebhookevent",
            index=models.Index(fields=["provider", "processed_at"], name="wh_provider_processed_idx"),
        ),
    ]
