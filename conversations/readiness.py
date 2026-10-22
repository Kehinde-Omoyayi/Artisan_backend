from django.conf import settings
from rest_framework.exceptions import ValidationError


def require_production_readiness():
    try:
        from .models import ConversationAttachment, MediaRelocation
        from .private_media import conversation_storage, replica_buckets
        from .purge_ledger import verify_database_identity, verify_ledger_integrity

        flags = (
            "CONVERSATION_MEDIA_BACKUP_POLICY_CONFIRMED",
            "FLW_CHARGEBACK_WEBHOOKS_CONFIRMED",
            "CONVERSATION_MEDIA_REPLICA_INVENTORY_CONFIRMED",
        )
        if not all(getattr(settings, k, False) for k in flags):
            raise ValueError()
        verify_database_identity()
        verify_ledger_integrity()
        if (
            ConversationAttachment.objects.filter(private_storage=False).exists()
            or MediaRelocation.objects.exists()
        ):
            raise ValueError()
        if not getattr(settings, "USE_S3", False):
            raise ValueError()
        store = conversation_storage()
        client = store.connection.meta.client
        bucket = store.bucket_name
        for name in {bucket} | replica_buckets(client, bucket):
            flags = client.get_public_access_block(Bucket=name)[
                "PublicAccessBlockConfiguration"
            ]
            if not all(
                flags.get(k)
                for k in (
                    "BlockPublicAcls",
                    "IgnorePublicAcls",
                    "BlockPublicPolicy",
                    "RestrictPublicBuckets",
                )
            ):
                raise ValueError()
    except Exception:
        raise ValidationError(
            "Recording is not ready for production. Use text/photos while the operator completes the private-storage, legacy migration, provider-hold and restore preflight."
        ) from None
