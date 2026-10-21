"""Worldwide geography independent of artisan coverage. See data/GEOGRAPHY-SOURCE.md."""
import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def world_geography():
    return json.loads((Path(__file__).parent / 'data' / 'world-geography.json').read_text(encoding='utf-8'))


def canonical_location(country, state, area):
    """Return catalogue spelling for a valid country/state/locality triple."""
    def find(value, candidates):
        key = str(value or '').strip().casefold()
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
    return {'country': country, 'state': state, 'area': area}
