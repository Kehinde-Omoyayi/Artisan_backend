import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models

class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('booking', '0003_remove_booking_booking_nonnegative_price_and_more'),
        ('home', '0004_alter_customuser_phone_number'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='PaymentWebhookEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('provider', models.CharField(max_length=50)),
                ('event_key', models.CharField(max_length=255, unique=True)),
                ('event_type', models.CharField(blank=True, max_length=100)),
                ('payload', models.JSONField(default=dict)),
                ('received_at', models.DateTimeField(auto_now_add=True)),
                ('processed_at', models.DateTimeField(blank=True, null=True)),
            ],
        ),
        migrations.CreateModel(
            name='ArtisanPayoutAccount',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('bank_code', models.CharField(max_length=20)),
                ('account_number_last4', models.CharField(max_length=4)),
                ('account_name', models.CharField(max_length=150)),
                ('provider_beneficiary_id', models.PositiveBigIntegerField(blank=True, null=True)),
                ('provider_subaccount_id', models.CharField(blank=True, max_length=100)),
                ('status', models.CharField(choices=[('PENDING', 'Pending'), ('VERIFIED', 'Verified'), ('SUSPENDED', 'Suspended')], default='PENDING', max_length=20)),
                ('is_active', models.BooleanField(default=False)),
                ('is_verified', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('artisan', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name='payout_account', to='home.artisanprofile')),
            ],
        ),
        migrations.CreateModel(
            name='EscrowPayment',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('reference', models.CharField(max_length=150, unique=True)),
                ('provider_transaction_id', models.CharField(blank=True, max_length=100, null=True)),
                ('provider_reference', models.CharField(blank=True, max_length=150)),
                ('provider_status', models.CharField(blank=True, max_length=50)),
                ('service_amount_kobo', models.PositiveBigIntegerField()),
                ('customer_fee_kobo', models.PositiveBigIntegerField(default=15000)),
                ('total_amount_kobo', models.PositiveBigIntegerField()),
                ('commission_rate', models.DecimalField(decimal_places=4, default=0.1, max_digits=5)),
                ('platform_commission_kobo', models.PositiveBigIntegerField()),
                ('artisan_payout_kobo', models.PositiveBigIntegerField()),
                ('status', models.CharField(choices=[('PAYMENT_PENDING', 'Payment Pending'), ('HELD', 'Held'), ('DISPUTED', 'Disputed'), ('RELEASE_PENDING', 'Release Pending'), ('RELEASED', 'Released'), ('REFUND_PENDING', 'Refund Pending'), ('REFUNDED', 'Refunded')], db_index=True, default='PAYMENT_PENDING', max_length=30)),
                ('provider_payload', models.JSONField(blank=True, default=dict)),
                ('verification_payload', models.JSONField(blank=True, default=dict)),
                ('verified_at', models.DateTimeField(blank=True, null=True)),
                ('held_at', models.DateTimeField(blank=True, null=True)),
                ('release_requested_at', models.DateTimeField(blank=True, null=True)),
                ('released_at', models.DateTimeField(blank=True, null=True)),
                ('failure_reason', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('artisan', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='artisan_escrow_payments', to='home.artisanprofile')),
                ('booking', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name='escrow_payment', to='booking.booking')),
                ('customer', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='customer_escrow_payments', to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name='EscrowRelease',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('reference', models.CharField(max_length=150, unique=True)),
                ('amount_kobo', models.PositiveBigIntegerField()),
                ('status', models.CharField(choices=[('PENDING', 'Pending'), ('SUBMITTED', 'Submitted'), ('SUCCESSFUL', 'Successful'), ('FAILED', 'Failed')], db_index=True, default='PENDING', max_length=20)),
                ('provider_transfer_id', models.CharField(blank=True, max_length=100, null=True)),
                ('provider_reference', models.CharField(blank=True, max_length=150)),
                ('provider_payload', models.JSONField(blank=True, default=dict)),
                ('requested_at', models.DateTimeField(auto_now_add=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('failure_reason', models.TextField(blank=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('payment', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name='release', to='payments.escrowpayment')),
            ],
        ),
    ]
