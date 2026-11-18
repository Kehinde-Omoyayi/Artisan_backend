"""Load the published institution register into the database.

The institution picker, the request filters, discovery and promotions all read
``home.Institution``. Nothing in the project ever *wrote* that table, so a fresh
database — however well the geography and service tables are populated — served
an empty institution list everywhere. This command closes that gap.

The register itself is the JSON the API publishes under
``catalogue/data/institutions/``; the command reads it through
``catalogue.sources`` so there is still exactly one copy of the data.

    python manage.py seed_institutions                # Nigeria (fast, the default)
    python manage.py seed_institutions --country all  # every country, ~30k rows
    python manage.py seed_institutions --dry-run      # report only, write nothing

Safe to run repeatedly: rows are matched on ``source_id`` and existing rows are
left alone (``--update`` refreshes their name, campus and aliases).
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from home.models import Institution, Locations


class Command(BaseCommand):
    help = "Seed the institution register (and the locations it hangs off)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--country",
            default="Nigeria",
            help="A single country name, or 'all' for the whole register.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Count what would change without writing anything.",
        )
        parser.add_argument(
            "--update",
            action="store_true",
            help="Refresh name, campus and aliases on rows that already exist.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=1000,
            help="Rows per bulk insert (default 1000).",
        )

    def handle(self, *args, **options):
        from catalogue import sources

        country = str(options["country"] or "").strip()
        dry_run = options["dry_run"]
        batch_size = max(1, int(options["batch_size"]))

        if country.casefold() == "all":
            rows = list(sources.all_institutions())
            label = "every country"
        else:
            known = {item["country"] for item in sources.index()["countries"]}
            match = next((name for name in known if name.casefold() == country.casefold()), None)
            if match is None:
                raise CommandError(
                    f"No institution file for {country!r}. Try --country all, or one of: "
                    + ", ".join(sorted(known)[:12])
                    + " …"
                )
            rows = list(sources.institutions(match))
            label = match

        if not rows:
            self.stdout.write(self.style.WARNING(f"The register holds no rows for {label}."))
            return

        self.stdout.write(f"Register: {len(rows):,} institutions for {label}.")

        existing = set(
            Institution.objects.filter(source_id__in=[r["source_id"] for r in rows]).values_list(
                "source_id", flat=True
            )
        )
        pending = [row for row in rows if row["source_id"] not in existing]
        self.stdout.write(
            f"Already in the database: {len(existing):,} · to add: {len(pending):,}"
        )

        if dry_run:
            new_locations = {
                (row.get("country", ""), row.get("state", ""), row.get("area", ""))
                for row in pending
            }
            known_locations = set(Locations.objects.values_list("country", "state", "area"))
            self.stdout.write(
                f"Locations that would be created: {len(new_locations - known_locations):,}"
            )
            self.stdout.write(self.style.WARNING("Dry run: nothing was written."))
            return

        created_institutions = 0
        with transaction.atomic():
            # 1. every institution needs a location row; reuse what is already there
            keys = {
                (row.get("country", ""), row.get("state", ""), row.get("area", ""))
                for row in rows
            }
            location_map = {
                (loc.country, loc.state, loc.area): loc
                for loc in Locations.objects.filter(
                    country__in={k[0] for k in keys}
                )
            }
            missing = keys - set(location_map)
            for chunk_start in range(0, len(missing), batch_size):
                chunk = sorted(missing)[chunk_start : chunk_start + batch_size]
                Locations.objects.bulk_create(
                    [Locations(country=c, state=s, area=a) for c, s, a in chunk],
                    ignore_conflicts=True,
                )
            location_map = {
                (loc.country, loc.state, loc.area): loc
                for loc in Locations.objects.filter(country__in={k[0] for k in keys})
            }

            # 2. the institutions themselves
            def build(row):
                location = location_map.get(
                    (row.get("country", ""), row.get("state", ""), row.get("area", ""))
                )
                if location is None:  # pragma: no cover - defensive
                    return None
                return Institution(
                    name=str(row.get("name") or "")[:180],
                    campus=str(row.get("campus") or "")[:180],
                    abbreviation=str(row.get("abbreviation") or "")[:40],
                    abbreviations=list(row.get("abbreviations") or []),
                    aliases=list(row.get("aliases") or []),
                    source_id=row["source_id"],
                    source_url=str(row.get("source_url") or ""),
                    location=location,
                    is_active=True,
                )

            objects = [obj for obj in (build(row) for row in pending) if obj is not None]
            for chunk_start in range(0, len(objects), batch_size):
                chunk = objects[chunk_start : chunk_start + batch_size]
                Institution.objects.bulk_create(chunk, ignore_conflicts=True)
                created_institutions += len(chunk)

            if options["update"] and existing:
                for row in rows:
                    if row["source_id"] not in existing:
                        continue
                    Institution.objects.filter(source_id=row["source_id"]).update(
                        name=str(row.get("name") or "")[:180],
                        campus=str(row.get("campus") or "")[:180],
                        abbreviation=str(row.get("abbreviation") or "")[:40],
                        abbreviations=list(row.get("abbreviations") or []),
                        aliases=list(row.get("aliases") or []),
                        source_url=str(row.get("source_url") or ""),
                        is_active=True,
                    )

        total = Institution.objects.filter(is_active=True).count()
        self.stdout.write(
            self.style.SUCCESS(
                f"Added {created_institutions:,} institutions. "
                f"The database now serves {total:,} active institutions."
            )
        )
