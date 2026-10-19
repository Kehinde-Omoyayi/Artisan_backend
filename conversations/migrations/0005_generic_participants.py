from django.conf import settings
from django.db import migrations, models
from django.db.models import Q
import django.db.models.deletion


def backfill_participants(apps, schema_editor):
    Conversation = apps.get_model("conversations", "Conversation")
    for conversation in Conversation.objects.select_related("customer", "artisan__user").iterator():
        ids = []
        if conversation.customer_id:
            ids.append(conversation.customer_id)
        if getattr(conversation, "artisan_id", None):
            artisan = getattr(conversation, "artisan", None)
            if artisan and artisan.user_id:
                ids.append(artisan.user_id)
        if ids:
            conversation.participants.set(ids)


class Migration(migrations.Migration):
    dependencies = [("conversations", "0004_remove_conversation_conversation_one_draft_per_pair_and_more"), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]

    operations = [
        migrations.AddField(
            model_name="conversation",
            name="conversation_type",
            field=models.CharField(choices=[("SERVICE", "Service"), ("DIRECT", "Direct")], db_index=True, default="SERVICE", max_length=16),
        ),
        migrations.AlterField(
            model_name="conversation",
            name="customer",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="marketplace_conversations", to=settings.AUTH_USER_MODEL),
        ),
        migrations.AlterField(
            model_name="conversation",
            name="artisan",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="marketplace_conversations", to="home.artisanprofile"),
        ),
        migrations.AddField(
            model_name="conversation",
            name="participants",
            field=models.ManyToManyField(blank=True, related_name="conversations", to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name="conversation",
            name="direct_key",
            field=models.CharField(blank=True, editable=False, max_length=80, null=True, unique=True),
        ),
        migrations.RemoveConstraint(model_name="conversation", name="conv_one_draft_per_pair"),
        migrations.AddConstraint(
            model_name="conversation",
            constraint=models.UniqueConstraint(condition=Q(service_request__isnull=True, conversation_type="SERVICE"), fields=("customer", "artisan"), name="conv_one_draft_per_pair"),
        ),
        migrations.AddIndex(
            model_name="conversation",
            index=models.Index(fields=["conversation_type", "-updated_at"], name="conv_type_updated_idx"),
        ),
        migrations.RunPython(backfill_participants, migrations.RunPython.noop),
    ]
