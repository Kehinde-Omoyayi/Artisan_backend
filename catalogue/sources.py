"""The marketplace catalogue: world geography and the institution register.

This data used to be shipped inside the browser app. It belongs with the API:
the browser and the server both read it from here, so there is one copy and
one set of search rules.

Nothing in this module touches the `Locations` model or the institution table.
The published JSON files are the source of truth for the pickers and for the
validation the browser app performs on a saved profile.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"
INSTITUTIONS = DATA / "institutions"

DEFAULTS = {
    "country": "Nigeria",
    "state": "Ogun",
    "area": "Abeokuta",
    "nearest_institution": "Federal University of Agriculture, Abeokuta",
    "nearest_institution_abbreviation": "FUNAAB",
}


# --------------------------------------------------------------------------
# World geography
# --------------------------------------------------------------------------


@lru_cache(maxsize=1)
def world_geography() -> dict:
    """country -> {state: [areas]}, parsed once per process (~2 MB)."""
    return json.loads((DATA / "world-geography.json").read_text(encoding="utf-8"))


def geography_index() -> dict:
    """country -> [states], plus the per-country file for each area list."""
    return json.loads((DATA / "world-geography-index.json").read_text(encoding="utf-8"))


def country_areas(slug: str) -> dict:
    """state -> [areas] for one country file ('' when the slug is unknown)."""
    safe = "".join(ch for ch in str(slug or "") if ch.isalnum() or ch in "-_")[:80]
    if not safe:
        return {}
    path = DATA / "world-geography" / f"{safe}.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def canonical_location(country, state, area):
    """Return the catalogue spelling for a valid country/state/locality triple."""

    def find(value, candidates):
        key = str(value or "").strip().casefold()
        return next((name for name in candidates if name.casefold() == key), None)

    data = world_geography()
    country = find(country, data)
    if country is None:
        return None
    state = find(state, data[country])
    if state is None:
        return None
    area = find(area, data[country][state])
    if area is None:
        return None
    return {"country": country, "state": state, "area": area}


# --------------------------------------------------------------------------
# Institution register
# --------------------------------------------------------------------------


@lru_cache(maxsize=1)
def index() -> dict:
    return json.loads((INSTITUTIONS / "index.json").read_text(encoding="utf-8"))


def _institution_sort_key(r):
    """Alphabetical by name; Nigeria always first when no country filter."""
    is_nigeria = 0 if r["country"].casefold() == "nigeria" else 1
    return (
        is_nigeria,
        r["name"].casefold(),
        r["country"].casefold(),
        r["state"].casefold(),
        r["area"].casefold(),
        r["source_id"],
    )


@lru_cache(maxsize=32)
def institutions(country):
    entry = next(
        (
            item
            for item in index()["countries"]
            if item["country"].casefold() == str(country).casefold()
        ),
        None,
    )
    rows = json.loads((INSTITUTIONS / entry["file"]).read_text(encoding="utf-8")) if entry else []
    return sorted(
        rows,
        key=lambda r: (
            r["name"].casefold(),
            r["state"].casefold(),
            r["area"].casefold(),
            r["source_id"],
        ),
    )


@lru_cache(maxsize=1)
def all_institutions():
    return tuple(
        sorted(
            (
                row
                for entry in index()["countries"]
                for row in institutions(entry["country"])
            ),
            key=_institution_sort_key,
        )
    )


@lru_cache(maxsize=1)
def source_records():
    return {row["source_id"]: row for row in all_institutions()}


@lru_cache(maxsize=1)
def _search_blobs():
    """One lower-cased haystack per institution, built once.

    Searching rebuilt the name/abbreviation/alias list for all 30k rows on every
    keystroke (~70 ms); matching against a prepared string is ~5 ms.
    """
    return {
        row["source_id"]: " ".join(
            [row["name"], *(row.get("abbreviations") or []), *(row.get("aliases") or [])]
        ).casefold()
        for row in all_institutions()
    }


def institution_record(source_id):
    return source_records().get(str(source_id or "").strip())


def catalogue_page(params):
    """Page the register: q / country / state / area / id, fifty rows at a time."""
    key = str(params.get("id") or "").strip()[:100]
    if key:
        row = institution_record(key)
        records = [row] if row else []
        return {
            "count": len(records),
            "results": [
                {
                    **row,
                    "id": row["source_id"],
                    "abbreviation": next(iter(row.get("abbreviations") or []), ""),
                    "campus": "",
                }
            ]
            if records
            else [],
            "offset": 0,
            "limit": 50,
            "source": "catalogue",
        }
    country = str(params.get("country") or "").strip()
    records = institutions(country) if country else all_institutions()
    for field in ("country", "state", "area"):
        value = str(params.get(field) or "").strip().casefold()[:150]
        if value:
            records = [r for r in records if r[field].casefold() == value]
    if country:
        records = sorted(
            records,
            key=lambda r: (
                r["name"].casefold(),
                r["state"].casefold(),
                r["area"].casefold(),
                r["source_id"],
            ),
        )
    elif not isinstance(records, tuple):
        records = sorted(records, key=_institution_sort_key)
    query = str(params.get("q") or "").strip().casefold()[:180]
    if query:
        blobs = _search_blobs()
        records = [r for r in records if query in blobs.get(r["source_id"], r["name"].casefold())]
    try:
        offset = max(0, min(int(params.get("offset") or 0), 1000000))
    except (TypeError, ValueError):
        offset = 0
    results = []
    for row in records[offset : offset + 50]:
        results.append(
            {
                **row,
                "id": row["source_id"],
                "abbreviation": next(iter(row.get("abbreviations") or []), ""),
                "campus": "",
            }
        )
    return {
        "count": len(records),
        "results": results,
        "offset": offset,
        "limit": 50,
        "source": "catalogue",
    }
