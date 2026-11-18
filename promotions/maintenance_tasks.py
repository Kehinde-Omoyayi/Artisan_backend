"""Scheduled promotion-performance maintenance tasks."""
import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task
def purge_performance_dedupe_rows():
    """Delete expired visitor-deduplication digests while preserving aggregate counters."""
    from .placement_services import delete_expired_performance_dedupe_rows

    deleted = delete_expired_performance_dedupe_rows()
    logger.info("promotion_performance_dedupe_rows_purged count=%s", deleted)
    return {"deleted": deleted}
