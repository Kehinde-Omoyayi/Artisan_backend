import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models

class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('auth', '0012_alter_user_first_name_max_length'),
    ]

    operations = [
        migrations.CreateModel(
            name='Locations',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('country', models.CharField(db_index=True, max_length=100)),
                ('state', models.CharField(db_index=True, max_length=100)),
                ('area', models.CharField(db_index=True, max_length=150)),
            ],
        ),
        migrations.CreateModel(
            name='ServiceCategory',
            fields=[
                ('name', models.CharField(max_length=100, primary_key=True, serialize=False, unique=True)),
            ],
        ),
        migrations.CreateModel(
            name='CustomUser',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('password', models.CharField(max_length=128, verbose_name='password')),
                ('last_login', models.DateTimeField(blank=True, null=True, verbose_name='last login')),
                ('is_superuser', models.BooleanField(default=False, help_text='Designates that this user has all permissions without explicitly assigning them.', verbose_name='superuser status')),
                ('email', models.EmailField(max_length=254, unique=True)),
                ('first_name', models.CharField(max_length=40)),
                ('last_name', models.CharField(max_length=40)),
                ('is_active', models.BooleanField(default=False)),
                ('is_staff', models.BooleanField(default=False)),
                ('token_version', models.PositiveIntegerField(default=1)),
                ('security_status', models.CharField(choices=[('NORMAL', 'Normal'), ('REVIEW', 'Review'), ('LOCKED', 'Locked')], default='NORMAL', max_length=10)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('groups', models.ManyToManyField(blank=True, help_text='The groups this user belongs to. A user will get all permissions granted to each of their groups.', related_name='user_set', related_query_name='user', to='auth.group', verbose_name='groups')),
                ('user_permissions', models.ManyToManyField(blank=True, help_text='Specific permissions for this user.', related_name='user_set', related_query_name='user', to='auth.permission', verbose_name='user permissions')),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='ArtisanProfile',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('business_name', models.CharField(blank=True, max_length=150)),
                ('bio', models.TextField(blank=True)),
                ('years_experience', models.PositiveIntegerField(default=0)),
                ('country', models.CharField(blank=True, max_length=100)),
                ('state', models.CharField(blank=True, max_length=100)),
                ('area', models.CharField(blank=True, max_length=150)),
                ('average_rating', models.DecimalField(decimal_places=2, default=0.0, max_digits=3)),
                ('completion_rate', models.FloatField(default=0.0)),
                ('cancellation_rate', models.FloatField(default=0.0)),
                ('profile_status', models.CharField(choices=[('DRAFT', 'Draft'), ('ACTIVE', 'Active'), ('INACTIVE', 'Inactive'), ('SUSPENDED', 'Suspended')], default='DRAFT', max_length=20)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('user', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='artisan_profile', to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name='CustomerProfile',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('date_of_birth', models.DateField(blank=True, null=True)),
                ('country', models.CharField(blank=True, max_length=100)),
                ('state', models.CharField(blank=True, max_length=100)),
                ('area', models.CharField(blank=True, max_length=150)),
                ('gender', models.CharField(choices=[('M', 'Male'), ('F', 'Female'), ('N', 'Prefer not to say')], default='N', max_length=1)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('user', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='customer_profile', to=settings.AUTH_USER_MODEL)),
                ('location', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='customer_location', to='home.locations')),
            ],
        ),
        migrations.CreateModel(
            name='Service',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=150, unique=True)),
                ('description', models.TextField(blank=True)),
                ('is_active', models.BooleanField(default=True)),
                ('category', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='services', to='home.servicecategory')),
            ],
        ),
        migrations.CreateModel(
            name='ArtisanService',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('starting_price', models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ('artisan', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='artisan_services', to='home.artisanprofile')),
                ('location', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='artisan_service_location', to='home.locations')),
                ('service', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='artisan_services_category', to='home.service')),
            ],
            options={
                'constraints': [models.UniqueConstraint(fields=('artisan', 'service', 'location'), name='unique_artisan_service_location')],
            },
        ),
    ]
