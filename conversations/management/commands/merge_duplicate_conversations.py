"""Fold every duplicate customer-artisan room into the pair's one table.

Before the "one room per pair" rule, a finished or cancelled job retired the
room, so the next message to the same artisan carved out a new one. A pair
could end up with several rooms, each holding a slice of the same
conversation, and no single place that had all of it.

This puts it right without losing anything: the pair keeps its newest room,
every message from the older rooms moves into it in the order it was written,
and the attachments come along because they hang off the messages. The old
rooms are then marked as folded-away rather than deleted -- their service
requests and bookings are still the record of the jobs.

    python manage.py merge_duplicate_conversations            # report only
    python manage.py merge_duplicate_conversations --apply    # do it
"""

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count, Max, OuterRef, Subquery
from django.utils import timezone

from conversations.models import Conversation, Message


class Command(BaseCommand):
    help = "Merge duplicate customer-artisan conversations into one table per pair."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually move the messages. Without it, only report.",
        )
        parser.add_argument(
            "--pair",
            type=str,
            default="",
            help="Optional '<customer_id>:<artisan_id>' to limit to one pair.",
        )

    def handle(self, *args, **options):
        apply_changes = bool(options.get("apply"))
        only_pair = str(options.get("pair") or "").strip()

        rooms = (
            Conversation.objects.filter(
                conversation_type=Conversation.ConversationType.SERVICE,
                customer__isnull=False,
                artisan__isnull=False,
            )
            .values("customer_id", "artisan_id")
            .annotate(total=Count("pk"))
            .filter(total__gt=1)
            .order_by("-total")
        )
        if only_pair:
            try:
                customer_id, artisan_id = only_pair.split(":", 1)
                rooms = rooms.filter(
                    customer_id=int(customer_id), artisan_id=int(artisan_id)
                )
            except (ValueError, TypeError):
                self.stderr.write("--pair must look like '<customer_id>:<artisan_id>'.")
                return

        pairs = list(rooms)
        if not pairs:
            self.stdout.write("No duplicated customer-artisan rooms found.")
            return

        moved_total = 0
        folded_total = 0
        for pair in pairs:
            group = list(
                Conversation.objects.filter(
                    customer_id=pair["customer_id"],
                    artisan_id=pair["artisan_id"],
                    conversation_type=Conversation.ConversationType.SERVICE,
                ).order_by("-updated_at", "-created_at")
            )
            if len(group) < 2:
                continue
            # The newest room is the one that survives; it is already the one
            # the inbox surfaces first.
            keeper = group[0]
            others = group[1:]
            moved = 0
            for old in others:
                count = Message.objects.filter(conversation=old).count()
                self.stdout.write(
                    f"  room {old.id} -> {keeper.id}: {count} message(s)"
                )
                if apply_changes and count:
                    with transaction.atomic():
                        # The messages keep their own ids, timestamps and
                        # senders: only their room changes, so nothing in the
                        # conversation is rewritten or reordered.
                        Message.objects.filter(conversation=old).update(
                            conversation=keeper
                        )
                        old.metadata = {
                            **(old.metadata if isinstance(old.metadata, dict) else {}),
                            "folded_into": {
                                "conversation_id": str(keeper.id),
                                "at": timezone.now().isoformat(),
                                "messages_moved": count,
                            },
                        }
                        old.save(update_fields=["metadata"])
                moved += count
                folded_total += 1
            if apply_changes and moved:
                Conversation.objects.filter(pk=keeper.pk).update(
                    updated_at=timezone.now()
                )
            moved_total += moved
            self.stdout.write(
                f"pair {pair['customer_id']}:{pair['artisan_id']} — "
                f"{len(group)} rooms, {moved} message(s) moved into {keeper.id}"
            )

        verb = "Moved" if apply_changes else "Would move"
        self.stdout.write("")
        self.stdout.write(
            f"{verb} {moved_total} message(s) across {folded_total} duplicate room(s) "
            f"in {len(pairs)} pair(s)."
        )
        if not apply_changes:
            self.stdout.write("Re-run with --apply to carry it out.")
