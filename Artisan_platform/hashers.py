"""Password hashing tuned for this deployment's machine.

Django ships Argon2id at 100 MiB / 2 passes / 8 lanes. That is a good default for
a server, but it costs ~165 ms per hash on a fast machine and ~300-500 ms on an
ordinary Windows laptop -- and every sign-in, registration and password change
pays it twice (once in the API call, once more when the browser app checks the
session). That single cost was the bulk of the "login takes more than 700 ms"
report.

The defaults here follow OWASP's *minimum* recommended Argon2id configuration
(19 MiB, 2 passes, 1 lane: ~24 ms on the same machine, ~50-70 ms on a laptop).
Raise it on a production server with environment variables -- no code change:

    ARGON2_MEMORY_COST=65536   # KiB, so 65536 = 64 MiB
    ARGON2_TIME_COST=3
    ARGON2_PARALLELISM=2

Hashes written with the old settings keep working: Django checks the stored
parameters against the current ones and re-hashes the password at the user's
next successful sign-in, in place.
"""
import os

from django.contrib.auth.hashers import Argon2PasswordHasher


def _positive_int(name, default):
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


class TunableArgon2PasswordHasher(Argon2PasswordHasher):
    """Argon2id whose cost comes from the environment (see module docstring)."""

    #: KiB of memory per hash. 19 MiB is the OWASP floor for Argon2id.
    memory_cost = _positive_int("ARGON2_MEMORY_COST", 19456)
    #: passes over that memory
    time_cost = _positive_int("ARGON2_TIME_COST", 2)
    #: lanes (threads) Argon2 may use for one hash
    parallelism = _positive_int("ARGON2_PARALLELISM", 1)
