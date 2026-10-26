"""Request catalogue options for the explicit request draft form.

Extracted from the retired rule-based assistant (automation.py). This is plain
catalogue data — the artisan's active services and the real location catalogue —
with no conversation interpretation, prompts or suggestions.
"""
from collections import defaultdict

from home.models import ArtisanService, Locations, Service, ServiceCategory


def _offered_service_rows(conversation):
    if not getattr(conversation, "artisan_id", None):
        from types import SimpleNamespace

        qs = Service.objects.filter(is_active=True).select_related("category")
        category = (conversation.draft or {}).get("category_id")
        if category:
            qs = qs.filter(category_id=category)
        return [
            SimpleNamespace(service=s, service_id=s.pk, location_id=None) for s in qs
        ]
    return list(
        ArtisanService.objects.filter(
            artisan=conversation.artisan, service__is_active=True
        )
        .select_related("service__category", "location")
        .order_by(
            "service__category__name", "service__name", "service_id", "location_id"
        )
    )


def _location_record(location):
    return {
        "id": str(location.pk),
        "country": location.country,
        "state": location.state,
        "area": location.area,
        "label": f"{location.area}, {location.state}, {location.country}",
    }


def _locations_for_service_rows(rows):
    service_has_general_coverage = defaultdict(bool)
    location_ids_by_service = defaultdict(set)
    for row in rows:
        service_id = str(row.service_id)
        if row.location_id is None:
            service_has_general_coverage[service_id] = True
        else:
            location_ids_by_service[service_id].add(row.location_id)

    all_locations = None
    if any(service_has_general_coverage.values()):
        all_locations = list(
            Locations.objects.order_by("country", "state", "area", "pk")
        )

    records_by_service = {}
    for service_id in {str(row.service_id) for row in rows}:
        if service_has_general_coverage[service_id]:
            locations = all_locations or []
        else:
            locations = list(
                Locations.objects.filter(
                    pk__in=location_ids_by_service[service_id]
                ).order_by("country", "state", "area", "pk")
            )
        records_by_service[service_id] = [
            _location_record(location) for location in locations
        ]
    return records_by_service


def request_options_for(conversation):
    """Return only this artisan's active service and real location catalogue options."""
    rows = _offered_service_rows(conversation)
    if not conversation.artisan_id:
        services = list(
            Service.objects.filter(is_active=True)
            .select_related("category")
            .order_by("name")
        )
        countries = []
        locations = [
            _location_record(loc)
            for loc in Locations.objects.all().order_by("country", "state", "area")
        ]
        items = [
            {
                "id": str(s.pk),
                "name": s.name,
                "description": s.description,
                "category_id": str(s.category_id),
                "category_name": s.category.name,
                "locations": [],
            }
            for s in services
        ]
        categories = [
            {
                "id": str(c.pk),
                "name": c.name,
                "services": [i for i in items if i["category_id"] == str(c.pk)],
            }
            for c in ServiceCategory.objects.all().order_by("name")
        ]
        return {"categories": categories, "services": items, "locations": locations}
    if not rows:
        return {"categories": [], "services": [], "locations": []}

    by_service = {}
    for row in rows:
        service = row.service
        key = str(service.pk)
        if key not in by_service:
            by_service[key] = {
                "id": key,
                "name": service.name,
                "description": service.description or "",
                "category_id": str(service.category_id),
                "category_name": service.category.name,
            }

    locations_by_service = _locations_for_service_rows(rows)
    services = []
    for key, service in by_service.items():
        item = dict(service)
        item["locations"] = locations_by_service.get(key, [])
        services.append(item)
    services.sort(
        key=lambda item: (
            item["category_name"].casefold(),
            item["name"].casefold(),
            item["id"],
        )
    )

    categories_by_id = {}
    for service in services:
        category_id = service["category_id"]
        category = categories_by_id.setdefault(
            category_id,
            {"id": category_id, "name": service["category_name"], "services": []},
        )
        category["services"].append(service)
    categories = sorted(
        categories_by_id.values(), key=lambda item: item["name"].casefold()
    )

    locations_by_id = {}
    for service in services:
        for location in service["locations"]:
            record = locations_by_id.setdefault(
                location["id"], {**location, "service_ids": []}
            )
            if service["id"] not in record["service_ids"]:
                record["service_ids"].append(service["id"])
    locations = sorted(
        locations_by_id.values(),
        key=lambda item: (
            item["country"].casefold(),
            item["state"].casefold(),
            item["area"].casefold(),
            item["id"],
        ),
    )
    return {"categories": categories, "services": services, "locations": locations}
