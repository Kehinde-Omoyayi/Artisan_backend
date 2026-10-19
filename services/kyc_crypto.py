import json

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


def _fernet():
    return Fernet(settings.KYC_ENCRYPTION_KEY.encode())


def encrypt_value(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_value(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("Invalid encrypted KYC payload.") from exc


def encrypt_nin(nin: str) -> str:
    return encrypt_value(nin)


def decrypt_nin(token: str) -> str:
    return decrypt_value(token)


def encrypt_bvn(bvn: str) -> str:
    return encrypt_value(bvn)


def decrypt_bvn(token: str) -> str:
    return decrypt_value(token)


def encrypt_otp(otp: str) -> str:
    return encrypt_value(otp)


def decrypt_otp(token: str) -> str:
    return decrypt_value(token)


def encrypt_identity_name(parts: dict) -> str:
    """Encrypt provider-confirmed identity name components as one payload."""
    if not isinstance(parts, dict):
        raise ValueError("Identity name payload must be an object.")
    safe_parts = {
        key: str(parts.get(key) or "").strip()
        for key in ("first_name", "middle_name", "last_name")
    }
    return encrypt_value(json.dumps(safe_parts, ensure_ascii=False, separators=(",", ":")))


def decrypt_identity_name(token: str) -> dict:
    """Decrypt a stored name only for an in-process comparison; never serialize it."""
    if not token:
        raise ValueError("Verified identity name is unavailable.")
    try:
        parts = json.loads(decrypt_value(token))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid encrypted identity name.") from exc
    if not isinstance(parts, dict) or not all(
        isinstance(parts.get(key), str) for key in ("first_name", "middle_name", "last_name")
    ):
        raise ValueError("Invalid encrypted identity name.")
    return parts
