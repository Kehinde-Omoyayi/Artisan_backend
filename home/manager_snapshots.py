"""Persistent, idempotent scheduling for platform-manager analytics snapshots."""
from calendar import monthrange
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import ManagerAnalyticsSnapshot, ManagerSnapshotSchedule

SNAPSHOT_DISPATCH_RETRY_AFTER = timedelta(minutes=5)
SNAPSHOT_STALE_AFTER = timedelta(minutes=10)


def _zone(timezone_name):
    try:
        return ZoneInfo(str(timezone_name or settings.TIME_ZONE))
    except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("The configured manager snapshot timezone is invalid.") from exc


def _calendar_month_after(reference, anchor_day, tz):
    reference = reference.astimezone(tz)
    month_index = reference.year * 12 + reference.month
    year, month = divmod(month_index, 12)
    year, month = year, month + 1
    if month > 12:
        year, month = year + 1, 1
    day = min(max(int(anchor_day), 1), monthrange(year, month)[1])
    return datetime(
        year,
        month,
        day,
        reference.hour,
        reference.minute,
        reference.second,
        reference.microsecond,
        tzinfo=tz,
        fold=reference.fold,
    )


def next_snapshot_at(frequency, reference, timezone_name, *, monthly_anchor_day=None):
    """Calculate one next intended run, preserving the selected timezone's wall clock."""
    tz = _zone(timezone_name)
    if timezone.is_naive(reference):
        reference = timezone.make_aware(reference, tz)
    local_reference = reference.astimezone(tz)
    if frequency == ManagerSnapshotSchedule.Frequency.HOURLY:
        # Hourly is elapsed-time based so the DST spring/fall transition cannot
        # produce a nonexistent or duplicate local wall-clock hour.
        return (reference.astimezone(UTC) + timedelta(hours=1)).astimezone(tz)
    if frequency == ManagerSnapshotSchedule.Frequency.DAILY:
        return local_reference + timedelta(days=1)
    if frequency == ManagerSnapshotSchedule.Frequency.EVERY_THREE_DAYS:
        return local_reference + timedelta(days=3)
    if frequency == ManagerSnapshotSchedule.Frequency.WEEKLY:
        return local_reference + timedelta(days=7)
    if frequency == ManagerSnapshotSchedule.Frequency.MONTHLY:
        anchor_day = monthly_anchor_day or local_reference.day
        return _calendar_month_after(local_reference, anchor_day, tz)
    raise ValueError("Choose hourly, daily, every three days, weekly, or monthly.")


def snapshot_period_key(frequency, scheduled_for, timezone_name):
    """Stable unique key for one scheduled period; repeated local DST hours retain offsets."""
    local_run = scheduled_for.astimezone(_zone(timezone_name))
    return f"{frequency}:{local_run.isoformat(timespec='microseconds')}"


def get_or_create_snapshot_schedule(*, now=None):
    now = now or timezone.now()
    timezone_name = settings.TIME_ZONE
    local_now = now.astimezone(_zone(timezone_name))
    schedule, _created = ManagerSnapshotSchedule.objects.get_or_create(
        key="manager-overview",
        defaults={
            "frequency": ManagerSnapshotSchedule.Frequency.DAILY,
            "timezone_name": timezone_name,
            "next_run_at": next_snapshot_at(
                ManagerSnapshotSchedule.Frequency.DAILY,
                now,
                timezone_name,
                monthly_anchor_day=local_now.day,
            ),
            "monthly_anchor_day": local_now.day,
        },
    )
    return schedule


def configure_snapshot_schedule(*, actor, frequency, now=None):
    """Change frequency atomically; pending runs from the previous schedule are cancelled."""
    valid_frequencies = {value for value, _label in ManagerSnapshotSchedule.Frequency.choices}
    if frequency not in valid_frequencies:
        raise ValueError("Choose a supported manager snapshot frequency.")
    now = now or timezone.now()
    get_or_create_snapshot_schedule(now=now)
    with transaction.atomic():
        schedule = ManagerSnapshotSchedule.objects.select_for_update().get(key="manager-overview")
        before = {
            "frequency": schedule.frequency,
            "timezone": schedule.timezone_name,
            "next_run_at": schedule.next_run_at,
            "schedule_version": schedule.schedule_version,
        }
        changed = frequency != schedule.frequency
        if changed:
            ManagerAnalyticsSnapshot.objects.filter(
                schedule=schedule,
                schedule_version=schedule.schedule_version,
                status=ManagerAnalyticsSnapshot.Status.PENDING,
            ).update(
                status=ManagerAnalyticsSnapshot.Status.CANCELLED,
                last_error="Cancelled because the manager changed snapshot frequency.",
                completed_at=now,
            )
            tz = _zone(schedule.timezone_name)
            local_now = now.astimezone(tz)
            schedule.frequency = frequency
            schedule.monthly_anchor_day = local_now.day
            schedule.next_run_at = next_snapshot_at(
                frequency,
                now,
                schedule.timezone_name,
                monthly_anchor_day=schedule.monthly_anchor_day,
            )
            schedule.schedule_version += 1
            schedule.updated_by = actor
            schedule.updated_by_name_snapshot = (
                actor.get_full_name() or getattr(actor, "email", "")
            )[:160]
            schedule.save(
                update_fields=[
                    "frequency",
                    "monthly_anchor_day",
                    "next_run_at",
                    "schedule_version",
                    "updated_by",
                    "updated_by_name_snapshot",
                    "updated_at",
                ]
            )
        after = {
            "frequency": schedule.frequency,
            "timezone": schedule.timezone_name,
            "next_run_at": schedule.next_run_at,
            "schedule_version": schedule.schedule_version,
        }
    return schedule, before, after, changed


def acquire_due_snapshot(*, now=None):
    """Create or reclaim one due run and mark its dispatch attempt under a DB lock."""
    now = now or timezone.now()
    get_or_create_snapshot_schedule(now=now)
    with transaction.atomic():
        schedule = ManagerSnapshotSchedule.objects.select_for_update().get(key="manager-overview")
        if schedule.next_run_at > now:
            return None, "not_due"

        period_key = snapshot_period_key(
            schedule.frequency,
            schedule.next_run_at,
            schedule.timezone_name,
        )
        run, _created = ManagerAnalyticsSnapshot.objects.get_or_create(
            period_key=period_key,
            defaults={
                "schedule": schedule,
                "schedule_version": schedule.schedule_version,
                "frequency": schedule.frequency,
                "timezone_name": schedule.timezone_name,
                "scheduled_for": schedule.next_run_at,
                "status": ManagerAnalyticsSnapshot.Status.PENDING,
            },
        )
        run = ManagerAnalyticsSnapshot.objects.select_for_update().get(pk=run.pk)
        if run.status in {
            ManagerAnalyticsSnapshot.Status.SUCCEEDED,
            ManagerAnalyticsSnapshot.Status.FAILED,
            ManagerAnalyticsSnapshot.Status.CANCELLED,
        }:
            return None, run.status.lower()
        if run.status == ManagerAnalyticsSnapshot.Status.RUNNING:
            if run.started_at and now - run.started_at < SNAPSHOT_STALE_AFTER:
                return None, "running"
            _finish_failed_run_locked(
                schedule,
                run,
                now=now,
                reason="Snapshot worker exceeded the ten-minute execution window.",
            )
            return None, "stale_worker_failed"
        if run.last_dispatched_at and now - run.last_dispatched_at < SNAPSHOT_DISPATCH_RETRY_AFTER:
            return None, "dispatch_pending"
        run.last_dispatched_at = now
        run.save(update_fields=["last_dispatched_at"])
        return str(run.pk), "queued"


def record_snapshot_dispatch_failure(run_id, exc, *, now=None):
    now = now or timezone.now()
    message = f"Celery dispatch failed ({type(exc).__name__}): {str(exc)[:600]}"[:700]
    with transaction.atomic():
        run_stub = ManagerAnalyticsSnapshot.objects.only("schedule_id").get(pk=run_id)
        schedule = ManagerSnapshotSchedule.objects.select_for_update().get(pk=run_stub.schedule_id)
        run = ManagerAnalyticsSnapshot.objects.select_for_update().get(pk=run_id)
        if run.status == ManagerAnalyticsSnapshot.Status.PENDING:
            run.last_error = message
            run.save(update_fields=["last_error"])
            schedule.last_error_at = now
            schedule.last_error = message
            schedule.save(update_fields=["last_error_at", "last_error", "updated_at"])


def claim_snapshot_run(run_id, *, now=None):
    now = now or timezone.now()
    run_stub = ManagerAnalyticsSnapshot.objects.only("schedule_id").get(pk=run_id)
    with transaction.atomic():
        schedule = ManagerSnapshotSchedule.objects.select_for_update().get(pk=run_stub.schedule_id)
        run = ManagerAnalyticsSnapshot.objects.select_for_update().get(pk=run_id)
        if run.status in {
            ManagerAnalyticsSnapshot.Status.SUCCEEDED,
            ManagerAnalyticsSnapshot.Status.FAILED,
            ManagerAnalyticsSnapshot.Status.CANCELLED,
        }:
            return None
        if run.status == ManagerAnalyticsSnapshot.Status.RUNNING:
            if run.started_at and now - run.started_at < SNAPSHOT_STALE_AFTER:
                return None
            _finish_failed_run_locked(
                schedule,
                run,
                now=now,
                reason="Snapshot worker exceeded the ten-minute execution window.",
            )
            return None
        run.status = ManagerAnalyticsSnapshot.Status.RUNNING
        run.started_at = now
        run.attempts += 1
        run.last_error = ""
        run.save(update_fields=["status", "started_at", "attempts", "last_error"])
        return run


def _advance_schedule_after_run(schedule, run, *, finished_at, succeeded):
    schedule.last_run_at = finished_at
    schedule.last_period_key = run.period_key
    fields = ["last_run_at", "last_period_key", "updated_at"]
    if succeeded:
        schedule.last_success_at = finished_at
        schedule.last_error = ""
        fields.extend(["last_success_at", "last_error"])
    else:
        schedule.last_error_at = finished_at
        schedule.last_error = run.last_error
        fields.extend(["last_error_at", "last_error"])
    if (
        schedule.schedule_version == run.schedule_version
        and schedule.next_run_at == run.scheduled_for
    ):
        schedule.next_run_at = next_snapshot_at(
            run.frequency,
            run.scheduled_for,
            run.timezone_name,
            monthly_anchor_day=schedule.monthly_anchor_day,
        )
        fields.append("next_run_at")
    schedule.save(update_fields=fields)


def _finish_failed_run_locked(schedule, run, *, now, reason):
    run.status = ManagerAnalyticsSnapshot.Status.FAILED
    run.last_error = str(reason)[:2000]
    run.completed_at = now
    run.save(update_fields=["status", "last_error", "completed_at"])
    _advance_schedule_after_run(schedule, run, finished_at=now, succeeded=False)


def complete_snapshot_run(run_id, payload, *, now=None):
    now = now or timezone.now()
    run_stub = ManagerAnalyticsSnapshot.objects.only("schedule_id").get(pk=run_id)
    with transaction.atomic():
        schedule = ManagerSnapshotSchedule.objects.select_for_update().get(pk=run_stub.schedule_id)
        run = ManagerAnalyticsSnapshot.objects.select_for_update().get(pk=run_id)
        if run.status == ManagerAnalyticsSnapshot.Status.SUCCEEDED:
            return run
        if run.status != ManagerAnalyticsSnapshot.Status.RUNNING:
            return run
        run.status = ManagerAnalyticsSnapshot.Status.SUCCEEDED
        run.payload = payload
        run.completed_at = now
        run.last_error = ""
        run.save(update_fields=["status", "payload", "completed_at", "last_error"])
        _advance_schedule_after_run(schedule, run, finished_at=now, succeeded=True)
        return run


def fail_snapshot_run(run_id, exc, *, now=None):
    now = now or timezone.now()
    message = f"{type(exc).__name__}: {str(exc)[:1800]}"[:2000]
    run_stub = ManagerAnalyticsSnapshot.objects.only("schedule_id").get(pk=run_id)
    with transaction.atomic():
        schedule = ManagerSnapshotSchedule.objects.select_for_update().get(pk=run_stub.schedule_id)
        run = ManagerAnalyticsSnapshot.objects.select_for_update().get(pk=run_id)
        if run.status != ManagerAnalyticsSnapshot.Status.RUNNING:
            return run
        _finish_failed_run_locked(schedule, run, now=now, reason=message)
        return run


def latest_successful_snapshot():
    return ManagerAnalyticsSnapshot.objects.filter(
        status=ManagerAnalyticsSnapshot.Status.SUCCEEDED
    ).order_by("-completed_at", "-scheduled_for").first()
