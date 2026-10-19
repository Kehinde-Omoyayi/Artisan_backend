import django.db.models.deletion
import uuid
from django.db import migrations, models

class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('home', '0004_alter_customuser_phone_number'),
        ('services', '__first__'),
    ]

    operations = [
        migrations.CreateModel(
            name='Booking',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('scheduled_time_start', models.DateTimeField()),
                ('scheduled_time_end', models.DateTimeField()),
                ('price_agreed', models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ('status', models.CharField(choices=[('REQUESTED', 'Requested'), ('ACCEPTED', 'Accepted'), ('SCHEDULED', 'Scheduled'), ('IN_PROGRESS', 'In Progress'), ('COMPLETED', 'Completed'), ('CANCELLED', 'Cancelled'), ('DISPUTED', 'Disputed'), ('REJECTED', 'Rejected')], db_index=True, default='REQUESTED', max_length=20)),
                ('source', models.CharField(choices=[('WEB', 'Web'), ('MOBILE', 'Mobile'), ('WHATSAPP', 'WhatsApp'), ('ADMIN', 'Admin')], default='WEB', max_length=20)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('artisan', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='bookings', to='home.artisanprofile')),
                ('customer', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='bookings', to='home.customerprofile')),
                ('request', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='bookings', to='services.servicerequest')),
            ],
        ),
    ]
