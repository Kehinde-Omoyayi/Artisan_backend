import uuid
from django.db import migrations, models
import django.db.models.deletion

class Migration(migrations.Migration):
    dependencies = [("payments", "0005_payment_financial_constraints")]
    operations = [
        migrations.CreateModel(name="PaymentLedgerLine", fields=[("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)), ("account", models.CharField(max_length=60)), ("direction", models.CharField(choices=[("DEBIT", "Debit"), ("CREDIT", "Credit")], max_length=6)), ("amount_kobo", models.PositiveBigIntegerField()), ("currency", models.CharField(default="NGN", max_length=3)), ("created_at", models.DateTimeField(auto_now_add=True)), ("entry", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="lines", to="payments.paymentledgerentry"))]),
        migrations.AddConstraint(model_name="paymentledgerline", constraint=models.CheckConstraint(condition=models.Q(amount_kobo__gt=0), name="payment_ledger_line_positive")),
        migrations.AddIndex(model_name="paymentledgerline", index=models.Index(fields=["entry", "account"], name="payment_ledger_line_entry_idx")),
    ]
