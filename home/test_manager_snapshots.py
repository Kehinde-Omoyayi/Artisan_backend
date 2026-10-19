from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from Artisan_platform.celery import app as celery_app
from core.models import AuditLog
from home.manager_snapshots import (
    acquire_due_snapshot,
    claim_snapshot_run,
    complete_snapshot_run,
    configure_snapshot_schedule,
    fail_snapshot_run,
    get_or_create_snapshot_schedule,
    next_snapshot_at,
)
from home.models import CustomUser, ManagerAnalyticsSnapshot, ManagerSnapshotSchedule


SNAPSHOT_TEST_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "manager-snapshot-tests",
    }
}


@override_settings(CACHES=SNAPSHOT_TEST_CACHES)
class ManagerSnapshotScheduleTests(TestCase):
    def setUp(self):
        self.manager = CustomUser.objects.create_superuser(
            email="snapshot-manager@example.test",
            first_name="Ayo",
            last_name="Manager",
            password="SafePass!483",
        )

    def test_configured_timezone_frequency_and_monthly_anchor_are_persisted(self):
        now = datetime(2026, 1, 31, 9, 30, tzinfo=ZoneInfo("Africa/Lagos"))
        with override_settings(TIME_ZONE="Africa/Lagos"):
            schedule = get_or_create_snapshot_schedule(now=now)
            self.assertEqual(schedule.timezone_name, "Africa/Lagos")
            self.assertEqual(schedule.frequency, ManagerSnapshotSchedule.Frequency.DAILY)

            schedule, before, after, changed = configure_snapshot_schedule(
                actor=self.manager,
                frequency=ManagerSnapshotSchedule.Frequency.MONTHLY,
                now=now,
            )
            self.assertTrue(changed)
            self.assertEqual(before["frequency"], ManagerSnapshotSchedule.Frequency.DAILY)
            self.assertEqual(after["frequency"], ManagerSnapshotSchedule.Frequency.MONTHLY)
            self.assertEqual(schedule.monthly_anchor_day, 31)
            next_local = schedule.next_run_at.astimezone(ZoneInfo("Africa/Lagos"))
            self.assertEqual((next_local.year, next_local.month, next_local.day), (2026, 2, 28))
            self.assertEqual((next_local.hour, next_local.minute), (9, 30))

    def test_monthly_frequency_returns_to_anchor_day_after_short_month(self):
        tz = ZoneInfo("Africa/Lagos")
        january = datetime(2026, 1, 31, 9, 30, tzinfo=tz)
        february = next_snapshot_at(
            ManagerSnapshotSchedule.Frequency.MONTHLY,
            january,
            "Africa/Lagos",
            monthly_anchor_day=31,
        )
        march = next_snapshot_at(
            ManagerSnapshotSchedule.Frequency.MONTHLY,
            february,
            "Africa/Lagos",
            monthly_anchor_day=31,
        )
        self.assertEqual((february.month, february.day), (2, 28))
        self.assertEqual((march.month, march.day), (3, 31))

    def test_due_period_is_claimed_once_and_success_advances_next_run(self):
        now = timezone.now()
        schedule = ManagerSnapshotSchedule.objects.create(
            key="manager-overview",
            frequency=ManagerSnapshotSchedule.Frequency.HOURLY,
            timezone_name="UTC",
            next_run_at=now - timedelta(seconds=30),
        )

        run_id, state = acquire_due_snapshot(now=now)
        self.assertEqual(state, "queued")
        self.assertTrue(run_id)
        second_run_id, second_state = acquire_due_snapshot(now=now)
        self.assertIsNone(second_run_id)
        self.assertEqual(second_state, "dispatch_pending")
        self.assertEqual(ManagerAnalyticsSnapshot.objects.count(), 1)

        run = claim_snapshot_run(run_id, now=now + timedelta(seconds=1))
        self.assertEqual(run.status, ManagerAnalyticsSnapshot.Status.RUNNING)
        self.assertEqual(run.attempts, 1)
        completed = complete_snapshot_run(run_id, {"kpis": {"customers_total": 3}}, now=now + timedelta(seconds=5))
        self.assertEqual(completed.status, ManagerAnalyticsSnapshot.Status.SUCCEEDED)
        schedule.refresh_from_db()
        self.assertEqual(schedule.last_success_at, now + timedelta(seconds=5))
        self.assertEqual(schedule.last_period_key, run.period_key)
        self.assertGreater(schedule.next_run_at, now)
        self.assertEqual(claim_snapshot_run(run_id, now=now + timedelta(seconds=6)), None)

    def test_failed_snapshot_advances_schedule_and_keeps_diagnostic(self):
        now = timezone.now()
        ManagerSnapshotSchedule.objects.create(
            key="manager-overview",
            frequency=ManagerSnapshotSchedule.Frequency.HOURLY,
            timezone_name="UTC",
            next_run_at=now - timedelta(minutes=1),
        )
        run_id, state = acquire_due_snapshot(now=now)
        self.assertEqual(state, "queued")
        self.assertIsNotNone(claim_snapshot_run(run_id, now=now))
        finished_at = now + timedelta(minutes=1)
        failed = fail_snapshot_run(run_id, RuntimeError("snapshot aggregate failed"), now=finished_at)

        self.assertEqual(failed.status, ManagerAnalyticsSnapshot.Status.FAILED)
        self.assertIn("RuntimeError", failed.last_error)
        schedule = ManagerSnapshotSchedule.objects.get(key="manager-overview")
        self.assertEqual(schedule.last_error_at, finished_at)
        self.assertIn("snapshot aggregate failed", schedule.last_error)
        self.assertEqual(schedule.last_run_at, finished_at)
        self.assertGreater(schedule.next_run_at, finished_at)

    def test_schedule_api_is_superuser_only_and_audits_frequency_changes(self):
        client = APIClient()
        url = reverse("manager-snapshot-schedule")
        self.assertEqual(client.get(url).status_code, 401)

        ordinary = CustomUser.objects.create_user(
            email="snapshot-member@example.test",
            first_name="Mira",
            last_name="Member",
            password="SafePass!483",
        )
        client.force_authenticate(ordinary)
        self.assertEqual(client.patch(url, {"frequency": "WEEKLY"}, format="json").status_code, 403)

        client.force_authenticate(self.manager)
        response = client.patch(
            url,
            {"frequency": "WEEKLY", "reason": "Align reports with weekly operations review."},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["frequency"], ManagerSnapshotSchedule.Frequency.WEEKLY)
        audit = AuditLog.objects.get(action="MANAGER_SNAPSHOT_SCHEDULE_CONFIGURED")
        self.assertEqual(audit.actor, self.manager)
        self.assertEqual(audit.metadata["after"]["frequency"], ManagerSnapshotSchedule.Frequency.WEEKLY)
        self.assertEqual(audit.metadata["reason"], "Align reports with weekly operations review.")
        self.assertEqual(response.data["history"][0]["actor_name"], "Ayo Manager")
        self.assertEqual(response.data["history"][0]["reason"], "Align reports with weekly operations review.")

    def test_celery_beat_polls_database_schedule_each_minute(self):
        entry = celery_app.conf.beat_schedule["dispatch-manager-analytics-snapshots-every-minute"]
        self.assertEqual(entry["task"], "home.tasks.dispatch_manager_snapshot_if_due")
        self.assertEqual(len(entry["schedule"].minute), 60)
