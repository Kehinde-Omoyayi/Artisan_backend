"""Load the published institution register into the database.

The institution table had no writer. Every picker, filter and promotion reads
``home.Institution``, so a correct, fully migrated database served an empty
institution list -- the "all the institutions are empty" report.

This runs the same loader as ``manage.py seed_institutions`` (Nigeria, the
register's default country, 342 rows) and skips anything already present, so it
is safe on a database that has been seeded by hand. The full 30k-row register is
one command away:

    python manage.py seed_institutions --country all
"""
from django.db import migrations

COUNTRY = "Nigeria"


def seed(apps, schema_editor):
    Institution = apps.get_model("home", "Institution")
    Locations = apps.get_model("home", "Locations")
    try:
        from catalogue import sources
    except Exception:  # pragma: no cover - the API package is always present here
        return

    rows = list(sources.institutions(COUNTRY))
    if not rows:
        return

    present = set(
        Institution.objects.filter(
            source_id__in=[row["source_id"] for row in rows]
        ).values_list("source_id", flat=True)
    )
    pending = [row for row in rows if row["source_id"] not in present]
    if not pending:
        return

    wanted = {
        (row.get("country", ""), row.get("state", ""), row.get("area", ""))
        for row in pending
    }
    known = {
        (loc.country, loc.state, loc.area): loc
        for loc in Locations.objects.filter(country__in={key[0] for key in wanted})
    }
    missing = wanted - set(known)
    Locations.objects.bulk_create(
        [Locations(country=c, state=s, area=a) for c, s, a in sorted(missing)],
        ignore_conflicts=True,
    )
    known = {
        (loc.country, loc.state, loc.area): loc
        for loc in Locations.objects.filter(country__in={key[0] for key in wanted})
    }

    objects = []
    for row in pending:
        location = known.get(
            (row.get("country", ""), row.get("state", ""), row.get("area", ""))
        )
        if location is None:  # pragma: no cover - defensive
            continue
        objects.append(
            Institution(
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
        )
    Institution.objects.bulk_create(objects, ignore_conflicts=True, batch_size=1000)


def unseed(apps, schema_editor):
    # Deliberately empty: rows a user may have edited are not ours to delete.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("home", "0016_profile_institution_text_snapshots"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
