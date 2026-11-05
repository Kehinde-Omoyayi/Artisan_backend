"""Celery tasks for the manager console.

Two rules:

* every task takes a job id (or a small payload) and re-reads its state, so a
  broker redelivery cannot act twice;
* heavy work is chunked and yields between batches, so a campaign or export can
  never hold a worker (or a web request) hostage.
"""
from __future__ import annotations

import logging

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task(autoretry_for=(Exception,), retry_backoff=True, retry_backoff_max=300,
              retry_jitter=True, max_retries=5)
def send_manager_step_up_code(challenge_id, encrypted_code):
    """Email a manager step-up code.

    Delivery is keyed to the stored challenge, so a code that expired, was already
    used or was invalidated between issuing and sending is never mailed. The code
    travels encrypted and is never written to a log.
    """
    from django.conf import settings

    from home.email_templates import send_security_code_email
    from home.security_mail import decrypt_email_code
    from manager.models import ManagerOTPChallenge

    challenge = ManagerOTPChallenge.objects.select_related("user").filter(
        pk=challenge_id,
        consumed_at__isnull=True,
        expires_at__gt=timezone.now(),
    ).first()
    if challenge is None:
        return {"sent": False, "reason": "challenge no longer valid"}

    try:
        code = decrypt_email_code(encrypted_code)
    except Exception:
        # A payload that cannot be decrypted will never decrypt: retrying is pointless.
        logger.warning("manager_step_up_code_undecryptable challenge=%s", challenge_id)
        return {"sent": False, "reason": "undecryptable payload"}
    purpose_label = {
        "STEP_UP": "sensitive action verification",
        "LOGIN": "staff sign-in",
    }.get(challenge.purpose, "staff account verification")
    send_security_code_email(
        recipient_email=challenge.user.email,
        recipient_name=challenge.user.first_name,
        subject="Your Manager Console verification code",
        heading="Confirm a sensitive action",
        intro=f"A manager started {purpose_label} on the Artisan Market Manager Console.",
        code=code,
        code_label="MANAGER CONSOLE CODE",
        purpose_label="Manager Console security",
        security_note="If you did not start this action, do not share this code and tell a super administrator.",
        action_url="",
        action_label="",
    )
    return {"sent": True, "purpose": challenge.purpose}


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def run_snapshot_job(self, job_id):
    from manager.models import BackgroundJob
    from manager.services import jobs, snapshots

    job = BackgroundJob.objects.filter(pk=job_id).first()
    if not job:
        return {"status": "missing"}
    jobs.mark_running(job)
    try:
        result = snapshots.capture()
        jobs.mark_succeeded(job_id, result)
        return result
    except Exception as exc:  # pragma: no cover - reported on the job row
        jobs.mark_failed(job_id, exc)
        raise


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def run_export_job(self, job_id):
    from manager.models import BackgroundJob
    from manager.services import exports, jobs

    job = BackgroundJob.objects.filter(pk=job_id).first()
    if not job or job.status == BackgroundJob.Status.CANCELLED:
        return {"status": "skipped"}
    jobs.mark_running(job)
    try:
        artifact = exports.build_export(job)
        if artifact is None:
            # build_export already recorded why on the job row.
            return {"status": "failed"}
        return {"status": "ok", "filename": artifact.filename, "rows": artifact.row_count}
    except Exception as exc:  # pragma: no cover
        jobs.mark_failed(job_id, exc)
        raise


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def run_report_job(self, job_id):
    from manager.models import BackgroundJob, SavedReport
    from manager.services import data_explorer, jobs

    job = BackgroundJob.objects.filter(pk=job_id).first()
    if not job:
        return {"status": "missing"}
    jobs.mark_running(job)
    try:
        report_id = (job.payload or {}).get("report_id")
        report = SavedReport.objects.filter(pk=report_id).first()
        if not report:
            raise ValueError("Report no longer exists.")
        result = data_explorer.run(report.spec)
        jobs.mark_succeeded(job_id, {"rows": result["row_count"], "total": result["total"]})
        return {"status": "ok"}
    except Exception as exc:  # pragma: no cover
        jobs.mark_failed(job_id, exc)
        raise


@shared_task(bind=True, max_retries=5, default_retry_delay=60)
def run_campaign_dispatch(self, job_id):
    from manager.models import BackgroundJob, Campaign
    from manager.services import campaigns, jobs

    job = BackgroundJob.objects.filter(pk=job_id).first()
    if not job:
        return {"status": "missing"}
    if job.status == BackgroundJob.Status.CANCELLED:
        return {"status": "cancelled"}
    jobs.mark_running(job)
    campaign_id = (job.payload or {}).get("campaign_id")
    campaign = Campaign.objects.filter(pk=campaign_id).first()
    if not campaign:
        jobs.mark_failed(job_id, "Campaign no longer exists.")
        return {"status": "missing_campaign"}
    try:
        built = campaigns.build_recipients(campaign)
        total = built["created"]
        sent_total = 0
        while True:
            batch = campaigns.dispatch(campaign)
            sent_total += batch["sent"]
            jobs.mark_progress(job_id, min(total, sent_total), total=total)
            if batch["remaining"] == 0:
                break
            if BackgroundJob.objects.filter(pk=job_id, status=BackgroundJob.Status.CANCELLED).exists():
                return {"status": "cancelled", "sent": sent_total}
        Campaign.objects.filter(pk=campaign.pk).update(dispatch_summary={
            "sent": sent_total,
            "skipped": built["skipped"],
            "at": timezone.now().isoformat(),
        })
        jobs.mark_succeeded(job_id, {"sent": sent_total, "skipped": built["skipped"]})
        return {"status": "ok", "sent": sent_total}
    except Exception as exc:  # pragma: no cover
        jobs.mark_failed(job_id, exc)
        raise


@shared_task
def run_reconciliation_job(job_id):
    from manager.models import BackgroundJob
    from manager.services import jobs
    from manager.services.metrics import reconciliation_summary, resolve_range

    job = BackgroundJob.objects.filter(pk=job_id).first()
    if not job:
        return {"status": "missing"}
    jobs.mark_running(job)
    try:
        rng = resolve_range((job.payload or {}).get("filters") or {})
        result = reconciliation_summary(rng)
        jobs.mark_succeeded(job_id, result)
        return {"status": "ok", "clean": result["clean"]}
    except Exception as exc:  # pragma: no cover
        jobs.mark_failed(job_id, exc)
        raise


@shared_task
def run_aggregation_job(job_id):
    from manager.models import BackgroundJob
    from manager.services import jobs
    from manager.services.aggregates import rebuild_daily

    job = BackgroundJob.objects.filter(pk=job_id).first()
    if not job:
        return {"status": "missing"}
    jobs.mark_running(job)
    try:
        result = rebuild_daily(days=int((job.payload or {}).get("days") or 30))
        jobs.mark_succeeded(job_id, result)
        return {"status": "ok", **result}
    except Exception as exc:  # pragma: no cover
        jobs.mark_failed(job_id, exc)
        raise


@shared_task
def run_data_quality_job(job_id):
    from manager.models import BackgroundJob
    from manager.services import jobs
    from manager.services.metrics import data_quality

    job = BackgroundJob.objects.filter(pk=job_id).first()
    if not job:
        return {"status": "missing"}
    jobs.mark_running(job)
    try:
        result = data_quality(None, {})
        jobs.mark_succeeded(job_id, {"checks": result["checks"]})
        return {"status": "ok"}
    except Exception as exc:  # pragma: no cover
        jobs.mark_failed(job_id, exc)
        raise


@shared_task
def capture_operational_snapshot():
    """Beat entry point: capture if due. Cheap and idempotent."""
    from manager.services import snapshots

    return snapshots.capture_if_due()


@shared_task
def dispatch_due_campaigns():
    from manager.models import Campaign
    from manager.services import campaigns, jobs

    now = timezone.now()
    sent = 0
    for campaign in Campaign.objects.filter(status=Campaign.Status.SCHEDULED,
                                            scheduled_for__lte=now).order_by("scheduled_for")[:25]:
        jobs.enqueue(kind="CAMPAIGN_DISPATCH", payload={"campaign_id": str(campaign.id)},
                     user=campaign.approved_by, dedupe_key=f"campaign:{campaign.id}", queue="campaigns")
        Campaign.objects.filter(pk=campaign.pk).update(status=Campaign.Status.DISPATCHING)
        sent += 1
    return {"queued": sent}


@shared_task
def requeue_stalled_jobs():
    """Recover jobs whose broker message was lost (worker restart, broker outage)."""
    from manager.models import BackgroundJob
    from manager.services import jobs

    threshold = timezone.now() - timezone.timedelta(minutes=15)
    requeued = 0
    for job in BackgroundJob.objects.filter(status=BackgroundJob.Status.QUEUED, created_at__lt=threshold)[:50]:
        jobs._dispatch(job)
        requeued += 1
    return {"requeued": requeued}


@shared_task
def purge_manager_retention():
    from manager.services import exports, governance

    result = governance.purge_expired_retention()
    result.update(exports.purge_expired())
    return result
