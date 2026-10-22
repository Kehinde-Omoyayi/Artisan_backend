import logging
from django.conf import settings
from django.core.files.base import ContentFile
import requests

logger = logging.getLogger(__name__)


def scan_attachment(attachment):
    scanner_url = str(getattr(settings, "CLAMAV_SCAN_URL", "") or "").strip()
    if not scanner_url:
        if getattr(settings, "REQUIRE_MEDIA_SCAN", False):
            return False, "media scanner is not configured"
        return True, "signature_validation_only"
    try:
        from .private_media import attachment_storage

        attachment.file.storage = attachment_storage(attachment)
        attachment.file.open("rb")
        # MEDIA_SCAN_TIMEOUT_SECONDS is an int-validated, bounded setting (1–120s).
        response = requests.post(
            scanner_url,
            files={
                "file": (
                    attachment.file.name,
                    attachment.file.file,
                    attachment.content_type,
                )
            },
            timeout=settings.MEDIA_SCAN_TIMEOUT_SECONDS,  # nosec B113: finite, validated configuration value
        )
        response.raise_for_status()
        data = response.json() if response.content else {}
        clean = bool(data.get("clean", data.get("status") == "clean"))
        return (
            clean,
            "scanner_clean" if clean else str(data.get("reason") or "scanner_rejected"),
        )
    except Exception as exc:
        logger.exception("media_scan_failed attachment=%s", attachment.id)
        return None, str(exc)[:500]
    finally:
        try:
            attachment.file.close()
        except Exception:
            logger.debug(
                "media_scan_file_close_failed attachment=%s",
                attachment.id,
                exc_info=True,
            )
