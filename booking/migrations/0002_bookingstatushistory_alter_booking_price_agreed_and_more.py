import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models

class Migration(migrations.Migration):

    dependencies = [
        ('booking', '0001_initial'),
        ('home', '0004_alter_customuser_phone_number'),
        ('services', '__first__'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='BookingStatusHistory',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('status', models.CharField(choices=[('REQUESTED', 'Requested'), ('ACCEPTED', 'Accepted'), ('IN_PROGRESS', 'In Progress'), ('COMPLETED', 'Completed'), ('CANCELLED', 'Cancelled'), ('REJECTED', 'Rejected'), ('DISPUTED', 'Disputed')], max_length=20)),
                ('changed_by', models.CharField(choices=[('CUSTOMER', 'Customer'), ('ARTISAN', 'Artisan'), ('ADMIN', 'Admin'), ('SYSTEM', 'System')], max_length=20)),
                ('notes', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'ordering': ['created_at'],
            },
        ),
        migrations.AlterField(
            model_name='booking',
            name='price_agreed',
            field=models.DecimalField(decimal_places=2, max_digits=12),
        ),
        migrations.AlterField(
            model_name='booking',
            name='status',
            field=models.CharField(choices=[('REQUESTED', 'Requested'), ('ACCEPTED', 'Accepted'), ('IN_PROGRESS', 'In Progress'), ('COMPLETED', 'Completed'), ('CANCELLED', 'Cancelled'), ('REJECTED', 'Rejected'), ('DISPUTED', 'Disputed')], db_index=True, default='REQUESTED', max_length=20),
        ),
        migrations.AddIndex(
            model_name='booking',
            index=models.Index(fields=['status', 'scheduled_time_start'], name='booking_status_start_idx'),
        ),
        migrations.AddIndex(
            model_name='booking',
            index=models.Index(fields=['customer', 'status'], name='booking_customer_status_idx'),
        ),
        migrations.AddIndex(
            model_name='booking',
            index=models.Index(fields=['artisan', 'status'], name='booking_artisan_status_idx'),
        ),
        migrations.AddConstraint(
            model_name='booking',
            constraint=models.CheckConstraint(condition=models.Q(('scheduled_time_end__gt', models.F('scheduled_time_start'))), name='booking_valid_schedule'),
        ),
        migrations.AddConstraint(
            model_name='booking',
            constraint=models.CheckConstraint(condition=models.Q(('price_agreed__gte', 0)), name='booking_nonnegative_price'),
        ),
        migrations.AddConstraint(
            model_name='booking',
            constraint=models.UniqueConstraint(condition=models.Q(('status__in', ['REQUESTED', 'ACCEPTED', 'IN_PROGRESS'])), fields=('request',), name='one_active_booking_per_request'),
        ),
        migrations.AddField(
            model_name='bookingstatushistory',
            name='booking',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='status_history', to='booking.booking'),
        ),
        migrations.AddField(
            model_name='bookingstatushistory',
            name='changed_by_user',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='booking_status_changes', to=settings.AUTH_USER_MODEL),
        ),
    ]
