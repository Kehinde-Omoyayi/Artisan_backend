"""Frontend-owned JSON catalogue and form snapshots; no backend catalogue lookup."""

import json
from functools import lru_cache
from pathlib import Path
from .geography import canonical_location

ROOT = Path(__file__).resolve().parents[1] / "static/data/institutions"
DEFAULTS = {
    "country": "Nigeria",
    "state": "Ogun",
    "area": "Abeokuta",
    "nearest_institution": "Federal University of Agriculture, Abeokuta",
    "nearest_institution_abbreviation": "FUNAAB",
}


@lru_cache(maxsize=1)
def index():
    return json.loads((ROOT / "index.json").read_text(encoding="utf-8"))


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
    rows = json.loads((ROOT / entry["file"]).read_text(encoding="utf-8")) if entry else []
    # Always return alphabetically sorted within the country
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


def institution_record(source_id):
    return source_records().get(str(source_id or "").strip())


def catalogue_page(params):
    """Public read-only frontend JSON search. Every geographic parameter is optional."""
    key = str(params.get("id") or "").strip()[:100]
    if key:
        row = institution_record(key)
        records = [row] if row else []
    else:
        country = str(params.get("country") or "").strip()
        records = institutions(country) if country else all_institutions()
    for field in ("country", "state", "area"):
        value = str(params.get(field) or "").strip().casefold()[:150]
        if value:
            records = [r for r in records if r[field].casefold() == value]
    # Directly reduce institution list when country/state/area is present — re-sort to keep alphabetical
    has_country = bool(str(params.get("country") or "").strip())
    if has_country:
        records = sorted(
            records,
            key=lambda r: (
                r["name"].casefold(),
                r["state"].casefold(),
                r["area"].casefold(),
                r["source_id"],
            ),
        )
    else:
        records = sorted(records, key=_institution_sort_key)
    query = str(params.get("q") or "").strip().casefold()[:180]
    if query:
        records = [
            r
            for r in records
            if any(
                query in str(value).casefold()
                for value in [
                    r["name"],
                    *(r.get("abbreviations") or []),
                    *(r.get("aliases") or []),
                ]
            )
        ]
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
        "source": "frontend-json",
    }


def initialize_profile_location(form):
    if form.is_bound:
        return
    if not any(
        str(form.initial.get(k) or "").strip()
        for k in ("state", "area", "nearest_institution")
    ) and str(form.initial.get("country") or "").strip() in ("", "Nigeria"):
        form.initial.update(DEFAULTS)


def clean_profile_location(form, cleaned):
    selected = str(cleaned.get("nearest_institution") or "").strip()
    source_id = str(cleaned.get("nearest_institution_source_id") or "").strip()
    original = str(form.initial.get("nearest_institution") or "")
    if selected:
        match = institution_record(source_id) if source_id else None
        if source_id and (not match or match["name"].casefold() != selected.casefold()):
            form.add_error(
                "nearest_institution", "Choose an institution from the search results."
            )
            return cleaned
        if not match:
            candidates = [
                r
                for r in all_institutions()
                if r["name"].casefold() == selected.casefold()
            ]
            # A unique typed name can resolve even when no location was chosen.
            if len(candidates) == 1:
                match = candidates[0]
            elif candidates:
                compatible = [
                    r
                    for r in candidates
                    if all(
                        not str(cleaned.get(k) or "").strip()
                        or r[k].casefold() == str(cleaned[k]).strip().casefold()
                        for k in ("country", "state", "area")
                    )
                ]
                if len(compatible) == 1:
                    match = compatible[0]
                else:
                    form.add_error(
                        "nearest_institution",
                        "Several institutions have this name. Choose a search result showing its location.",
                    )
                    return cleaned
        if match:
            # User chose to align geography, rather than prohibit an out-of-area choice.
            cleaned.update({k: match[k] for k in ("country", "state", "area")})
            cleaned["nearest_institution"] = match["name"]
            cleaned["nearest_institution_abbreviation"] = next(
                iter(match.get("abbreviations") or []), ""
            )
            cleaned["nearest_institution_source_id"] = match["source_id"]
            return cleaned
        changed = any(
            str(cleaned.get(k) or "").strip().casefold()
            != str(form.initial.get(k) or "").strip().casefold()
            for k in ("country", "state", "area")
        )
        if selected == original and not changed:
            cleaned["nearest_institution_abbreviation"] = str(
                form.initial.get("nearest_institution_abbreviation") or ""
            )
            return cleaned
        form.add_error(
            "nearest_institution",
            "Choose an institution from the frontend catalogue, or clear it.",
        )
        return cleaned
    cleaned["nearest_institution"] = ""
    cleaned["nearest_institution_abbreviation"] = ""
    cleaned["nearest_institution_source_id"] = ""
    current = {
        k: str(cleaned.get(k) or "").strip() for k in ("country", "state", "area")
    }
    if all(current.values()):
        canonical = canonical_location(**current)
        if canonical:
            cleaned.update(canonical)
        elif any(
            current[k].casefold() != str(form.initial.get(k) or "").strip().casefold()
            for k in current
        ):
            form.add_error("area", "Choose an area matching your country and state.")
    return cleaned
