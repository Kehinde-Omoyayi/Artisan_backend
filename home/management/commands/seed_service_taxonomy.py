"""Load the service catalogue into ServiceCategory and Service.

Safe to run as many times as you like: everything is get_or_create on the natural
key, so a re-run reports "unchanged" instead of duplicating rows or crashing on
Service.name's global unique constraint.

    python manage.py seed_service_taxonomy              # write (core + growth + specialist)
    python manage.py seed_service_taxonomy --dry-run    # show what would change
    python manage.py seed_service_taxonomy --tier core  # launch set only
    python manage.py seed_service_taxonomy --prune      # also soft-disable services
                                                        # the catalogue dropped

`--prune` only sets is_active=False. It never deletes: bookings, artisan services
and ratings point at these rows, and deleting a Service would take that history
with it (or be refused by the FK).
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from home.models import Service, ServiceCategory
from services.catalogue import service_taxonomy


class Command(BaseCommand):
    help = "Create the researched service categories and sub-services (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--tier",
            action="append",
            choices=[service_taxonomy.TIER_CORE, service_taxonomy.TIER_GROWTH, service_taxonomy.TIER_SPECIALIST],
            help="Only seed this tier. Repeat for more than one. Default: all tiers.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be created or updated without writing to the database.",
        )
        parser.add_argument(
            "--prune",
            action="store_true",
            help="Soft-disable services in a seeded category that are no longer in the catalogue.",
        )

    def handle(self, *args, **options):
        tiers = set(options["tier"] or [service_taxonomy.TIER_CORE, service_taxonomy.TIER_GROWTH, service_taxonomy.TIER_SPECIALIST])
        dry_run = options["dry_run"]
        prune = options["prune"]

        categories = [c for c in service_taxonomy.CATEGORIES if c["tier"] in tiers]

        if not categories:
            self.stdout.write(self.style.WARNING("Nothing selected."))
            return

        created_c = updated_c = created_s = updated_s = unchanged_s = 0
        reassigned = []
        pruned = []

        # One transaction: a half-loaded catalogue would show categories with no
        # services in the customer app.
        with transaction.atomic():
            for category in categories:
                cat_obj, cat_created = ServiceCategory.objects.get_or_create(name=category["name"])
                if cat_created:
                    created_c += 1
                else:
                    updated_c += 1  # get_or_create found it; nothing to update (name is the key)

                for service_name, description in category["services"]:
                    service, created = Service.objects.get_or_create(
                        name=service_name,
                        defaults={
                            "category": cat_obj,
                            "description": description,
                            "is_active": True,
                        },
                    )
                    if created:
                        created_s += 1
                        continue

                    changed = False
                    if service.description != description:
                        service.description = description
                        changed = True
                    if not service.is_active:
                        service.is_active = True
                        changed = True
                    if service.category_id != cat_obj.name:
                        # The name exists under another category. Move it rather than
                        # creating a duplicate, which the unique constraint forbids.
                        reassigned.append(f"{service_name}: {service.category_id} -> {cat_obj.name}")
                        service.category = cat_obj
                        changed = True
                    if changed:
                        service.save(update_fields=["description", "is_active", "category"])
                        updated_s += 1
                    else:
                        unchanged_s += 1

                if prune:
                    keep = [name for name, _ in category["services"]]
                    stale = Service.objects.filter(category=cat_obj, is_active=True).exclude(name__in=keep)
                    for name in stale.values_list("name", flat=True):
                        pruned.append(name)
                    stale.update(is_active=False)

            if dry_run:
                transaction.set_rollback(True)

        verb = "Would create" if dry_run else "Created"
        self.stdout.write(
            f"{verb} {created_c} categories / {created_s} services; "
            f"{updated_s} services updated, {unchanged_s} already correct "
            f"({updated_c} categories already existed)."
        )

        if reassigned:
            self.stdout.write(self.style.WARNING("Moved between categories:"))
            for line in reassigned:
                self.stdout.write(f"  {line}")

        if pruned:
            label = "Would disable" if dry_run else "Disabled"
            self.stdout.write(self.style.WARNING(f"{label} {len(pruned)} stale services:"))
            for name in pruned:
                self.stdout.write(f"  {name}")

        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run: nothing was written."))
        else:
            self.stdout.write(self.style.SUCCESS("Service catalogue is up to date."))
