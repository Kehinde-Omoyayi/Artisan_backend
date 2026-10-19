import json
import logging
from django_redis import get_redis_connection

logger = logging.getLogger(__name__)
CHANNEL_PREFIX = "artisan:conversation:event:"

def publish_event(*, conversation_id, payload):
    try: get_redis_connection("default").publish(f"{CHANNEL_PREFIX}{conversation_id}", json.dumps(payload, default=str, separators=(",", ":")))
    except Exception: logger.exception("conversation_realtime_publish_failed conversation=%s", conversation_id)
