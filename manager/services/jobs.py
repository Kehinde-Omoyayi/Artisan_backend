"""Durable background job registry.

Creation is a single insert; the heavy work happens in Celery on a dedicated
queue (``manager``) so a 50 000-row export can never starve payment traffic.
Every job records attempts, progress, result and error, and de-duplicates on an
optional key so a double click cannot queue the same export twice.
"""
from __future__ import annotations

import uuid

from django.db import transaction
from django.utils import timezone

from core.services import create_audit_log
from manager.models import BackgroundJob

QUEUE_BY_KIND = {
    BackgroundJob.Kind.SNAPSHOT: "manager",
    BackgroundJob.Kind.AGGREGATION: "manager",
    BackgroundJob.Kind.DATA_QUALITY: "manager",
    BackgroundJob.Kind.RECONCILIATION: "manager",
    BackgroundJob.Kind.REPORT: "manager",
    BackgroundJob.Kind.EXPORT: "exports",
    BackgroundJob.Kind.CAMPAIGN_DISPATCH: "campaigns",
}


class JobError(ValueError):
    pass


def dedupe_redispatch_after():
    """How long a deduplicated job may sit unpublished before it is re-sent."""
    from datetime import timedelta

    return timedelta(seconds=10)


# These jobs act on behalf of a named manager: the artefact they produce belongs to
# that person and the audit trail must say who asked for it.
REQUESTER_REQUIRED = frozenset({"EXPORT", "REPORT"})


def enqueue(*, kind, payload, user=None, dedupe_key="", correlation_id="", scheduled_for=None, queue=None):
    if kind not in BackgroundJob.Kind.values:
        raise JobError(f"Unknown job kind '{kind}'.")
    if kind in REQUESTER_REQUIRED and user is None:
        raise JobError(f"A {kind.lower()} job must be requested by a signed-in manager.")
    if dedupe_key:
        existing = BackgroundJob.objects.filter(
            kind=kind,
            dedupe_key=dedupe_key,
            status__in=[BackgroundJob.Status.QUEUED, BackgroundJob.Status.RUNNING],
        ).first()
        if existing:
            # A queued job whose broker message was lost (outage, worker restart)
            # would otherwise sit here forever while every new request dedupes onto
            # it. Re-publishing is safe: the worker claims a job atomically and a
            # second delivery exits as "skipped".
            if (
                existing.status == BackgroundJob.Status.QUEUED
                and existing.created_at < timezone.now() - dedupe_redispatch_after()
            ):
                _dispatch(existing)
            return existing, False
    with transaction.atomic():
        job = BackgroundJob.objects.create(
            kind=kind,
            status=BackgroundJob.Status.QUEUED,
            queue=queue or QUEUE_BY_KIND.get(kind, "manager"),
            dedupe_key=dedupe_key[:80],
            payload=payload or {},
            requested_by=user,
            correlation_id=correlation_id or uuid.uuid4().hex,
            scheduled_for=scheduled_for,
        )
        create_audit_log(
            action="MANAGER_JOB_QUEUED",
            resource_type="BACKGROUND_JOB",
            resource_id=str(job.id),
            actor=user,
            metadata={"kind": kind, "queue": job.queue},
        )
    _dispatch(job)
    return job, True


def _dispatch(job):
    """Hand the job to Celery. If the broker is down the row stays QUEUED and the
    watchdog task picks it up, so nothing silently disappears."""
    from manager import tasks

    mapping = {
        BackgroundJob.Kind.SNAPSHOT: tasks.run_snapshot_job,
        BackgroundJob.Kind.EXPORT: tasks.run_export_job,
        BackgroundJob.Kind.REPORT: tasks.run_report_job,
        BackgroundJob.Kind.CAMPAIGN_DISPATCH: tasks.run_campaign_dispatch,
        BackgroundJob.Kind.RECONCILIATION: tasks.run_reconciliation_job,
        BackgroundJob.Kind.AGGREGATION: tasks.run_aggregation_job,
        BackgroundJob.Kind.DATA_QUALITY: tasks.run_data_quality_job,
    }
    task = mapping.get(job.kind)
    if not task:
        return
    try:
        task.apply_async(args=[str(job.id)], queue=job.queue, retry=False)
    except Exception:  # pragma: no cover - broker outage is handled by the watchdog
        return


def mark_running(job):
    BackgroundJob.objects.filter(pk=job.pk, status=BackgroundJob.Status.QUEUED).update(
        status=BackgroundJob.Status.RUNNING, started_at=timezone.now(), attempts=job.attempts + 1
    )
    job.refresh_from_db()


def mark_progress(job_id, progress, total=None):
    fields = {"progress": progress, "updated_at": timezone.now()}
    if total is not None:
        fields["total"] = total
    BackgroundJob.objects.filter(pk=job_id).update(**fields)


def mark_succeeded(job_id, result=None):
    BackgroundJob.objects.filter(pk=job_id).update(
        status=BackgroundJob.Status.SUCCEEDED,
        progress=BackgroundJob.objects.filter(pk=job_id).values_list("total", flat=True).first() or 0,
        result=result or {},
        finished_at=timezone.now(),
        error="",
    )


def mark_failed(job_id, error):
    BackgroundJob.objects.filter(pk=job_id).update(
        status=BackgroundJob.Status.FAILED,
        error=str(error)[:2000],
        finished_at=timezone.now(),
    )


def cancel(job, *, user=None):
    if job.status not in {BackgroundJob.Status.QUEUED, BackgroundJob.Status.RUNNING}:
        raise JobError("Only queued or running jobs can be cancelled.")
    job.status = BackgroundJob.Status.CANCELLED
    job.finished_at = timezone.now()
    job.save(update_fields=["status", "finished_at", "updated_at"])
    create_audit_log(action="MANAGER_JOB_CANCELLED", resource_type="BACKGROUND_JOB",
                     resource_id=str(job.id), actor=user, metadata={"kind": job.kind})
    return job


def status_payload(job):
    duration = None
    if job.started_at and job.finished_at:
        duration = round((job.finished_at - job.started_at).total_seconds(), 3)
    return {
        "id": str(job.id),
        "kind": job.kind,
        "status": job.status,
        "queue": job.queue,
        "progress": job.progress,
        "total": job.total,
        # Bounded, because progress is a counter and total is an estimate; a batch
        # that overshoots must not render as "1200%".
        "percent": min(100.0, round(job.progress * 100 / job.total, 1)) if job.total else None,
        "result": job.result,
        "error": job.error,
        "attempts": job.attempts,
        "correlation_id": job.correlation_id,
        "created_at": job.created_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "duration_seconds": duration,
    }


def queue_health():
    now = timezone.now()
    queued = BackgroundJob.objects.filter(status=BackgroundJob.Status.QUEUED)
    running = BackgroundJob.objects.filter(status=BackgroundJob.Status.RUNNING)
    oldest = queued.order_by("created_at").values_list("created_at", flat=True).first()
    recent = BackgroundJob.objects.filter(finished_at__gte=now - __import__("datetime").timedelta(hours=24),
                                          status=BackgroundJob.Status.SUCCEEDED)
    durations = [
        (job.finished_at - job.started_at).total_seconds()
        for job in recent.only("started_at", "finished_at")[:500]
        if job.started_at and job.finished_at
    ]
    durations.sort()
    p95 = durations[min(len(durations) - 1, int(len(durations) * 0.95))] if durations else None
    return {
        "queued": queued.count(),
        "running": running.count(),
        "failed_24h": BackgroundJob.objects.filter(status=BackgroundJob.Status.FAILED,
                                                   finished_at__gte=now - __import__("datetime").timedelta(hours=24)).count(),
        "succeeded_24h": recent.count(),
        "oldest_queued_at": oldest.isoformat() if oldest else None,
        "oldest_queued_seconds": round((now - oldest).total_seconds()) if oldest else None,
        "average_duration_seconds": round(sum(durations) / len(durations), 3) if durations else None,
        "p95_duration_seconds": round(p95, 3) if p95 is not None else None,
        "sample": len(durations),
    }
