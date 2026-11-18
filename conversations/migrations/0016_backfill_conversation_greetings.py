"""Backfill the marketplace greeting for service rooms created before Phase 1.

Rooms created from now on are initialized by
``conversations.services.ensure_conversation_greeting`` when they are opened for
the first time. This migration gives every existing room the same single
greeting; it is idempotent because the greeting is keyed by ``event_key``.
"""

from django.db import migrations


def backfill_greetings(apps, schema_editor):
    Conversation = apps.get_model("conversations", "Conversation")
    Message = apps.get_model("conversations", "Message")

    def greeting_text(customer):
        name = ""
        if customer is not None:
            name = (getattr(customer, "first_name", "") or "").strip()
        name = name or "there"
        return (
            f"Good day {name}. Your chat is open — say hello, ask a question or send a "
            "photo of the job whenever you like. When you are ready, choose Request "
            "service so the artisan can send you a formal itemised offer."
        )

    for conversation in Conversation.objects.filter(
        conversation_type="SERVICE"
    ).select_related("customer"):
        if Message.objects.filter(
            conversation=conversation, event_code="REQUEST_GREETING"
        ).exists():
            continue
        Message.objects.get_or_create(
            event_key=f"conversation:{conversation.pk}:greeting",
            defaults={
                "conversation": conversation,
                "sender": None,
                "message_type": "SYSTEM",
                "event_code": "REQUEST_GREETING",
                "body": greeting_text(conversation.customer),
                "metadata": {"greeting": True},
            },
        )


def noop(apps, schema_editor):
    """The greeting is production data; nothing is undone on reverse."""


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0015_remove_offer_offer_total_matches_parts_and_more"),
    ]

    operations = [migrations.RunPython(backfill_greetings, noop)]
