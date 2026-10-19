import hashlib

import redis
from django.conf import settings


redis_client = redis.Redis.from_url(
    settings.NOTIFICATION_REDIS_URL,
    decode_responses=True,
    socket_connect_timeout=3,
    socket_timeout=5,
)


def _digest(event_key):
    return hashlib.sha256(
        event_key.encode("utf-8")
    ).hexdigest()


def sent_key(event_key):
    return f"artisan:notification:sent:{_digest(event_key)}"


def lock_key(event_key):
    return f"artisan:notification:lock:{_digest(event_key)}"


def already_sent(event_key):
    return bool(redis_client.exists(sent_key(event_key)))


def acquire_lock(event_key, token):
    return bool(
        redis_client.set(
            lock_key(event_key),
            token,
            nx=True,
            ex=settings.NOTIFICATION_LOCK_TTL,
        )
    )


def mark_sent(event_key):
    redis_client.set(
        sent_key(event_key),
        "1",
        ex=settings.NOTIFICATION_SENT_TTL,
    )


def release_lock(event_key, token):
    key = lock_key(event_key)

    current_token = redis_client.get(key)

    if current_token == token:
        redis_client.delete(key)