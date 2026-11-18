"""Publish every registered Locations row into the world geography files.

The pickers and ``canonical_location`` read the published catalogue
(``catalogue/data``), while places the platform already holds — institution
addresses, offering locations, request places — live in the ``Locations``
table. When a registered area is missing from the catalogue the area picker
cannot offer it and validation refuses the triple, so the two drift apart.

This closes the gap, in both directions it can drift:

* an area whose spelling only differs by punctuation or case from a published
  one is canonicalised **in the database** to the published spelling, so one
  place has one name and the picker does not show near-duplicates;
* an area the catalogue has never heard of is appended to its state in every
  published store (the per-country file, the combined file in ``catalogue``,
  and the identical copy in ``services``), creating the state/country and the
  index entries when needed.

Nothing is ever deleted. Safe to run again — a second run reports zero work.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import transaction

from home.models import Locations


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _published(value: str, candidates) -> str | None:
    """The catalogue's spelling for this name, matched exactly then loosely."""
    key = str(value or "").strip().casefold()
    for name in candidates:
        if str(name).casefold() == key:
            return str(name)
    target = _norm(value)
    if target:
        for name in candidates:
            if _norm(name) == target:
                return str(name)
    return None


def _slug_for(index: dict, country: str) -> str | None:
    """The per-country file name, taken from the index when it has one."""
    for name, path in (index.get("files") or {}).items():
        if str(name).casefold() == country.casefold():
            return str(path).rsplit("/", 1)[-1].removesuffix(".json")
    return None


class Command(BaseCommand):
    help = (
        "Align the Locations table with the published world geography: "
        "canonicalise spelling variants in the database and publish every "
        "registered area into the geography files."
    )

    def handle(self, *args, **options):
        import catalogue

        data_dir = Path(catalogue.__file__).resolve().parent / "data"
        monolith_path = data_dir / "world-geography.json"
        services_monolith = (
            Path(catalogue.__file__).resolve().parent.parent
            / "services"
            / "data"
            / "world-geography.json"
        )
        index_path = data_dir / "world-geography-index.json"

        raw = monolith_path.read_text(encoding="utf-8")
        monolith = json.loads(raw)
        trailing_newline = raw.endswith("\n")
        index = json.loads(index_path.read_text(encoding="utf-8"))

        canonicalised: list[tuple[str, str]] = []
        # country -> state -> number of areas added there
        published: dict[str, dict[str, int]] = {}

        with transaction.atomic():
            for loc in Locations.objects.all().order_by("country", "state", "area"):
                country = str(loc.country or "").strip()
                state = str(loc.state or "").strip()
                area = str(loc.area or "").strip()
                if not (country and state and area):
                    continue

                country_key = _published(country, monolith) or country
                states = monolith.setdefault(country_key, {})
                state_key = _published(state, states) or state
                areas = states.setdefault(state_key, [])

                hit = _published(area, areas)
                if hit is None:
                    areas.append(area)
                    areas.sort(key=str.casefold)
                    published.setdefault(country_key, {})
                    published[country_key][state_key] = (
                        published[country_key].get(state_key, 0) + 1
                    )
                elif hit != area:
                    Locations.objects.filter(pk=loc.pk).update(area=hit)
                    canonicalised.append((area, hit))

        # A brand-new state or country must also appear in the index.
        index_changed = False
        for country_key, state_counts in published.items():
            listed = index.setdefault("countries", {}).setdefault(country_key, [])
            for state_key in sorted(state_counts):
                if state_key not in listed:
                    listed.append(state_key)
                    listed.sort(key=str.casefold)
                    index_changed = True
            slug = _slug_for(index, country_key)
            if slug is None:
                # Never seen before: no file to put it in yet, so register the
                # file name the publisher will use and create it below.
                slug = re.sub(r"[^a-z0-9]+", "-", country_key.lower()).strip("-")
                index.setdefault("files", {})[country_key] = (
                    f"data/world-geography/{slug}.json"
                )
                index_changed = True

        # One canonical serialisation for both copies of the combined file.
        body = json.dumps(monolith, ensure_ascii=False, separators=(",", ":"))
        if trailing_newline:
            body += "\n"
        monolith_path.write_text(body, encoding="utf-8")
        if services_monolith.is_file():
            services_monolith.write_text(body, encoding="utf-8")

        for country_key in sorted(published):
            slug = _slug_for(index, country_key)
            if not slug:
                continue
            split_path = data_dir / "world-geography" / f"{slug}.json"
            split_body = json.dumps(
                monolith[country_key], ensure_ascii=False, separators=(",", ":")
            )
            if split_path.is_file() and split_path.read_text(encoding="utf-8").endswith(
                "\n"
            ):
                split_body += "\n"
            split_path.write_text(split_body, encoding="utf-8")

        if index_changed:
            index_path.write_text(
                json.dumps(index, ensure_ascii=False, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )

        for old, new in canonicalised:
            self.stdout.write(f"canonicalised in database: {old!r} -> {new!r}")
        for country_key, states in sorted(published.items()):
            for state_key, count in sorted(states.items()):
                self.stdout.write(
                    f"published: {count} area(s) -> {state_key}, {country_key}"
                )
        self.stdout.write(
            self.style.SUCCESS(
                f"Done: {len(canonicalised)} database row(s) canonicalised, "
                f"{sum(sum(s.values()) for s in published.values())} area(s) "
                f"published across {len(published)} country file(s)."
            )
        )
