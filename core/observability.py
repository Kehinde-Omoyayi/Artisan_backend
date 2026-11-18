import logging
import time
from functools import wraps

logger = logging.getLogger("marketplace.observability")


def log_event(event, *, request_id="", actor_id=None, resource_type="", resource_id="", **fields):
    payload = {"event": event, "request_id": request_id, "actor_id": actor_id, "resource_type": resource_type, "resource_id": resource_id, **fields}
    logger.info("marketplace_event %s", payload, extra=payload)


def timed_operation(name):
    def decorator(func):
        @wraps(func)
        def wrapped(*args, **kwargs):
            started = time.perf_counter()
            try: return func(*args, **kwargs)
            finally: log_event("operation_timing", operation=name, elapsed_ms=round((time.perf_counter() - started) * 1000, 2))
        return wrapped
    return decorator


# --------------------------------------------------------------------------- #
# Request latency sampling
# --------------------------------------------------------------------------- #
# A bounded ring buffer of recent server-side durations, per endpoint and overall.
# It is deliberately cheap: one list operation per request, capped in size, stored
# in the cache so it works across workers and never grows without limit.
_SAMPLE_CAP = 500
_OVERALL_KEY = "obs:latency:all:v1"
_ENDPOINT_KEY = "obs:latency:endpoint:{name}:v1"


def _normalise_path(path):
    """Collapse identifiers so each endpoint keeps one bucket."""
    parts = [part for part in str(path or "").split("/") if part]
    cleaned = []
    for part in parts:
        if len(part) >= 16 or part.isdigit() or (("-" in part) and len(part) > 8):
            cleaned.append("{}")
        else:
            cleaned.append(part[:40])
    return "/" + "/".join(cleaned[:8])


def record_latency(path, elapsed_ms, status_code=None):
    from django.core.cache import cache

    sample = {"ms": round(float(elapsed_ms), 2), "path": _normalise_path(path), "status": int(status_code or 0)}
    try:
        for key in (_OVERALL_KEY, _ENDPOINT_KEY.format(name=sample["path"])):
            values = cache.get(key) or []
            values.append(sample)
            cache.set(key, values[-_SAMPLE_CAP:], 3600)
    except Exception:  # pragma: no cover - sampling must never break a request
        logger.exception("latency_sample_failed")


def _percentile(sorted_values, fraction):
    if not sorted_values:
        return None
    index = min(len(sorted_values) - 1, max(0, int(round(fraction * (len(sorted_values) - 1)))))
    return round(sorted_values[index], 2)


def summarise(values):
    ordered = sorted(values)
    if not ordered:
        return {"count": 0, "p50": None, "p95": None, "p99": None, "max": None, "average": None}
    return {
        "count": len(ordered),
        "p50": _percentile(ordered, 0.50),
        "p95": _percentile(ordered, 0.95),
        "p99": _percentile(ordered, 0.99),
        "max": round(ordered[-1], 2),
        "average": round(sum(ordered) / len(ordered), 2),
    }


def latency_samples(window_minutes=60):
    """Percentiles for all sampled requests and for the slowest endpoints."""
    from django.core.cache import cache

    samples = cache.get(_OVERALL_KEY) or []
    del window_minutes  # the buffer is already a short rolling window
    overall = summarise([row["ms"] for row in samples])
    error_rate = 0.0
    if samples:
        errors = sum(1 for row in samples if row["status"] >= 500)
        error_rate = round(errors * 100 / len(samples), 2)
    by_path = {}
    for row in samples:
        by_path.setdefault(row["path"], []).append(row["ms"])
    endpoints = [
        {"endpoint": path, **summarise(values)}
        for path, values in sorted(by_path.items(), key=lambda item: -max(item[1]))[:10]
    ]
    return {"overall": overall, "error_rate_percent": error_rate, "slowest_endpoints": endpoints,
            "samples": len(samples)}
