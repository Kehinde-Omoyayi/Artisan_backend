"""Payable total now depends on whether materials are included in the price.

Included: total = labour + transport + additional.
Charged separately: total = labour + materials + transport + additional.
"""

import django.db.models.expressions
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('conversations', '0014_offer_itemized_only'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='offer',
            name='offer_total_matches_parts',
        ),
        migrations.AddConstraint(
            model_name='offer',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('materials_included', True), ('total_amount', django.db.models.expressions.CombinedExpression(django.db.models.expressions.CombinedExpression(models.F('labour_amount'), '+', models.F('transport_amount')), '+', models.F('additional_amount')))), models.Q(('materials_included', False), ('total_amount', django.db.models.expressions.CombinedExpression(django.db.models.expressions.CombinedExpression(django.db.models.expressions.CombinedExpression(models.F('labour_amount'), '+', models.F('materials_amount')), '+', models.F('transport_amount')), '+', models.F('additional_amount')))), _connector='OR'), name='offer_total_matches_parts'),
        ),
    ]
