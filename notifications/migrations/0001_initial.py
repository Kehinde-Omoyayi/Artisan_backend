import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models

class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='Notification',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('event_type', models.CharField(choices=[('SERVICE_REQUEST_CREATED', 'Service Request Created'), ('SERVICE_REQUEST_NO_MATCH', 'Service Request No Match'), ('BOOKING_CREATED', 'Booking Created'), ('BOOKING_ACCEPTED', 'Booking Accepted'), ('BOOKING_REJECTED', 'Booking Rejected'), ('BOOKING_CANCELLED', 'Booking Cancelled'), ('BOOKING_STARTED', 'Booking Started'), ('ARTISAN_COMPLETED', 'Artisan Completed'), ('CUSTOMER_COMPLETED', 'Customer Completed'), ('BOOKING_DISPUTED', 'Booking Disputed'), ('PAYMENT_INITIALIZED', 'Payment Initialized'), ('PAYMENT_HELD', 'Payment Held'), ('PAYMENT_FAILED', 'Payment Failed'), ('PAYOUT_PENDING', 'Payout Pending'), ('PAYOUT_SUCCESSFUL', 'Payout Successful'), ('PAYOUT_FAILED', 'Payout Failed'), ('VERIFICATION_SUBMITTED', 'Verification Submitted'), ('VERIFICATION_APPROVED', 'Verification Approved'), ('VERIFICATION_REJECTED', 'Verification Rejected'), ('ACCOUNT_SUSPENDED', 'Account Suspended'), ('ARTISAN_APPLICATION', 'Artisan Application'), ('NEW_REVIEW', 'New Review'), ('REFUND_INITIATED', 'Refund Initiated'), ('REFUND_COMPLETED', 'Refund Completed')], db_index=True, max_length=60)),
                ('channel', models.CharField(choices=[('EMAIL', 'Email'), ('WHATSAPP', 'WhatsApp'), ('IN_APP', 'In App')], db_index=True, default='EMAIL', max_length=20)),
                ('status', models.CharField(choices=[('QUEUED', 'Queued'), ('SENDING', 'Sending'), ('SENT', 'Sent'), ('FAILED', 'Failed')], db_index=True, default='QUEUED', max_length=20)),
                ('title', models.CharField(max_length=255)),
                ('message', models.TextField()),
                ('data', models.JSONField(blank=True, default=dict)),
                ('event_key', models.CharField(max_length=255)),
                ('service_request_id', models.UUIDField(blank=True, db_index=True, null=True)),
                ('booking_id', models.UUIDField(blank=True, db_index=True, null=True)),
                ('attempts', models.PositiveIntegerField(default=0)),
                ('sent_at', models.DateTimeField(blank=True, null=True)),
                ('failed_at', models.DateTimeField(blank=True, null=True)),
                ('last_error', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('recipient', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='notifications', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-created_at'],
                'indexes': [models.Index(fields=['recipient', 'status'], name='notificatio_recipie_e285de_idx'), models.Index(fields=['event_type', 'created_at'], name='notificatio_event_t_4562fd_idx')],
                'constraints': [models.UniqueConstraint(fields=('event_key', 'recipient', 'channel'), name='unique_notification_delivery')],
            },
        ),
    ]
