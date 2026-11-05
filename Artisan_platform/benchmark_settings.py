"""Benchmark settings: the test configuration, but against a real file database.

SQLite is used because it is the only engine available in this environment. The
numbers produced here are an upper bound on Python/ORM cost, not a claim about
PostgreSQL throughput - see docs/MANAGER_CONSOLE_PERFORMANCE.md.
"""
import os

from .test_settings import *  # noqa: F401,F403

# test_settings mints a random encryption key per process, which makes a seeded
# row unreadable to a server started in another process. The benchmark database
# is a throwaway SQLite file, so the key is pinned to a throwaway value too.
# Never reuse this key anywhere real.
KYC_ENCRYPTION_KEY = os.environ.get(
    "BENCHMARK_KYC_ENCRYPTION_KEY",
    "RJXbfO5yshxcOiDr-moQWslsGjNBRWoN5h-405AowWI=",
)

# Emails (staff sign-in codes, verification codes) are written to disk so a
# developer can finish a real sign-in flow locally without a mail server. This is
# a benchmark-only convenience; production uses its configured SMTP backend.
EMAIL_BACKEND = "django.core.mail.backends.filebased.EmailBackend"
EMAIL_FILE_PATH = os.environ.get("BENCHMARK_EMAIL_DIR", "/tmp/manager-emails")

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("BENCHMARK_DB_PATH", "/tmp/manager_bench.sqlite3"),
        "OPTIONS": {"timeout": 30},
    }
}
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "manager-benchmark-cache",
        "TIMEOUT": 300,
    }
}
