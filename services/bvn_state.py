from django.core.cache import cache


BVN_STATE_TIMEOUT = 600


def _key(prefix, verification_id):
    return f"bvn:{prefix}:{verification_id}"


def set_bvn_state(verification_id, **data):
    cache.set(
        _key("state", verification_id),
        data,
        timeout=BVN_STATE_TIMEOUT,
    )


def get_bvn_state(verification_id):
    return (
        cache.get(
            _key("state", verification_id)
        )
        or {}
    )


def delete_bvn_state(verification_id):
    cache.delete(
        _key("state", verification_id)
    )


def set_bvn_session(verification_id, session_id):
    cache.set(
        _key("session", verification_id),
        session_id,
        timeout=BVN_STATE_TIMEOUT,
    )


def get_bvn_session(verification_id):
    return cache.get(
        _key("session", verification_id)
    )


def delete_bvn_session(verification_id):
    cache.delete(
        _key("session", verification_id)
    )


def acquire_bvn_lock(verification_id, step):
    return cache.add(
        _key(f"lock:{step}", verification_id),
        "1",
        timeout=120,
    )


def release_bvn_lock(verification_id, step):
    cache.delete(
        _key(f"lock:{step}", verification_id)
    )