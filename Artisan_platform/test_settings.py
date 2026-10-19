"""Isolated API test settings; never reads live provider values from the environment."""
import os
from cryptography.fernet import Fernet

_test_values = {
    "ENVIRONMENT": "test",
    "DEBUG": "true",
    "SECRET_KEY": "test-only-key-do-not-use-in-production",
    "ALLOWED_HOSTS": "localhost,127.0.0.1,testserver",
    "CSRF_TRUSTED_ORIGINS": "http://localhost,http://127.0.0.1",
    "DB_ENGINE": "sqlite",
    "REDIS_URL": "redis://127.0.0.1:6379/0",
    "USE_S3": "false",
    "MONO_SECRET_KEY": "test-only-placeholder",
    "KYC_ENCRYPTION_KEY": Fernet.generate_key().decode("ascii"),
    "FLW_SECRET_KEY": "test-only-placeholder",
    "FLW_PUBLIC_KEY": "test-only-placeholder",
    "FLW_SECRET_HASH": "test-only-placeholder",
    "FLW_REDIRECT_URL": "http://localhost/payments/callback",
    "FLW_TRANSFER_CALLBACK_URL": "http://localhost/payments/transfer",
    "EMAIL_BACKEND": "django.core.mail.backends.locmem.EmailBackend",
}
os.environ.update(_test_values)
del _test_values

from .api_settings import *  # noqa: F401,F403,E402

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "api-package-test-cache",
    }
}
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
