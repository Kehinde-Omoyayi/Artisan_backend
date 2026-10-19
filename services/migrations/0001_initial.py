import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
import uuid


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("home", "0004_alter_customuser_phone_number"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ArtisanVerification",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("verification_status", models.CharField(choices=[("UNVERIFIED", "Unverified"), ("PENDING", "Pending"), ("VERIFIED", "Verified"), ("REJECTED", "Rejected"), ("SUSPENDED", "Suspended")], default="UNVERIFIED", max_length=20)),
                ("verification_method", models.CharField(blank=True, choices=[("NIN", "NIN"), ("BVN", "BVN")], max_length=10)),
                ("verification_tier", models.PositiveSmallIntegerField(default=1)),
                ("verification_provider", models.CharField(blank=True, max_length=30)),
                ("verification_reference", models.CharField(blank=True, max_length=255)),
                ("verified_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("artisan", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="verification", to="home.artisanprofile")),
            ],
        ),
        migrations.CreateModel(
            name="ServiceRequest",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("preferred_date", models.DateField()),
                ("urgency", models.CharField(choices=[("LOW", "Low"), ("NORMAL", "Normal"), ("HIGH", "High"), ("EMERGENCY", "Emergency")], default="NORMAL", max_length=20)),
                ("budget", models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ("description", models.TextField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("category", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="service_requests", to="home.servicecategory")),
                ("location", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="service_requests", to="home.locations")),
                ("requester", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="service_requests", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "indexes": [
                    models.Index(fields=["requester", "-updated_at"], name="svc_req_requester_updated_idx"),
                    models.Index(fields=["category", "location"], name="svc_req_category_location_idx"),
                    models.Index(fields=["preferred_date", "urgency"], name="svc_req_date_urgency_idx"),
                ],
            },
        ),
    ]
