"""Versioned public-discovery response caching on the configured Django cache."""
from __future__ import annotations

import hashlib
import json
import logging
import unicodedata
from uuid import UUID

from django.core.cache import cache

logger = logging.getLogger(__name__)

DISCOVERY_CACHE_TTL = 5 * 60
_DISCOVERY_CACHE_VERSION_KEY = "artisan-discovery:cache-generation:v3"
_DISCOVERY_CACHE_PREFIX = "artisan-discovery:results:v3"


def normalize_text(value, *, limit=160):
    value = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(value.split()).casefold()[:limit]


def _normalize_institution_id(value):
    text = normalize_text(value, limit=64)
    if not text:
        return ""
    try:
        return str(UUID(text))
    except (TypeError, ValueError, AttributeError):
        return text


def _normalize_boolean_filter(value):
    text = normalize_text(value, limit=10)
    if text in {"1", "true", "yes", "on"}:
        return "true"
    if text in {"0", "false", "no", "off"}:
        return "false"
    return text


def current_discovery_generation():
    """Read the shared invalidation generation, or return None if Redis is unavailable."""
    try:
        generation = cache.get(_DISCOVERY_CACHE_VERSION_KEY)
        if generation is None:
            cache.add(_DISCOVERY_CACHE_VERSION_KEY, 1, timeout=None)
            generation = cache.get(_DISCOVERY_CACHE_VERSION_KEY, 1)
        return int(generation)
    except Exception:
        logger.warning("Public artisan discovery cache read failed; bypassing cache.")
        return None


def invalidate_public_discovery_cache():
    """Invalidate all discovery variants without scanning Redis keys.

    Promotion campaign activation, pause, revocation, expiry, capacity changes and public eligibility
    transitions call this helper so cached discovery responses do not outlive paid-placement visibility.
    """
    try:
        if not cache.add(_DISCOVERY_CACHE_VERSION_KEY, 1, timeout=None):
            cache.incr(_DISCOVERY_CACHE_VERSION_KEY)
    except Exception:
        logger.warning("Public artisan discovery cache invalidation failed.")


def discovery_cache_key(request, paginator, *, default_ordering):
    """Build a bounded key from every result-changing public filter and page input."""
    generation = current_discovery_generation()
    if generation is None:
        return None

    params = request.query_params
    filters = {
        "q": normalize_text(params.get("q"), limit=160),
        "category_name": normalize_text(params.get("category_name"), limit=100),
        "service_name": normalize_text(params.get("service_name"), limit=150),
        "country": normalize_text(params.get("country"), limit=100),
        "state": normalize_text(params.get("state"), limit=100),
        "area": normalize_text(params.get("area"), limit=150),
        "institution_id": _normalize_institution_id(params.get("institution_id")),
        "available_anywhere": _normalize_boolean_filter(params.get("available_anywhere")),
        # Preserve the supported legacy filter names as distinct cache dimensions.
        "average_rating": normalize_text(params.get("average_rating"), limit=20),
        "average_rating__gte": normalize_text(params.get("average_rating__gte"), limit=20),
        "average_rating__lte": normalize_text(params.get("average_rating__lte"), limit=20),
        "artisan_service_id": normalize_text(params.get("artisan_services__service"), limit=64),
        "service_category_id": normalize_text(params.get("artisan_services__service__category"), limit=100),
        "service_location_id": _normalize_institution_id(params.get("artisan_services__location")),
    }
    limit = paginator.get_limit(request)
    offset = paginator.get_offset(request)
    raw_ordering = params.get("ordering") or ",".join(default_ordering)
    ordering = ",".join(
        normalize_text(term, limit=40)
        for term in str(raw_ordering).split(",")
    )
    host = f"{request.scheme}://{request.get_host()}"
    payload = {
        "generation": generation,
        "eligibility": "active-verified-organic-results-separate-promotions-v3",
        "filters": filters,
        "limit": limit,
        "offset": offset,
        "ordering": ordering,
        "host": host,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"{_DISCOVERY_CACHE_PREFIX}:{generation}:{digest}"
