"""Private conversation storage. Never return a storage URL to a browser."""

from pathlib import Path
from functools import lru_cache
from django.conf import settings
from django.core.files.storage import FileSystemStorage, default_storage
from django.core.exceptions import ImproperlyConfigured


class PrivateLocalStorage(FileSystemStorage):
    def url(self, name):
        raise ValueError("Conversation files require authenticated playback.")


@lru_cache(maxsize=1)
def conversation_storage():
    if getattr(settings, "USE_S3", False):
        from storages.backends.s3 import S3Storage

        return S3Storage(
            location="private-conversation-media",
            default_acl="private",
            querystring_auth=True,
            custom_domain=None,
            file_overwrite=False,
        )
    root = Path(
        getattr(
            settings,
            "CONVERSATION_PRIVATE_MEDIA_ROOT",
            Path(settings.BASE_DIR) / "private_conversation_media",
        )
    ).resolve()
    public = Path(settings.MEDIA_ROOT).resolve()
    if root == public or public in root.parents:
        raise ImproperlyConfigured(
            "Private conversation media must be outside MEDIA_ROOT."
        )
    return PrivateLocalStorage(
        location=root, file_permissions_mode=0o600, directory_permissions_mode=0o700
    )


def attachment_storage(attachment):
    return conversation_storage() if attachment.private_storage else default_storage


def replica_buckets(client, bucket):
    import re

    names = set(
        getattr(settings, "CONVERSATION_MEDIA_HISTORICAL_REPLICA_BUCKETS", []) or []
    )
    try:
        configuration = client.get_bucket_replication(Bucket=bucket)
    except Exception as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code")
        if code not in (
            "ReplicationConfigurationNotFoundError",
            "NoSuchReplicationConfiguration",
        ):
            raise
        configuration = {}
    for rule in configuration.get("ReplicationConfiguration", {}).get("Rules", []):
        if rule.get("Status") == "Enabled":
            arn = rule.get("Destination", {}).get("Bucket", "")
            if not arn.startswith("arn:aws:s3:::"):
                raise ImproperlyConfigured(
                    "Unsupported replication destination; deletion cannot be verified."
                )
            names.add(arn.removeprefix("arn:aws:s3:::"))
    for name in names:
        if not isinstance(name, str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", name
        ):
            raise ImproperlyConfigured("Invalid historical replica configuration.")
    return names - {bucket}


def reject_unverified_replication(client, bucket):
    # Kept as a compatibility preflight: discover and validate all destinations.
    return replica_buckets(client, bucket)


def _delete_s3_key(client, bucket, key):
    from botocore.exceptions import ClientError

    versioned = client.get_bucket_versioning(Bucket=bucket).get("Status") in (
        "Enabled",
        "Suspended",
    )

    def versions():
        for page in client.get_paginator("list_object_versions").paginate(
            Bucket=bucket, Prefix=key
        ):
            for row in page.get("Versions", []) + page.get("DeleteMarkers", []):
                if row["Key"] == key:
                    yield row

    if versioned:
        for row in list(versions()):
            client.delete_object(Bucket=bucket, Key=key, VersionId=row["VersionId"])
        if list(versions()):
            raise OSError("Version deletion not verified")
    else:
        client.delete_object(Bucket=bucket, Key=key)
    try:
        client.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if str(exc.response.get("Error", {}).get("Code")) in (
            "404",
            "NoSuchKey",
            "NotFound",
        ):
            return
        raise
    raise OSError("Object deletion not verified")


def permanently_delete(storage, key):
    """Delete all primary and discovered/historical replica versions, verifying absence.

    Any inaccessible destination, object lock, permission failure or unknown backend
    remains RETRY_REQUIRED. Never claim replica deletion based on the primary alone.
    """
    if not key:
        return
    if hasattr(storage, "bucket_name") and hasattr(storage, "connection"):
        client = storage.connection.meta.client
        bucket = storage.bucket_name
        replicas = replica_buckets(client, bucket)
        object_key = storage._normalize_name(key)
        # Remove the source first so it cannot enqueue new replication writes. Listing
        # replication PENDING/FAILED is a retry, not permission to finish prematurely.
        try:
            head = client.head_object(Bucket=bucket, Key=object_key)
        except Exception as exc:
            code = str(getattr(exc, "response", {}).get("Error", {}).get("Code"))
            if code not in ("404", "NoSuchKey", "NotFound"):
                raise
            head = {}
        if replicas and head.get("ReplicationStatus") in ("PENDING", "FAILED"):
            raise OSError(
                "Replication has not quiesced; retry before removing references"
            )
        if replicas and client.get_bucket_versioning(Bucket=bucket).get("Status") in (
            "Enabled",
            "Suspended",
        ):
            for page in client.get_paginator("list_object_versions").paginate(
                Bucket=bucket, Prefix=object_key
            ):
                for version in page.get("Versions", []):
                    if version["Key"] == object_key and client.head_object(
                        Bucket=bucket, Key=object_key, VersionId=version["VersionId"]
                    ).get("ReplicationStatus") in ("PENDING", "FAILED"):
                        raise OSError("Historical version replication has not quiesced")
        _delete_s3_key(client, bucket, object_key)
        for replica in replicas:
            _delete_s3_key(client, replica, object_key)
    elif isinstance(storage, FileSystemStorage):
        storage.delete(key)
        if storage.exists(key):
            raise OSError("Storage deletion not verified")
    else:
        raise ImproperlyConfigured("Unsupported media deletion backend")


def purge_recording_namespace(storage, conversation_id):
    """Catch upload-rollback orphans without touching the separate still-photo tree."""
    prefix = f"conversation_recordings/{conversation_id}/"
    if hasattr(storage, "bucket_name") and hasattr(storage, "connection"):
        client = storage.connection.meta.client
        bucket = storage.bucket_name
        reject_unverified_replication(client, bucket)
        full = storage._normalize_name(prefix).rstrip("/") + "/"
        names = set()
        for origin in {bucket} | replica_buckets(client, bucket):
            versioned = client.get_bucket_versioning(Bucket=origin).get("Status") in (
                "Enabled",
                "Suspended",
            )
            if versioned:
                for page in client.get_paginator("list_object_versions").paginate(
                    Bucket=origin, Prefix=full
                ):
                    names.update(
                        r["Key"]
                        for r in page.get("Versions", [])
                        + page.get("DeleteMarkers", [])
                        if r["Key"].startswith(full)
                    )
            else:
                for page in client.get_paginator("list_objects_v2").paginate(
                    Bucket=origin, Prefix=full
                ):
                    names.update(
                        r["Key"]
                        for r in page.get("Contents", [])
                        if r["Key"].startswith(full)
                    )
        base = storage.location.rstrip("/")
        for name in names:
            relative = name[len(base) + 1 :] if base else name
            permanently_delete(storage, relative)
    elif isinstance(storage, FileSystemStorage):

        def walk(path):
            try:
                directories, files = storage.listdir(path)
            except FileNotFoundError:
                return
            for name in files:
                permanently_delete(storage, path.rstrip("/") + "/" + name)
            for name in directories:
                walk(path.rstrip("/") + "/" + name)

        walk(prefix)
    else:
        raise ImproperlyConfigured("Unsupported recording namespace backend")
