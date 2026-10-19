import logging
import re
import unicodedata
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.utils import timezone

from home.models import ArtisanService, Locations, Service, ServiceCategory

from .state import derive_conversation_state

logger = logging.getLogger(__name__)

PROMPTS = {
    "service": [
        "Which service from this artisan’s catalogue best matches the job?",
        "Choose the service you need from the artisan’s available services.",
        "I can help organize this. Which listed service should the artisan provide?",
    ],
    "description": [
        "What needs to be repaired, installed, or done?",
        "Tell me what is happening and what you would like the artisan to do.",
        "Add a short description of the work so the artisan can review it.",
    ],
    "location": [
        "Where should the work be carried out? Choose a location from the catalogue.",
        "Which country, state, and area should the artisan travel to?",
        "Choose the service location from the available catalogue areas.",
    ],
    "preferred_date": [
        "What date would you prefer? You can choose one on the calendar.",
        "Which date works best for the service?",
        "Choose a preferred service date; it can still be discussed with the artisan.",
    ],
}

STOPWORDS = {
    "a", "about", "an", "and", "are", "at", "be", "can", "could", "do", "done",
    "for", "from", "get", "help", "i", "in", "is", "it", "me", "my", "need", "of",
    "on", "or", "please", "the", "this", "to", "want", "what", "would", "you", "your",
    "service", "services", "job", "work", "works", "something", "someone", "some",
    "there", "here", "kind", "type", "with", "have", "has", "just", "like", "somebody",
    "not", "instead", "rather", "actually", "longer", "wrong", "am", "pm",
    "artisan", "tomorrow", "today", "next", "week", "morning", "afternoon", "evening",
    "budget", "price", "cost", "quote", "amount", "n", "location", "country", "state", "area",
    "date", "time", "preferred", "prefer", "schedule", "scheduled", "move", "change", "update", "set",
}
GENERIC_SERVICE_TERMS = {
    "service", "services", "repair", "repairs", "installation", "installations", "work",
    "works", "general", "and", "the", "for", "of", "professional",
}
INTENT_ALIASES = {
    "plumber": {"plumbing"}, "tap": {"plumbing"}, "faucet": {"plumbing"}, "pipe": {"plumbing"},
    "leak": {"plumbing"}, "leaking": {"plumbing"}, "drain": {"plumbing"},
    "blocked": {"plumbing"}, "toilet": {"plumbing"}, "sink": {"plumbing"},
    "water": {"plumbing"}, "burst": {"plumbing"},
    "electrician": {"electrical"}, "electric": {"electrical"},
    "socket": {"electrical"}, "wiring": {"electrical"}, "wire": {"electrical"},
    "light": {"electrical"}, "switch": {"electrical"}, "fuse": {"electrical"},
    "circuit": {"electrical"}, "power": {"electrical"}, "outlet": {"electrical"},
    "painter": {"painting"}, "paint": {"painting"}, "repaint": {"painting"},
    "carpenter": {"carpentry"}, "cabinet": {"carpentry"}, "furniture": {"carpentry"},
    "tailor": {"tailoring"}, "sewing": {"tailoring"}, "zipper": {"tailoring"},
    "tile": {"tiling"}, "tiles": {"tiling"}, "grout": {"tiling"},
    "cleaner": {"cleaning"}, "clean": {"cleaning"}, "cleaning": {"cleaning"}, "mop": {"cleaning"},
}
CORRECTION_RE = re.compile(
    r"\b(actually|correction|i meant|i mean|rather|instead|not that|not the|"
    r"change(?: the)?|update(?: the)?|ignore (?:that|the previous)|wrong (?:area|date|service))\b",
    re.I,
)
ADDITIONAL_RE = re.compile(r"^\s*(?:also|in addition|one more thing|additional detail|another detail)\b", re.I)


def _pick(values, conversation_id):
    return values[int(str(conversation_id).replace("-", "")[-4:], 16) % len(values)]


def _normalize_text(value):
    value = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _stem(token):
    token = str(token or "")
    if len(token) > 5 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 5 and token.endswith("ing"):
        return token[:-3]
    if len(token) > 4 and token.endswith("ers"):
        return token[:-1]
    if len(token) > 4 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _words(value):
    return [_stem(token) for token in _normalize_text(value).split()]


def _contains_phrase(text, phrase):
    normalized_text = _normalize_text(text)
    normalized_phrase = _normalize_text(phrase)
    return bool(normalized_phrase and f" {normalized_phrase} " in f" {normalized_text} ")


def _offered_service_rows(conversation):
    if not getattr(conversation, "artisan_id", None):
        return []
    return list(
        ArtisanService.objects.filter(artisan=conversation.artisan, service__is_active=True)
        .select_related("service__category", "location")
        .order_by("service__category__name", "service__name", "service_id", "location_id")
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
        all_locations = list(Locations.objects.order_by("country", "state", "area", "pk"))

    records_by_service = {}
    for service_id in {str(row.service_id) for row in rows}:
        if service_has_general_coverage[service_id]:
            locations = all_locations or []
        else:
            locations = list(
                Locations.objects.filter(pk__in=location_ids_by_service[service_id])
                .order_by("country", "state", "area", "pk")
            )
        records_by_service[service_id] = [_location_record(location) for location in locations]
    return records_by_service


def request_options_for(conversation):
    """Return only this artisan’s active service and real location catalogue options."""
    rows = _offered_service_rows(conversation)
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
    services.sort(key=lambda item: (item["category_name"].casefold(), item["name"].casefold(), item["id"]))

    categories_by_id = {}
    for service in services:
        category_id = service["category_id"]
        category = categories_by_id.setdefault(
            category_id,
            {"id": category_id, "name": service["category_name"], "services": []},
        )
        category["services"].append(service)
    categories = sorted(categories_by_id.values(), key=lambda item: item["name"].casefold())

    locations_by_id = {}
    for service in services:
        for location in service["locations"]:
            record = locations_by_id.setdefault(location["id"], {**location, "service_ids": []})
            if service["id"] not in record["service_ids"]:
                record["service_ids"].append(service["id"])
    locations = sorted(
        locations_by_id.values(),
        key=lambda item: (item["country"].casefold(), item["state"].casefold(), item["area"].casefold(), item["id"]),
    )
    return {"categories": categories, "services": services, "locations": locations}


def _draft_values(conversation):
    draft = conversation.draft if isinstance(conversation.draft, dict) else {}
    if conversation.service_request_id and conversation.service_request:
        request = conversation.service_request
        location = request.location
        return {
            "service_id": str(request.service_id) if request.service_id else "",
            "category_id": str(request.category_id),
            "description": request.description or "",
            "additional_details": request.additional_details or "",
            "location_id": str(request.location_id),
            "country": location.country,
            "state": location.state,
            "area": location.area,
            "preferred_date": request.preferred_date.isoformat() if request.preferred_date else "",
            "preferred_time_start": request.preferred_time_start.isoformat(timespec="minutes") if request.preferred_time_start else "",
            "preferred_time_end": request.preferred_time_end.isoformat(timespec="minutes") if request.preferred_time_end else "",
            "preferred_time_note": request.preferred_time_note or "",
            "urgency": request.urgency,
            "budget": str(request.budget) if request.budget is not None else "",
            "budget_unknown": request.budget is None,
        }
    values = dict(draft)
    for key in ("service_id", "category_id", "location_id", "description", "additional_details", "country", "state", "area", "preferred_date", "preferred_time_start", "preferred_time_end", "preferred_time_note", "urgency", "budget"):
        value = values.get(key)
        values[key] = "" if value is None else str(value)
    values["budget_unknown"] = bool(values.get("budget_unknown", False))
    return values


def _request_state(conversation):
    values = _draft_values(conversation)
    missing_fields = []
    if not values.get("service_id"):
        missing_fields.append("service_id")
    if not str(values.get("description") or "").strip():
        missing_fields.append("description")
    if not values.get("location_id"):
        missing_fields.append("location_id")
    if not values.get("preferred_date"):
        missing_fields.append("preferred_date")
    submitted = bool(conversation.service_request_id)
    return {
        "status": "submitted" if submitted else "draft",
        "values": values,
        "missing_fields": missing_fields,
        "complete": not missing_fields,
        "ready_to_submit": not submitted and not missing_fields,
    }


def _location_records_for_conversation(conversation):
    rows = _offered_service_rows(conversation)
    draft = conversation.draft if isinstance(conversation.draft, dict) else {}
    service_id = str(draft.get("service_id") or "")
    category_id = str(draft.get("category_id") or "")
    if service_id:
        rows = [row for row in rows if str(row.service_id) == service_id]
    elif category_id:
        rows = [row for row in rows if str(row.service.category_id) == category_id]
    if not rows:
        return []
    if any(row.location_id is None for row in rows):
        locations = Locations.objects.order_by("country", "state", "area", "pk")
    else:
        location_ids = {row.location_id for row in rows if row.location_id}
        locations = Locations.objects.filter(pk__in=location_ids).order_by("country", "state", "area", "pk")
    return [_location_record(location) for location in locations]


def _service_intent(conversation, text):
    rows = _offered_service_rows(conversation)
    unique_services = {}
    for row in rows:
        unique_services[str(row.service_id)] = row.service
    services = list(unique_services.values())
    if not services:
        return {}

    normalized = _normalize_text(text)
    query_words = set(_words(text))
    expanded_words = set(query_words)
    alias_categories = set()
    for token in _normalize_text(text).split():
        for category in INTENT_ALIASES.get(token, set()):
            alias_categories.add(category)
            expanded_words.update(_words(category))

    scores = []
    category_scores = defaultdict(float)
    for service in services:
        service_name = _normalize_text(service.name)
        name_words = {
            _stem(word) for word in service_name.split()
            if _stem(word) not in GENERIC_SERVICE_TERMS
        }
        category_words = set(_words(service.category.name))
        exact_service = _contains_phrase(normalized, service.name)
        exact_category = _contains_phrase(normalized, service.category.name)
        service_query_words = expanded_words - category_words if exact_category else expanded_words
        name_overlap = (len(name_words & service_query_words) / len(name_words)) if name_words else 0.0
        direct_category = bool(category_words & expanded_words)
        alias_category = any(_stem(alias) in category_words for alias in alias_categories)
        category_signal = 1.0 if exact_category else (0.82 if direct_category else (0.78 if alias_category else 0.0))
        score = 1.0 if exact_service else max(0.58 * name_overlap + 0.40 * category_signal, 0.76 if exact_category else 0.0)
        scores.append((score, service, exact_service, category_signal))
        category_scores[str(service.category_id)] = max(category_scores[str(service.category_id)], category_signal, score * 0.55)

    exact_matches = [item for item in scores if item[2]]
    if len(exact_matches) == 1:
        service = exact_matches[0][1]
        return {"service_id": str(service.pk), "category_id": str(service.category_id)}

    scores.sort(key=lambda item: (-item[0], item[1].name.casefold(), item[1].pk))
    top_score, top_service, _exact, _signal = scores[0]
    second_score = scores[1][0] if len(scores) > 1 else 0.0
    if top_score >= 0.78 and top_score - second_score >= 0.16:
        return {"service_id": str(top_service.pk), "category_id": str(top_service.category_id)}

    # A clear category can be retained without guessing between multiple services.
    ranked_categories = sorted(category_scores.items(), key=lambda item: (-item[1], item[0]))
    if ranked_categories:
        category_id, category_score = ranked_categories[0]
        next_category_score = ranked_categories[1][1] if len(ranked_categories) > 1 else 0.0
        if category_score >= 0.72 and category_score - next_category_score >= 0.16:
            category_services = [service for service in services if str(service.category_id) == category_id]
            if len(category_services) == 1:
                service = category_services[0]
                return {"service_id": str(service.pk), "category_id": category_id}
            return {"category_id": category_id}
    return {}


def _location_is_negated(text, location_name):
    normalized = _normalize_text(text)
    phrase = _normalize_text(location_name)
    if not phrase:
        return False
    return bool(re.search(rf"\b(?:not|instead of|rather than|no longer)\s+(?:in\s+)?{re.escape(phrase)}\b", normalized))


def _match_location(conversation, text, service_hint=None):
    if service_hint and service_hint.get("service_id"):
        original = conversation.draft if isinstance(conversation.draft, dict) else {}
        service_id = service_hint["service_id"]
        conversation.draft = {**original, "service_id": service_id, "category_id": service_hint.get("category_id", original.get("category_id"))}
        try:
            records = _location_records_for_conversation(conversation)
        finally:
            conversation.draft = original
    elif service_hint and service_hint.get("category_id"):
        original = conversation.draft if isinstance(conversation.draft, dict) else {}
        conversation.draft = {**original, "service_id": "", "category_id": service_hint["category_id"]}
        try:
            records = _location_records_for_conversation(conversation)
        finally:
            conversation.draft = original
    else:
        records = _location_records_for_conversation(conversation)
    if not records:
        return {}

    area_matches = [
        record for record in records
        if record["area"] and _contains_phrase(text, record["area"])
        and not _location_is_negated(text, record["area"])
    ]
    if area_matches:
        matching = area_matches
        state_matches = [record for record in matching if _contains_phrase(text, record["state"]) and not _location_is_negated(text, record["state"])]
        if state_matches:
            matching = state_matches
        country_matches = [record for record in matching if _contains_phrase(text, record["country"]) and not _location_is_negated(text, record["country"])]
        if country_matches:
            matching = country_matches
        unique = {record["id"]: record for record in matching}
        if len(unique) == 1:
            record = next(iter(unique.values()))
            return {"location_id": record["id"], "country": record["country"], "state": record["state"], "area": record["area"]}
        fields = {}
        unique_areas = {record["area"] for record in matching}
        unique_states = {record["state"] for record in matching if _contains_phrase(text, record["state"]) and not _location_is_negated(text, record["state"])}
        unique_countries = {record["country"] for record in matching if _contains_phrase(text, record["country"]) and not _location_is_negated(text, record["country"])}
        if len(unique_areas) == 1:
            fields["area"] = next(iter(unique_areas))
        if len(unique_states) == 1:
            fields["state"] = next(iter(unique_states))
        if len(unique_countries) == 1:
            fields["country"] = next(iter(unique_countries))
        return fields

    hints = {}
    for field in ("state", "country"):
        values = {
            record[field] for record in records
            if record[field] and _contains_phrase(text, record[field])
            and not _location_is_negated(text, record[field])
        }
        if len(values) == 1:
            hints[field] = next(iter(values))
    return hints


def _parse_money(text):
    pattern = re.compile(
        r"(?P<currency>₦|ngn\s*)?"
        r"(?P<amount>[0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?|[0-9]+(?:\.[0-9]{1,2})?)"
        r"\s*(?P<scale>k|thousand)?\b",
        re.I,
    )
    matches = list(pattern.finditer(str(text or "")))
    if not matches:
        return None

    budget_terms = list(re.finditer(r"\b(?:budget|price|cost|quote)\b", str(text or ""), re.I))
    explicit = [match for match in matches if match.group("currency") or match.group("scale")]
    if explicit:
        match = explicit[0]
    elif budget_terms:
        after_term = [match for match in matches if match.start() >= budget_terms[0].end()]
        match = after_term[0] if after_term else matches[-1]
    else:
        match = matches[0]

    raw = match.group("amount").replace(",", "")
    try:
        amount = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    if match.group("scale"):
        amount *= Decimal("1000")
    if not amount.is_finite() or amount < 0:
        return None
    return amount.quantize(Decimal("0.01"))


def _next_weekday(today, weekday):
    delta = (weekday - today.weekday()) % 7
    return today + timedelta(days=delta or 7)


def _future_date(year, month, day, today):
    try:
        candidate = date(year, month, day)
    except ValueError:
        return None
    if candidate < today and year == today.year:
        try:
            candidate = date(year + 1, month, day)
        except ValueError:
            return None
    return candidate if candidate >= today else None


def _date_match_is_negated(raw, start):
    prefix = raw[max(0, start - 50):start]
    return bool(re.search(r"\b(?:not|instead of|rather than|no longer)\s*$", prefix))


def _numeric_date_match_is_confounded(raw, match):
    if match.group(3):
        return False
    prefix = raw[max(0, match.start() - 30):match.start()]
    suffix = raw[match.end():match.end() + 25]
    if re.match(r"\s*(?:a\.?m\.?|p\.?m\.?)\b", suffix):
        return True
    if re.match(r"\s*(?:in\s+the\s+)?(?:morning|afternoon|evening)\b", suffix):
        return True
    if re.match(r"\s*(?:-\s*)?(?:inch(?:es)?|in\b|cm\b|mm\b|feet|ft\b|meters?|metres?)\b", suffix):
        return True
    separator = raw[match.start() + len(match.group(1)):match.start() + len(match.group(1)) + 1]
    if separator == "-" and re.search(r"\b(?:between|from|at|around)\s*$", prefix):
        return True
    return False


def _has_preferred_date_reference(text):
    raw = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii").casefold()
    normalized = _normalize_text(text)
    if re.search(r"\b(?:today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", raw):
        return True
    if re.search(r"\bin\s+\d{1,2}\s+days?\b|\bdate\b", normalized):
        return True
    iso_matches = list(re.finditer(r"\b20\d{2}-\d{1,2}-\d{1,2}\b", raw))
    if iso_matches:
        return True
    numeric_pattern = r"\b(\d{1,2})[/.\-](\d{1,2})(?:[/.\-](20\d{2}))?\b"
    for match in re.finditer(numeric_pattern, raw):
        if _numeric_date_match_is_confounded(raw, match):
            continue
        return True
    month_pattern = r"january|february|march|april|may|june|july|august|september|october|november|december"
    return bool(
        re.search(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{month_pattern})(?:\s+20\d{{2}})?\b", normalized)
        or re.search(rf"\b(?:{month_pattern})\s+\d{{1,2}}(?:st|nd|rd|th)?(?:\s+20\d{{2}})?\b", normalized)
    )


def _parse_preferred_date(text, today=None):
    today = today or timezone.localdate()
    raw = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii").casefold()
    normalized = _normalize_text(text)
    candidates = []

    def add_candidate(value):
        if value is not None:
            candidates.append(value)

    for match in re.finditer(r"\b(?:today|tomorrow)\b", raw):
        if not _date_match_is_negated(raw, match.start()):
            add_candidate(today + timedelta(days=1) if match.group() == "tomorrow" else today)

    for match in re.finditer(r"\bin\s+(\d{1,2})\s+days?\b", normalized):
        if not _date_match_is_negated(normalized, match.start()):
            add_candidate(today + timedelta(days=int(match.group(1))))

    iso_matches = list(re.finditer(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", raw))
    iso_spans = [(match.start(), match.end()) for match in iso_matches]
    for match in iso_matches:
        if _date_match_is_negated(raw, match.start()):
            continue
        try:
            parsed = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            continue
        add_candidate(parsed if parsed >= today else None)

    numeric_pattern = r"\b(\d{1,2})[/.\-](\d{1,2})(?:[/.\-](20\d{2}))?\b"
    for match in re.finditer(numeric_pattern, raw):
        if any(match.start() < end and match.end() > start for start, end in iso_spans):
            continue
        if _date_match_is_negated(raw, match.start()) or _numeric_date_match_is_confounded(raw, match):
            continue
        # Day-first interpretation is deterministic and matches the local convention.
        day, month = int(match.group(1)), int(match.group(2))
        year = int(match.group(3)) if match.group(3) else today.year
        add_candidate(_future_date(year, month, day, today))

    months = {
        "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
        "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    }
    month_pattern = "|".join(months)
    month_matches = []
    for match in re.finditer(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({month_pattern})(?:\s+(20\d{{2}}))?\b", normalized):
        month_matches.append((match, "day_month"))
    for match in re.finditer(rf"\b({month_pattern})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(20\d{{2}}))?\b", normalized):
        month_matches.append((match, "month_day"))
    for match, format_name in month_matches:
        if _date_match_is_negated(normalized, match.start()):
            continue
        if format_name == "day_month":
            day, month_name, year_text = match.groups()
        else:
            month_name, day, year_text = match.groups()
        year = int(year_text) if year_text else today.year
        add_candidate(_future_date(year, months[month_name], int(day), today))

    weekdays = {name: index for index, name in enumerate(("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"))}
    for name, index in weekdays.items():
        for match in re.finditer(rf"\b(?:next\s+)?{name}\b", normalized):
            if not _date_match_is_negated(normalized, match.start()):
                add_candidate(_next_weekday(today, index))

    distinct = set(candidates)
    return next(iter(distinct)) if len(distinct) == 1 else None

def _clock_value(hour_text, minute_text, meridiem=None, *, twenty_four_hour=False):
    hour = int(hour_text)
    minute = int(minute_text or 0)
    if minute > 59:
        return None
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        marker = meridiem.lower().replace(".", "")
        if marker == "pm" and hour != 12:
            hour += 12
        elif marker == "am" and hour == 12:
            hour = 0
    elif twenty_four_hour and 0 <= hour <= 23:
        pass
    else:
        return None
    if hour > 23:
        return None
    return f"{hour:02d}:{minute:02d}"


def _parse_preferred_time(text):
    raw = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii").casefold()
    normalized = _normalize_text(text)
    range_match = re.search(
        r"\b(?:between|from)\s+(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?\s*(?:to|and|until|[-–])\s*"
        r"(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?\b",
        raw,
    )
    if range_match:
        h1, m1, meridiem1, h2, m2, meridiem2 = range_match.groups()
        meridiem1 = (meridiem1 or meridiem2 or "").replace(".", "")
        meridiem2 = (meridiem2 or meridiem1 or "").replace(".", "")
        is_24_hour = bool(m1 or m2 or int(h1) > 12 or int(h2) > 12)
        start = _clock_value(h1, m1, meridiem1 or None, twenty_four_hour=is_24_hour)
        end = _clock_value(h2, m2, meridiem2 or None, twenty_four_hour=is_24_hour)
        if start and end and end > start:
            return {"preferred_time_start": start, "preferred_time_end": end, "preferred_time_note": ""}

    single = re.search(r"\b(?:at|around|by|after|before)\s+(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?\b", raw)
    if single:
        hour, minute, meridiem = single.groups()
        meridiem = (meridiem or "").replace(".", "")
        is_24_hour = bool(minute or int(hour) > 12)
        start = _clock_value(hour, minute, meridiem or None, twenty_four_hour=is_24_hour)
        if start:
            return {"preferred_time_start": start, "preferred_time_end": None, "preferred_time_note": ""}

    labels = (
        ("early morning", "Early morning"), ("morning", "Morning"),
        ("afternoon", "Afternoon"), ("evening", "Evening"), ("after work", "After work"),
        ("before noon", "Before noon"), ("any time", "Flexible"), ("flexible", "Flexible"),
    )
    for phrase, label in labels:
        if _contains_phrase(normalized, phrase):
            return {"preferred_time_note": label, "preferred_time_start": None, "preferred_time_end": None}
    return {}

def _description_is_useful(text, *, conversation=None, service_hint=None, location_hint=None):
    normalized = f" {_normalize_text(text)} "
    if service_hint:
        service_id = service_hint.get("service_id")
        category_id = service_hint.get("category_id")
        service = Service.objects.filter(pk=service_id).select_related("category").first() if service_id else None
        category = service.category if service else ServiceCategory.objects.filter(pk=category_id).first() if category_id else None
        if service:
            normalized = normalized.replace(f" {_normalize_text(service.name)} ", " ")
        if category:
            normalized = normalized.replace(f" {_normalize_text(category.name)} ", " ")
    if conversation:
        for record in _location_records_for_conversation(conversation):
            for key in ("area", "state", "country"):
                value = record.get(key)
                if value:
                    normalized = normalized.replace(f" {_normalize_text(value)} ", " ")
    if location_hint:
        for key in ("area", "state", "country"):
            value = location_hint.get(key)
            if value:
                normalized = normalized.replace(f" {_normalize_text(value)} ", " ")
    normalized = re.sub(r"\b20\d{2}\s+\d{1,2}\s+\d{1,2}\b", " ", normalized)
    normalized = re.sub(r"\b\d{1,2}\s+\d{1,2}\s+(?:20\d{2})\b", " ", normalized)
    normalized = re.sub(r"\b(?:from|between|at|around|by|after|before)\s+\d{1,2}(?:\s+\d{2})?\s*(?:am|pm)?(?:\s+(?:to|and|until)\s+\d{1,2}(?:\s+\d{2})?\s*(?:am|pm)?)?\b", " ", normalized)
    normalized = re.sub(r"\b\d{1,2}(?:\s+\d{2})?\s*(?:am|pm)\b", " ", normalized)
    normalized = re.sub(r"\b(?:today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday|january|february|march|april|may|june|july|august|september|october|november|december|next|this)\b", " ", normalized)
    words = [word for word in normalized.split() if word not in STOPWORDS and not word.isdigit()]
    return len(words) >= 2


def _is_budget_unknown(text, *, budget_prompt=False):
    normalized = _normalize_text(text)
    explicit = re.search(
        r"\b(?:don t know|dont know|do not know|not sure|no budget|skip(?: the)? budget|skip it|skip|"
        r"rather not say|not ready yet|decide later|leave (?:it )?open|open to (?:a )?quote|budget is open)\b",
        normalized,
    )
    mentions_budget = re.search(r"\b(budget|price|cost|quote)\b", normalized)
    return bool(explicit and (mentions_budget or budget_prompt))


def _correction(text):
    return bool(CORRECTION_RE.search(text or ""))


def _parse_message_hints(*, conversation, body):
    if conversation.conversation_type != conversation.ConversationType.SERVICE:
        return {}
    if not body or conversation.service_request_id or not getattr(conversation, "customer_id", None):
        return {}

    text = body.strip()
    normalized = _normalize_text(text)
    draft = conversation.draft if isinstance(conversation.draft, dict) else {}
    correction = _correction(text)
    current_prompt = next_prompt(conversation=conversation)
    awaiting_budget = bool(current_prompt and current_prompt.get("field") == "budget")
    hints = {}

    service_hint = _service_intent(conversation, text)
    if service_hint and (not draft.get("service_id") or correction):
        current_service = str(draft.get("service_id") or "")
        if str(service_hint.get("service_id") or "") != current_service or (service_hint.get("category_id") and str(draft.get("category_id") or "") != str(service_hint["category_id"])):
            hints.update(service_hint)

    location_hint = _match_location(conversation, text, service_hint=service_hint)
    if location_hint:
        has_changed_location = any(
            str(draft.get(key) or "").casefold() != str(location_hint.get(key) or "").casefold()
            for key in ("location_id", "country", "state", "area") if key in location_hint
        )
        if not draft.get("location_id") or correction or has_changed_location:
            hints.update(location_hint)

    preferred_date = _parse_preferred_date(text)
    if preferred_date and (not draft.get("preferred_date") or correction or str(draft.get("preferred_date")) != preferred_date.isoformat()):
        hints["preferred_date"] = preferred_date.isoformat()
    elif draft.get("preferred_date") and correction and _has_preferred_date_reference(text):
        # Invalidate a superseded date when a correction is ambiguous or invalid;
        # the assistant will ask only for the missing preferred date.
        hints["preferred_date"] = None

    time_hints = _parse_preferred_time(text)
    if time_hints:
        time_changed = any(
            str(draft.get(key) or "") != str(value or "")
            for key, value in time_hints.items()
        )
        if not draft.get("preferred_time_start") or correction or time_changed:
            hints.update(time_hints)

    budget_unknown = _is_budget_unknown(text, budget_prompt=awaiting_budget)
    if budget_unknown and (not draft.get("budget_unknown") or correction or draft.get("budget")):
        hints["budget_unknown"] = True
    budget_reference = bool(re.search(r"\b(?:budget|price|cost|quote)\b", normalized))
    explicit_currency = bool(re.search(r"₦|\bngn\b|\b\d[\d,]*(?:\.\d{1,2})?\s*(?:k|thousand)\b", text, re.I))
    budget_amount_context = bool(re.search(
        r"\b(?:budget|price|cost|quote)\b\s*(?:(?:is|of|at|around|about|to)\s*)?(?:₦|ngn\s*)?"
        r"\d[\d,]*(?:\.\d{1,2})?\s*(?:k|thousand)?\b",
        text,
        re.I,
    ))
    amount = _parse_money(text) if budget_reference or awaiting_budget else None
    if amount is not None and (_has_preferred_date_reference(text) or time_hints):
        # Do not interpret a date or clock token as a budget. A nearby budget label,
        # currency symbol, or scale word makes a mixed natural-language reply clear.
        if not explicit_currency and not budget_amount_context:
            amount = None
    current_amount = _parse_money(str(draft.get("budget") or "")) if draft.get("budget") else None
    if amount is not None and not budget_unknown and (current_amount != amount or correction):
        hints["budget"] = amount
        hints["budget_unknown"] = False

    if not draft.get("description") or correction:
        if _description_is_useful(text, conversation=conversation, service_hint=service_hint, location_hint=location_hint) and not budget_unknown:
            hints["description"] = text
    elif ADDITIONAL_RE.search(text) and (not draft.get("additional_details") or correction):
        extra = re.sub(ADDITIONAL_RE, "", text, count=1).strip()
        if extra:
            hints["additional_details"] = extra

    return hints


def extract_message_hints(*, conversation, body):
    """Public pure-ish wrapper retained for focused automation tests and callers."""
    return _parse_message_hints(conversation=conversation, body=body)


def _missing_field(conversation):
    draft = conversation.draft or {}
    if not draft.get("service_id"):
        return "service"
    if not draft.get("description"):
        return "description"
    if not draft.get("location_id"):
        return "location"
    if not draft.get("preferred_date"):
        return "preferred_date"
    return None


def next_prompt(*, conversation):
    if conversation.service_request_id:
        return None
    field = _missing_field(conversation)
    draft = conversation.draft if isinstance(conversation.draft, dict) else {}
    if field is None:
        if not draft.get("budget") and not draft.get("budget_unknown"):
            return {
                "field": "budget",
                "text": "Do you have a budget in mind, or would you prefer an open quote? Budget is optional.",
                "optional": True,
                "allows_unknown": True,
            }
        return None
    if field == "service" and draft.get("category_id"):
        category_name = ServiceCategory.objects.filter(pk=draft["category_id"]).values_list("name", flat=True).first()
        text = f"Which listed service in {category_name} do you need?" if category_name else PROMPTS[field][0]
    elif field == "location" and draft.get("state"):
        text = f"Which area in {draft['state']} should the artisan visit? Choose it from the location catalogue."
    elif field == "location" and draft.get("country"):
        text = f"Which state or region and area in {draft['country']} should the artisan visit?"
    elif field == "location" and draft.get("area"):
        text = f"Which state and country is {draft['area']} in? I’ll check it against the location catalogue."
    else:
        text = _pick(PROMPTS[field], conversation.id)
    return {"field": field, "text": text, "optional": False, "allows_unknown": False}


def _request_context_payload(conversation, values):
    if conversation.conversation_type != conversation.ConversationType.SERVICE:
        return values
    values["request_state"] = _request_state(conversation)
    values["request_options"] = request_options_for(conversation) if not conversation.service_request_id else {"categories": [], "services": [], "locations": []}
    return values


def assistant_context(*, conversation, user, booking=None, offer=None, payment=None):
    state = derive_conversation_state(conversation=conversation, booking=booking, offer=offer, payment=payment)
    if conversation.conversation_type != conversation.ConversationType.SERVICE:
        return {
            "mode": "direct_chat",
            "reply": "This is a direct conversation. You can message the other participant normally.",
            "prompt": {"field": None, "text": "Direct conversation", "optional": True, "allows_unknown": True},
            "state": state,
            "suggestions": ["I have a question", "I’ll send a photo"],
        }

    is_customer = conversation.customer_id == getattr(user, "id", None)
    is_artisan = bool(conversation.artisan_id and conversation.artisan.user_id == getattr(user, "id", None))
    prompt = next_prompt(conversation=conversation)
    if prompt and is_artisan:
        field_labels = {
            "service": "the service they need",
            "description": "a clear description of the work",
            "location": "a catalogue-backed service location",
            "preferred_date": "a preferred date",
            "budget": "an optional budget",
        }
        reply = (
            "This customer request is still a draft. The customer is being guided to add "
            f"{field_labels[prompt['field']]}. No offer can be made until the customer reviews and submits it. "
            "You can ask a clarifying question here."
        )
        return _request_context_payload(conversation, {
            "mode": "waiting_customer_details",
            "reply": reply,
            "prompt": {"field": None, "text": reply, "optional": True, "allows_unknown": True},
            "state": state,
            "suggestions": ["Could you clarify the job?", "I’ll confirm my service area", "Please send a photo"],
        })

    if prompt and is_customer:
        intro = {
            "service": "I’ll use the artisan’s actual service catalogue. Choose the closest listed service; if more than one could fit, I’ll leave the choice to you.",
            "description": "I have the other details so far. Add a short description of the work, and you can correct any field before submission.",
            "location": "I’ll only use a location from the existing catalogue and this artisan’s available coverage. Choose the country, state, area, and matching location.",
            "preferred_date": "The service and location are saved. Choose a preferred date; it is a preference, not a confirmed booking.",
            "budget": "The required details are in the draft. Add a budget if you have one, or leave it open for the artisan to quote.",
        }[prompt["field"]]
        suggestions = (
            ["I’m open to a quote", "I’m not sure yet", "Review my request"]
            if prompt["field"] == "budget"
            else ["I’m not sure yet", "Let me correct a detail", "I’ll send a photo"]
        )
        return _request_context_payload(conversation, {
            "mode": "collecting_optional_details" if prompt.get("optional") else "collecting_details",
            "reply": intro,
            "prompt": prompt,
            "state": state,
            "suggestions": suggestions,
        })

    if not conversation.service_request_id and is_customer:
        reply = (
            "The required details are in your draft. Review the request below, make any corrections, and submit it when you’re ready. "
            "Budget and time preferences are optional; the preferred date does not confirm a booking."
        )
        return _request_context_payload(conversation, {
            "mode": "ready_for_review",
            "reply": reply,
            "prompt": {"field": None, "text": "Review your draft before submission.", "optional": True, "allows_unknown": True},
            "state": state,
            "suggestions": ["Review my request", "I’ll correct a detail", "I’ll send a photo"],
        })

    if not conversation.service_request_id and is_artisan:
        reply = "The customer has filled the required draft fields, but the request is not submitted yet. Wait for their review and submission before preparing an offer."
        return _request_context_payload(conversation, {
            "mode": "waiting_customer_submission",
            "reply": reply,
            "prompt": {"field": None, "text": reply, "optional": True, "allows_unknown": True},
            "state": state,
            "suggestions": ["I’ll wait for submission", "I have a clarifying question"],
        })

    if is_artisan:
        if state == "OPEN":
            reply = "The customer’s submitted request is ready to review. Send a clear offer with the work scope, labour, materials and timing, or ask a question first."
            suggestions = ["I’ll prepare an offer", "Could you send a photo?", "I have a question"]
        elif state == "NEGOTIATING":
            if offer and conversation.customer_id and offer.created_by_id == conversation.customer_id and offer.status == "PROPOSED":
                reply = "The customer has sent a counter offer. Review the changes and reply with a counter offer or a question."
                suggestions = ["I’ll review the counter offer", "Can we clarify the scope?", "I have a question"]
            else:
                reply = "Your offer is with the customer. Answer any questions here, or review a counter offer when it arrives."
                suggestions = ["I can clarify the scope", "I have a question", "I’ll check availability"]
        elif state == "BOOKING":
            reply = "The customer accepted the offer. Payment is being handled through the marketplace; wait for escrow confirmation before starting the service."
            suggestions = ["I’ll wait for payment confirmation", "I have a booking question"]
        elif state == "ACTIVE":
            if booking and booking.status == "ACCEPTED":
                reply = "Payment is secured in escrow. You can start the service from the booking controls, then share progress and photos here."
            elif booking and booking.status == "COMPLETION_REQUESTED":
                reply = "You submitted a completion request. The customer can now review the work and confirm completion or contact support."
            else:
                reply = "The service is in progress. Share updates or photos here, then submit a completion request when the work is finished."
            suggestions = ["I’ll share a progress update", "I’ll send a photo", "I have a question"]
        elif state == "COMPLETED":
            reply = "The booking is complete. Thank the customer here, or contact support if any follow-up is needed."
            suggestions = ["Thanks for choosing my service", "I have a follow-up question"]
        elif state == "DISPUTED":
            reply = "Customer care is reviewing the service issue. Add relevant updates or evidence here and follow the linked support case."
            suggestions = ["I’ll add supporting details", "I have a question for support"]
        else:
            reply = "This service conversation is closed. You can still review the messages and contact support if needed."
            suggestions = ["I have a follow-up question", "I’ll contact support"]
    else:
        if state == "OPEN":
            reply = "Your submitted request is visible to the artisan. You can ask a question here while they review it and prepare an offer."
            suggestions = ["I have a question", "I’ll send a photo", "When can you start?"]
        elif state == "NEGOTIATING":
            reply = "Review the artisan’s offer and scope below. You can ask a question, accept, decline or send a counter offer."
            suggestions = ["I have a question", "I’ll send a counter offer", "I’m ready to accept"]
        elif state == "BOOKING":
            reply = "Your offer is accepted and a booking is in place. Complete payment through the secure marketplace flow to move forward."
            suggestions = ["I’ll pay securely", "I have a question about payment"]
        elif state == "ACTIVE":
            reply = "Your service is underway. Follow progress and photos here, and confirm the work when the artisan submits completion."
            suggestions = ["I have a progress question", "I’ll send a photo"]
        elif state == "COMPLETED":
            reply = "Everything is marked complete. Leave a review, or contact support if anything still needs attention."
            suggestions = ["I’ll leave a review", "I need help with the completed service"]
        elif state == "DISPUTED":
            reply = "Customer care is handling this now. Keep useful evidence and updates in this conversation, and follow the linked case."
            suggestions = ["I’ll add more details", "I have a question for support"]
        else:
            reply = "Tell me what you need in your own words. I’ll keep the useful details organized without making you fill out a long form."
            suggestions = ["Tell me what happens next", "I’ll send a photo"]

    return _request_context_payload(conversation, {
        "mode": state.lower(),
        "reply": reply,
        "prompt": {"field": None, "text": reply, "optional": True, "allows_unknown": True},
        "state": state,
        "suggestions": suggestions,
    })


def apply_message_automation(*, conversation, user, body):
    if conversation.conversation_type != conversation.ConversationType.SERVICE:
        return conversation, None
    if conversation.customer_id != user.id or conversation.service_request_id:
        return conversation, None
    hints = extract_message_hints(conversation=conversation, body=body)
    if not hints:
        return conversation, None
    from .services import apply_request_patch
    # A chat message may save parsed draft fields, but must never submit the request.
    hints["submit_request"] = False
    try:
        service_request, updated = apply_request_patch(conversation=conversation, user=user, payload=hints)
    except Exception:
        logger.exception("Conversation draft automation failed", extra={"conversation_id": str(conversation.id)})
        return conversation, None
    return updated, service_request
