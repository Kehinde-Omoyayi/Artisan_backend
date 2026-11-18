"""Encrypt short-lived email secrets passed through the asynchronous task broker."""
import base64
import hashlib

from cryptography.fernet import Fernet
from django.conf import settings


def _mail_cipher():
    material = hashlib.sha256(
        (settings.SECRET_KEY + ":staff-recovery-email").encode("utf-8")
    ).digest()
    return Fernet(base64.urlsafe_b64encode(material))


def encrypt_staff_code(code: str) -> str:
    return _mail_cipher().encrypt(str(code).encode("ascii")).decode("ascii")


def decrypt_staff_code(encrypted_code: str) -> str:
    return _mail_cipher().decrypt(str(encrypted_code).encode("ascii")).decode("ascii")


# The original domain-specific names remain as aliases for Phase 14 recovery tasks.
# Account-verification and password-reset email tasks use the same authenticated
# encryption boundary, with their own purpose labels in the email content.
encrypt_email_code = encrypt_staff_code
decrypt_email_code = decrypt_staff_code
