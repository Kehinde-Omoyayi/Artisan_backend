from django.core.management.base import BaseCommand
from conversations.retention import reconcile


class Command(BaseCommand):
    help = "Reconcile completed request+booking recording purges, with holds and bounded retries."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=500)
        parser.add_argument(
            "--restore-scan",
            action="store_true",
            help="Rescan private namespaces even for completed purge states before serving restored data.",
        )

    def handle(self, *args, **options):
        self.stdout.write(
            str(
                reconcile(
                    max(1, min(options["limit"], 5000)),
                    force_scan=options["restore_scan"],
                )
            )
        )
