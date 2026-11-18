import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('booking', '0003_remove_booking_booking_nonnegative_price_and_more'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='SupportCase',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('reference', models.CharField(editable=False, max_length=32, unique=True)),
                ('case_type', models.CharField(choices=[('DISPUTE', 'Dispute'), ('REPORT', 'Report'), ('PAYMENT', 'Payment'), ('ACCOUNT', 'Account'), ('TECHNICAL', 'Technical'), ('OTHER', 'Other')], max_length=30)),
                ('status', models.CharField(choices=[('OPEN', 'Open'), ('UNDER_REVIEW', 'Under review'), ('RESOLVED', 'Resolved'), ('CLOSED', 'Closed')], default='OPEN', max_length=20)),
                ('priority', models.CharField(choices=[('LOW', 'Low'), ('MEDIUM', 'Medium'), ('HIGH', 'High'), ('URGENT', 'Urgent')], default='MEDIUM', max_length=20)),
                ('subject', models.CharField(max_length=200)),
                ('description', models.TextField()),
                ('resolution_code', models.CharField(blank=True, choices=[('REFUND', 'Refund'), ('PAYOUT', 'Payout'), ('PARTIAL_REFUND', 'Partial refund'), ('PARTIAL_PAYOUT', 'Partial payout'), ('REBOOK', 'Rebook'), ('REPERFORM', 'Re-perform service'), ('NO_ADJUSTMENT', 'No financial adjustment'), ('POLICY_ACTION', 'Policy action'), ('OTHER', 'Other')], max_length=30, null=True)),
                ('resolution_note', models.TextField(blank=True)),
                ('resolved_at', models.DateTimeField(blank=True, null=True)),
                ('closed_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('assigned_to', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='support_cases_assigned', to=settings.AUTH_USER_MODEL)),
                ('booking', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='support_cases', to='booking.booking')),
                ('opened_by', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='support_cases_opened', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.CreateModel(
            name='SupportCaseEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('action', models.CharField(max_length=50)),
                ('note', models.TextField(blank=True)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('case', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='events', to='support.supportcase')),
                ('performed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='support_case_events', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='supportcase',
            index=models.Index(fields=['status', 'priority'], name='support_sup_status_f650af_idx'),
        ),
        migrations.AddIndex(
            model_name='supportcase',
            index=models.Index(fields=['opened_by', 'status'], name='support_sup_opened__9542c8_idx'),
        ),
        migrations.AddIndex(
            model_name='supportcase',
            index=models.Index(fields=['booking', 'status'], name='support_sup_booking_b84661_idx'),
        ),
        migrations.AddIndex(
            model_name='supportcase',
            index=models.Index(fields=['assigned_to', 'status'], name='support_sup_assigne_a23abb_idx'),
        ),
    ]
