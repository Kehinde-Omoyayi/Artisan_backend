from django.db import migrations, models
from django.db.models import Q, F
from decimal import Decimal

class Migration(migrations.Migration):
    dependencies = [("payments", "0004_paymentledgerentry")]
    operations = [
        migrations.AddConstraint(model_name="escrowpayment", constraint=models.CheckConstraint(condition=Q(service_amount_kobo__gt=0), name="payment_service_positive")),
        migrations.AddConstraint(model_name="escrowpayment", constraint=models.CheckConstraint(condition=Q(total_amount_kobo=F("service_amount_kobo") + F("customer_fee_kobo")), name="payment_total_matches_parts")),
        migrations.AddConstraint(model_name="escrowpayment", constraint=models.CheckConstraint(condition=Q(platform_commission_kobo__lte=F("service_amount_kobo")), name="payment_commission_within_service")),
        migrations.AddConstraint(model_name="escrowpayment", constraint=models.CheckConstraint(condition=Q(artisan_payout_kobo__lte=F("service_amount_kobo")), name="payment_payout_within_service")),
        migrations.AddConstraint(model_name="escrowpayment", constraint=models.CheckConstraint(condition=Q(platform_commission_kobo=F("service_amount_kobo") - F("artisan_payout_kobo")), name="payment_service_matches_payout")),
        migrations.AddConstraint(model_name="escrowpayment", constraint=models.CheckConstraint(condition=Q(commission_rate__gte=0) & Q(commission_rate__lte=Decimal("0.1000")), name="payment_commission_rate_bounds")),
    ]
