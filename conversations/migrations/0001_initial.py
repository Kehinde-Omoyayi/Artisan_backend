import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import conversations.models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("home", "0007_service_browse_index"),
        ("services", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="Conversation",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("draft", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "artisan",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="marketplace_conversations",
                        to="home.artisanprofile",
                    ),
                ),
                (
                    "customer",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="marketplace_conversations",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "service_request",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="conversation",
                        to="services.servicerequest",
                    ),
                ),
            ],
            options={
                "ordering": ["-updated_at"],
                "indexes": [
                    models.Index(
                        fields=["customer", "-updated_at"],
                        name="conversation_customer_updated_idx",
                    ),
                    models.Index(
                        fields=["artisan", "-updated_at"],
                        name="conversation_artisan_updated_idx",
                    ),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("customer", "artisan"),
                        name="conversation_customer_artisan_unique",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="ConversationReadState",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("last_read_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "conversation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="read_states",
                        to="conversations.conversation",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="conversation_read_states",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["user", "conversation"],
                        name="conversation_read_user_idx",
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("conversation", "user"),
                        name="conversation_read_state_unique",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="Message",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "message_type",
                    models.CharField(
                        choices=[("USER", "User"), ("SYSTEM", "System")],
                        default="USER",
                        max_length=10,
                    ),
                ),
                ("body", models.TextField(blank=True)),
                ("event_code", models.CharField(blank=True, max_length=60)),
                (
                    "event_key",
                    models.CharField(
                        blank=True, max_length=255, null=True, unique=True
                    ),
                ),
                (
                    "idempotency_key",
                    models.CharField(blank=True, max_length=255, null=True),
                ),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "conversation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="messages",
                        to="conversations.conversation",
                    ),
                ),
                (
                    "sender",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="conversation_messages",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["created_at"],
                "indexes": [
                    models.Index(
                        fields=["conversation", "-created_at"],
                        name="conversation_msg_created_idx",
                    ),
                    models.Index(
                        fields=["conversation", "event_code"],
                        name="conversation_msg_event_idx",
                    ),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(idempotency_key__isnull=False),
                        fields=("conversation", "sender", "idempotency_key"),
                        name="conversation_msg_idempotency_unique",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(event_key__isnull=False),
                        fields=("conversation", "sender", "event_key"),
                        name="conversation_msg_event_scoped_unique",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="ConversationAttachment",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "file",
                    models.FileField(
                        upload_to=conversations.models.conversation_attachment_upload_to
                    ),
                ),
                ("content_type", models.CharField(max_length=80)),
                ("size", models.PositiveBigIntegerField()),
                ("uploaded_at", models.DateTimeField(auto_now_add=True)),
                (
                    "message",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="attachments",
                        to="conversations.message",
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["message", "uploaded_at"],
                        name="conversation_att_created_idx",
                    )
                ]
            },
        ),
        migrations.CreateModel(
            name="Offer",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("labour_amount", models.DecimalField(decimal_places=2, max_digits=12)),
                (
                    "materials_amount",
                    models.DecimalField(decimal_places=2, max_digits=12),
                ),
                ("total_amount", models.DecimalField(decimal_places=2, max_digits=12)),
                ("materials_included", models.BooleanField(default=False)),
                ("scope", models.TextField(max_length=5000)),
                ("notes", models.TextField(blank=True, max_length=3000)),
                ("scheduled_start", models.DateTimeField()),
                ("scheduled_end", models.DateTimeField()),
                ("expires_at", models.DateTimeField()),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("PROPOSED", "Proposed"),
                            ("COUNTERED", "Countered"),
                            ("ACCEPTED", "Accepted"),
                            ("DECLINED", "Declined"),
                            ("EXPIRED", "Expired"),
                            ("CANCELLED", "Cancelled"),
                        ],
                        db_index=True,
                        default="PROPOSED",
                        max_length=20,
                    ),
                ),
                (
                    "idempotency_key",
                    models.CharField(blank=True, max_length=255, null=True),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "conversation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="offers",
                        to="conversations.conversation",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="offers_created",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "parent_offer",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="counter_offers",
                        to="conversations.offer",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
                "indexes": [
                    models.Index(
                        fields=["conversation", "status", "-created_at"],
                        name="offer_conversation_status_idx",
                    ),
                    models.Index(
                        fields=["expires_at", "status"], name="offer_expiry_status_idx"
                    ),
                    models.Index(
                        fields=["conversation", "parent_offer"], name="offer_parent_idx"
                    ),
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(labour_amount__gte=0),
                        name="offer_labour_nonnegative",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(materials_amount__gte=0),
                        name="offer_materials_nonnegative",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(total_amount__gt=0),
                        name="offer_total_positive",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            total_amount=models.F("labour_amount")
                            + models.F("materials_amount")
                        ),
                        name="offer_total_matches_parts",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(idempotency_key__isnull=False),
                        fields=("created_by", "idempotency_key"),
                        name="offer_creator_idempotency_unique",
                    ),
                ],
            },
        ),
    ]
