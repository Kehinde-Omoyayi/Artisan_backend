import uuid

import django.db.models.deletion
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("home", "0007_service_browse_index"),
        ("booking", "0004_alter_booking_artisan_alter_booking_customer_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="CustomerRiskProfile",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("cancellation_points", models.PositiveSmallIntegerField(default=0)),
                ("cancellation_count", models.PositiveIntegerField(default=0)),
                ("eligible_booking_count", models.PositiveIntegerField(default=0)),
                ("weighted_cancellation_rate", models.DecimalField(decimal_places=2, default=0, max_digits=5)),
                ("risk_level", models.CharField(choices=[("NORMAL", "Normal"), ("WARNING", "Warning"), ("REVIEW", "Review"), ("SUSPENDED", "Suspended")], default="NORMAL", max_length=10)),
                ("last_evaluated_at", models.DateTimeField(blank=True, null=True)),
                ("suspended_at", models.DateTimeField(blank=True, null=True)),
                ("suspension_reason", models.CharField(blank=True, max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("customer", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="risk_profile", to="home.customerprofile")),
            ],
            options={"indexes": [models.Index(fields=["risk_level", "updated_at"], name="risk_level_updated_idx")]},
        ),
        migrations.AddConstraint(
            model_name="customerriskprofile",
            constraint=models.CheckConstraint(
                condition=Q(cancellation_points__gte=0) & Q(cancellation_points__lte=5),
                name="risk_score_0_5",
            ),
        ),
        migrations.AddConstraint(
            model_name="customerriskprofile",
            constraint=models.CheckConstraint(
                condition=Q(weighted_cancellation_rate__gte=0) & Q(weighted_cancellation_rate__lte=100),
                name="risk_rate_0_100",
            ),
        ),
        migrations.CreateModel(
            name="CustomerCancellationEvent",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("reason_code", models.CharField(max_length=50)),
                ("reason_text", models.TextField(blank=True)),
                ("severity", models.PositiveSmallIntegerField(default=0)),
                ("points", models.PositiveSmallIntegerField(default=0)),
                ("customer_attributable", models.BooleanField(default=False)),
                ("requires_review", models.BooleanField(default=False)),
                ("ip_hash", models.CharField(blank=True, max_length=64)),
                ("occurred_at", models.DateTimeField(auto_now_add=True)),
                ("scheduled_time_start", models.DateTimeField()),
                ("booking", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="customer_cancellation_event", to="booking.booking")),
                ("customer", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="cancellation_events", to="home.customerprofile")),
            ],
            options={"indexes": [
                models.Index(fields=["customer", "occurred_at"], name="cancel_customer_time_idx"),
                models.Index(fields=["ip_hash", "occurred_at"], name="cancel_ip_time_idx"),
                models.Index(fields=["reason_code", "occurred_at"], name="cancel_reason_time_idx"),
            ]},
        ),
        migrations.AddConstraint(
            model_name="customercancellationevent",
            constraint=models.CheckConstraint(
                condition=Q(severity__gte=0) & Q(severity__lte=5),
                name="cancel_severity_0_5",
            ),
        ),
        migrations.AddConstraint(
            model_name="customercancellationevent",
            constraint=models.CheckConstraint(
                condition=Q(points__gte=0) & Q(points__lte=5),
                name="cancel_points_0_5",
            ),
        ),
        migrations.CreateModel(
            name="CustomerNetworkObservation",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("ip_hash", models.CharField(max_length=64)),
                ("observation_type", models.CharField(default="ACTIVITY", max_length=30)),
                ("first_seen_at", models.DateTimeField(auto_now_add=True)),
                ("last_seen_at", models.DateTimeField(auto_now=True)),
                ("observation_count", models.PositiveIntegerField(default=1)),
                ("customer", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="network_observations", to="home.customerprofile")),
            ],
            options={"indexes": [
                models.Index(fields=["ip_hash", "last_seen_at"], name="network_ip_last_seen_idx"),
                models.Index(fields=["customer", "last_seen_at"], name="network_customer_last_seen_idx"),
            ]},
        ),
        migrations.CreateModel(
            name="CustomerNetworkBlock",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("ip_hash", models.CharField(max_length=64, unique=True)),
                ("scope", models.CharField(choices=[("AUTH_BOOKING", "Authentication and booking")], default="AUTH_BOOKING", max_length=20)),
                ("reason", models.CharField(max_length=255)),
                ("confidence", models.DecimalField(decimal_places=2, default=0, max_digits=5)),
                ("block_count", models.PositiveIntegerField(default=1)),
                ("blocked_until", models.DateTimeField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"indexes": [
                models.Index(fields=["ip_hash", "blocked_until"], name="network_block_active_idx"),
                models.Index(fields=["blocked_until"], name="network_block_expiry_idx"),
            ]},
        ),
        migrations.AddConstraint(
            model_name="customernetworkobservation",
            constraint=models.UniqueConstraint(fields=("customer", "ip_hash"), name="unique_customer_ip_observation"),
        ),
    ]
