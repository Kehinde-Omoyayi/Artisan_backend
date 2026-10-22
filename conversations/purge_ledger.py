"""External, minimal deletion tombstones, kept outside database/media backups.

Operators must place this on durable private storage and preserve it across restore.
No object key, filename, URL, media content or participant information is stored.
"""

import json, os
from pathlib import Path
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone


def ledger_path():
    value = str(getattr(settings, "CONVERSATION_PURGE_LEDGER_PATH", "") or "")
    if not value:
        if getattr(settings, "ENVIRONMENT", "") == "production":
            raise ImproperlyConfigured(
                "A durable external conversation purge ledger is required in production."
            )
        return None
    return Path(value)


def verify_database_identity():
    if getattr(settings, "ENVIRONMENT", "") != "production":
        return
    from .models import MediaPolicyIdentity

    path = ledger_path()
    receipt = Path(str(path) + ".identity")
    if not receipt.exists() or receipt.read_text().strip() != str(
        MediaPolicyIdentity.objects.get(pk=1).identity
    ):
        raise ImproperlyConfigured(
            "Media restore identity is absent or mismatched. Playback and purge remain blocked until a verified restore reconciliation."
        )


def remember_access_block(request_id):
    verify_database_identity()
    path = ledger_path()
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        verify_ledger_integrity()
    # Host deployment is Linux (Django/Celery); flock serializes concurrent writers.
    import fcntl

    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a") as out:
        fcntl.flock(out, fcntl.LOCK_EX)
        from services.models import ServiceRequest

        identity = str(
            ServiceRequest.objects.only("media_policy_id")
            .get(pk=request_id)
            .media_policy_id
        )
        out.write(
            json.dumps(
                {
                    "request_id": int(request_id),
                    "request_identity": identity,
                    "blocked_at": timezone.now().isoformat(),
                    "policy_version": "1.0",
                },
                separators=(",", ":"),
            )
            + "\n"
        )
        out.flush()
        os.fsync(out.fileno())


from functools import lru_cache


@lru_cache(maxsize=4)
def _index(path, mtime, size):
    import fcntl

    rows = set()
    with Path(path).open() as source:
        fcntl.flock(source, fcntl.LOCK_SH)
        for line in source:
            if not line.strip():
                continue
            row = json.loads(line)
            rid = int(row["request_id"])
            identity = row.get("request_identity")
            if identity:
                import uuid

                identity = str(uuid.UUID(identity))
            rows.add((rid, identity))
    return frozenset(rows)


def ledger_blocks(request_id):
    verify_database_identity()
    path = ledger_path()
    if path is None:
        return False
    if not path.is_file():
        raise ImproperlyConfigured(
            "External recording purge ledger is missing; playback is blocked."
        )
    stat = path.stat()
    if stat.st_mode & 0o077:
        raise ImproperlyConfigured("The external deletion ledger must be private.")
    from services.models import ServiceRequest

    identity = str(
        ServiceRequest.objects.only("media_policy_id")
        .get(pk=request_id)
        .media_policy_id
    )
    rows = _index(str(path), stat.st_mtime_ns, stat.st_size)
    return (int(request_id), identity) in rows or (int(request_id), None) in rows


def verify_ledger_integrity():
    path = ledger_path()
    if path is None:
        return
    stat = path.stat()
    if stat.st_mode & 0o077:
        raise ImproperlyConfigured("The external deletion ledger must be private.")
    _index(str(path), stat.st_mtime_ns, stat.st_size)
