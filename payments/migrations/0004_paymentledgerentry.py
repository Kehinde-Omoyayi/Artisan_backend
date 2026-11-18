import uuid
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion

class Migration(migrations.Migration):
    dependencies=[("payments","0003_alter_escrowpayment_status")]
    operations=[
        migrations.CreateModel(name="PaymentLedgerEntry", fields=[("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)), ("event_key", models.CharField(max_length=255, unique=True)), ("event_type", models.CharField(max_length=80)), ("status_before", models.CharField(blank=True, max_length=30)), ("status_after", models.CharField(blank=True, max_length=30)), ("amount_kobo", models.PositiveBigIntegerField(blank=True, null=True)), ("metadata", models.JSONField(blank=True, default=dict)), ("created_at", models.DateTimeField(auto_now_add=True)), ("actor", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="payment_ledger_actions", to=settings.AUTH_USER_MODEL)), ("payment", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="ledger_entries", to="payments.escrowpayment"))]),
        migrations.AddIndex(model_name="paymentledgerentry", index=models.Index(fields=["payment", "created_at"], name="payment_ledger_created_idx")),
        migrations.AddIndex(model_name="paymentledgerentry", index=models.Index(fields=["event_type", "created_at"], name="payment_ledger_type_idx")),
    ]
