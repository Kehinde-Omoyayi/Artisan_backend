from django.db import migrations, models

class Migration(migrations.Migration):

    dependencies = [
        ('booking', '0002_bookingstatushistory_alter_booking_price_agreed_and_more'),
        ('home', '0004_alter_customuser_phone_number'),
        ('services', '__first__'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='booking',
            name='booking_nonnegative_price',
        ),
        migrations.RemoveConstraint(
            model_name='booking',
            name='one_active_booking_per_request',
        ),
        migrations.AddField(
            model_name='booking',
            name='artisan_completed_at',
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name='booking',
            name='customer_completed_at',
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name='booking',
            name='payout_eligible_at',
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AlterField(
            model_name='booking',
            name='source',
            field=models.CharField(choices=[('WEB', 'Web'), ('MOBILE', 'Mobile'), ('ADMIN', 'Admin')], default='WEB', max_length=20),
        ),
        migrations.AlterField(
            model_name='booking',
            name='status',
            field=models.CharField(choices=[('REQUESTED', 'Requested'), ('ACCEPTED', 'Accepted'), ('IN_PROGRESS', 'In Progress'), ('COMPLETION_REQUESTED', 'Completion Requested'), ('COMPLETED', 'Completed'), ('CANCELLED', 'Cancelled'), ('REJECTED', 'Rejected'), ('DISPUTED', 'Disputed')], db_index=True, default='REQUESTED', max_length=25),
        ),
        migrations.AlterField(
            model_name='bookingstatushistory',
            name='status',
            field=models.CharField(choices=[('REQUESTED', 'Requested'), ('ACCEPTED', 'Accepted'), ('IN_PROGRESS', 'In Progress'), ('COMPLETION_REQUESTED', 'Completion Requested'), ('COMPLETED', 'Completed'), ('CANCELLED', 'Cancelled'), ('REJECTED', 'Rejected'), ('DISPUTED', 'Disputed')], max_length=25),
        ),
        migrations.AddConstraint(
            model_name='booking',
            constraint=models.CheckConstraint(condition=models.Q(('price_agreed__gt', 0)), name='booking_positive_price'),
        ),
        migrations.AddConstraint(
            model_name='booking',
            constraint=models.UniqueConstraint(condition=models.Q(('status__in', ['REQUESTED', 'ACCEPTED', 'IN_PROGRESS', 'COMPLETION_REQUESTED', 'DISPUTED'])), fields=('request',), name='one_active_booking_per_request'),
        ),
    ]
