"""Keyed OTP cache values and non-reversible cache-key helpers."""
import hashlib
import hmac

from django.conf import settings


def _keyed_digest(label, value):
    key = str(settings.SECRET_KEY).encode()
    message = f"{label}\0{value}".encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def otp_cache_key(purpose, email):
    normalized = str(email or "").strip().casefold()
    return f"auth-otp:{purpose}:{_keyed_digest('email', normalized)}"


def otp_attempts_cache_key(purpose, email):
    normalized = str(email or "").strip().casefold()
    return f"auth-otp-attempts:{purpose}:{_keyed_digest('email', normalized)}"


def otp_cooldown_cache_key(purpose, email):
    normalized = str(email or "").strip().casefold()
    return f"auth-otp-cooldown:{purpose}:{_keyed_digest('email', normalized)}"


def hash_otp_code(purpose, email, code):
    normalized = str(email or "").strip().casefold()
    return _keyed_digest(f"otp:{purpose}:{normalized}", str(code))


def verify_otp_code(purpose, email, code, stored_digest):
    if not isinstance(stored_digest, str) or not stored_digest:
        return False
    candidate = hash_otp_code(purpose, email, code)
    return hmac.compare_digest(candidate, stored_digest)


def reset_token_cache_key(token):
    """Use a digest of the high-entropy reset token as the cache key, not the token itself."""
    digest = hashlib.sha256(str(token).encode("utf-8")).hexdigest()
    return f"auth-reset-token:{digest}"
