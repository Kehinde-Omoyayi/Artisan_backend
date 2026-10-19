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
