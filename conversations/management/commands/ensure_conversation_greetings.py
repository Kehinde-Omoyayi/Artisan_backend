"""Give every service room its single marketplace greeting.

New rooms are initialized by the services layer when they are created. This
command backfills rooms that predate that behaviour (or that were created by
any other path) and is idempotent: a room that already has its greeting is
untouched, so it is safe to run repeatedly and safe to run from a migration.
"""

from django.core.management.base import BaseCommand

from conversations.models import Conversation
from conversations.services import ensure_conversation_greeting


class Command(BaseCommand):
    help = "Ensure every service conversation has exactly one marketplace greeting."

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch-size",
            type=int,
            default=500,
            help="Rooms to inspect per query (default 500).",
        )

    def handle(self, *args, **options):
        batch_size = max(1, int(options["batch_size"]))
        rooms = (
            Conversation.objects.filter(
                conversation_type=Conversation.ConversationType.SERVICE
            )
            .order_by("pk")
            .iterator(chunk_size=batch_size)
        )
        checked = 0
        written = 0
        for room in rooms:
            checked += 1
            before = room.messages.filter(event_code="REQUEST_GREETING").count()
            if before:
                continue
            ensure_conversation_greeting(conversation=room)
            if room.messages.filter(event_code="REQUEST_GREETING").count() > before:
                written += 1
        self.stdout.write(
            f"Conversation greetings: {checked} room(s) checked, {written} greeting(s) written."
        )
