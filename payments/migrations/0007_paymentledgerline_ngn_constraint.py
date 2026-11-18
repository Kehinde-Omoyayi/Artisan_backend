from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [("payments", "0006_paymentledgerline")]
    operations = [migrations.AddConstraint(model_name="paymentledgerline", constraint=models.CheckConstraint(condition=models.Q(currency="NGN"), name="payment_ledger_line_ngn_only"))]
