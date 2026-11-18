"""Campaign audience rules, async dispatch, exports, staff privileges, privacy."""
from django.contrib.auth import get_user_model

from manager.models import (
    BackgroundJob,
    Campaign,
    CampaignDelivery,
    ContactPreference,
    ExportArtifact,
    ManagerProfile,
    ManagerRole,
    SavedReport,
)
from manager.services import campaigns as campaign_service
from manager.services import data_explorer, exports as export_service, jobs, staff

from .base import ManagerTestCase

User = get_user_model()


class AudienceTests(ManagerTestCase):
    def test_estimate_counts_exclusions_without_leaking_the_list(self):
        ContactPreference.objects.create(user=self.customer_user, channel="EMAIL", opted_out=True)
        result = campaign_service.estimate({"segment": "all_customers", "channels": ["EMAIL"]})
        self.assertEqual(result["estimated"], 1)
        self.assertEqual(result["opted_out"], 1)
        self.assertEqual(result["eligible"], 0)
        self.assertNotIn("emails", result)

    def test_an_opted_out_user_is_skipped_at_dispatch(self):
        ContactPreference.objects.create(user=self.customer_user, channel="IN_APP", opted_out=True)
        campaign = campaign_service.create(user=self.superuser, payload={
            "title": "Plumbing offer", "body": "Book a plumber today", "channels": ["IN_APP"],
            "segment": "all_customers",
        })
        campaign_service.approve(user=self.approver, campaign=campaign)
        built = campaign_service.build_recipients(campaign)
        self.assertEqual(built["created"], 0)
        self.assertGreaterEqual(built["skipped"], 1)

    def test_a_campaign_needs_a_different_approver(self):
        campaign = campaign_service.create(user=self.approver, payload={
            "title": "Hello", "body": "A message for you", "channels": ["IN_APP"], "segment": "all_customers",
        })
        with self.assertRaises(campaign_service.CampaignError):
            campaign_service.approve(user=self.approver, campaign=campaign)

    def test_dispatch_creates_deliveries_and_marks_the_campaign_sent(self):
        campaign = campaign_service.create(user=self.superuser, payload={
            "title": "Hello", "body": "A message for you", "channels": ["IN_APP"], "segment": "all_customers",
        })
        campaign_service.approve(user=self.approver, campaign=campaign)
        campaign_service.build_recipients(campaign)
        result = campaign_service.dispatch(campaign)
        self.assertEqual(result["sent"], 1)
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, Campaign.Status.SENT)
        self.assertEqual(CampaignDelivery.objects.filter(campaign=campaign, status="SENT").count(), 1)

    def test_dispatch_never_sends_the_same_delivery_twice(self):
        campaign = campaign_service.create(user=self.superuser, payload={
            "title": "Hello", "body": "A message for you", "channels": ["IN_APP"], "segment": "all_customers",
        })
        campaign_service.approve(user=self.approver, campaign=campaign)
        campaign_service.build_recipients(campaign)
        campaign_service.dispatch(campaign)
        again = campaign_service.dispatch(campaign)
        self.assertEqual(again["sent"], 0)

    def test_campaign_creation_and_dispatch_are_separate_api_calls(self):
        self.login_superuser()
        created = self.client.post("/api/v1/manager/campaigns/", {
            "title": "Weekend push", "body": "Book a verified artisan this weekend",
            "channels": ["IN_APP"], "segment": "all_customers",
        }, format="json")
        self.assertEqual(created.status_code, 201)
        campaign_id = created.json()["id"]
        self.assertEqual(CampaignDelivery.objects.filter(campaign_id=campaign_id).count(), 0)

        self.login(self.approver)
        approved = self.client.post(f"/api/v1/manager/campaigns/{campaign_id}/approve/", {}, format="json")
        self.assertEqual(approved.status_code, 200)

        self.login_superuser()
        dispatched = self.client.post(f"/api/v1/manager/campaigns/{campaign_id}/dispatch/", {}, format="json")
        self.assertEqual(dispatched.status_code, 202)
        self.assertTrue(BackgroundJob.objects.filter(kind="CAMPAIGN_DISPATCH").exists())

    def test_a_user_can_opt_out_and_it_is_respected(self):
        self.login(self.customer_user)
        response = self.client.post("/api/v1/manager/campaigns/opt-out/", {"channel": "EMAIL"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(ContactPreference.objects.get(user=self.customer_user, channel="EMAIL").opted_out)


class ExportTests(ManagerTestCase):
    def test_export_runs_as_a_job_and_writes_a_private_file(self):
        self.login_superuser()
        response = self.client.post("/api/v1/manager/exports/", {
            "spec": {"domain": "bookings", "measure": "count", "dimension": "status", "period": "last_30_days"},
            "format": "csv",
        }, format="json")
        self.assertEqual(response.status_code, 202, response.content)
        job = BackgroundJob.objects.get(kind="EXPORT")
        from manager.tasks import run_export_job

        run_export_job(str(job.id))
        job.refresh_from_db()
        self.assertEqual(job.status, BackgroundJob.Status.SUCCEEDED)
        artifact = ExportArtifact.objects.get(job=job)
        self.assertGreaterEqual(artifact.row_count, 1)
        path = export_service.storage_dir() / artifact.filename
        self.assertTrue(path.exists())
        self.assertIn("manager_exports", str(path))

    def test_a_lost_broker_message_is_republished_instead_of_stuck(self):
        """An outage must not wedge the job: the next identical request re-sends it."""
        from unittest import mock

        from django.utils import timezone as tz

        job, created = jobs.enqueue(kind="EXPORT", payload={
            "spec": {"domain": "bookings", "measure": "count"}, "format": "csv"},
            user=self.superuser, dedupe_key="export:test:csv")
        self.assertTrue(created)
        BackgroundJob.objects.filter(pk=job.pk).update(
            created_at=tz.now() - jobs.dedupe_redispatch_after() - tz.timedelta(seconds=1))

        with mock.patch.object(jobs, "_dispatch") as dispatch:
            same, created_again = jobs.enqueue(kind="EXPORT", payload={
                "spec": {"domain": "bookings", "measure": "count"}, "format": "csv"},
                user=self.superuser, dedupe_key="export:test:csv")
        self.assertFalse(created_again, "the caller still gets the original job")
        self.assertEqual(same.pk, job.pk)
        dispatch.assert_called_once()

        with mock.patch.object(jobs, "_dispatch") as dispatch:
            jobs.enqueue(kind="EXPORT", payload={
                "spec": {"domain": "bookings", "measure": "count"}, "format": "csv"},
                user=self.superuser, dedupe_key="export:fresh:csv")
        dispatch.assert_called_once()  # a brand new job is always published

    def test_an_export_is_never_produced_without_a_requesting_manager(self):
        from manager.services import jobs as job_service

        with self.assertRaises(job_service.JobError):
            jobs.enqueue(kind="EXPORT", payload={}, user=None)
        with self.assertRaises(job_service.JobError):
            jobs.enqueue(kind="REPORT", payload={}, user=None)

        # A row that somehow lost its requester fails cleanly instead of raising.
        job = BackgroundJob.objects.create(kind="EXPORT", queue="exports",
                                           payload={"spec": {}, "format": "csv"})
        from manager.tasks import run_export_job

        run_export_job(str(job.id))
        job.refresh_from_db()
        self.assertEqual(job.status, BackgroundJob.Status.FAILED)
        self.assertIn("requesting manager", job.error)
        self.assertFalse(ExportArtifact.objects.filter(job=job).exists())

    def test_export_download_is_authorised(self):
        self.login_superuser()
        job, _ = jobs.enqueue(kind="EXPORT", payload={
            "spec": {"domain": "bookings", "measure": "count"}, "format": "csv"}, user=self.superuser)
        from manager.tasks import run_export_job

        run_export_job(str(job.id))
        allowed = self.client.get(f"/api/v1/manager/exports/{job.id}/download/")
        self.assertEqual(allowed.status_code, 200)

        self.login(self.finance)  # finance has export.data? it does not by default
        denied = self.client.get(f"/api/v1/manager/exports/{job.id}/download/")
        self.assertEqual(denied.status_code, 403)

    def test_an_invalid_explorer_spec_is_rejected_before_queueing(self):
        self.login_superuser()
        response = self.client.post("/api/v1/manager/exports/", {
            "spec": {"domain": "bookings; DROP TABLE", "measure": "count"}, "format": "csv"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(BackgroundJob.objects.filter(kind="EXPORT").exists())

    def test_explorer_refuses_unknown_fields(self):
        with self.assertRaises(data_explorer.ExplorerError):
            data_explorer.run({"domain": "bookings", "dimension": "password"})
        with self.assertRaises(data_explorer.ExplorerError):
            data_explorer.run({"domain": "customers", "measure": "salary"})

    def test_saved_report_is_owned_and_shared_deliberately(self):
        self.login_superuser()
        response = self.client.post("/api/v1/manager/reports/", {
            "name": "Bookings by status",
            "spec": {"domain": "bookings", "measure": "count", "dimension": "status", "period": "last_30_days"},
        }, format="json")
        self.assertEqual(response.status_code, 201)
        report = SavedReport.objects.get(name="Bookings by status")
        self.assertEqual(report.owner, self.superuser)
        self.assertFalse(report.is_shared)


    def test_an_unknown_audience_segment_is_refused_at_creation(self):
        """Otherwise the KeyError lands in the dispatch worker, hours later."""
        with self.assertRaises(campaign_service.CampaignError):
            campaign_service.create(user=self.superuser, payload={
                "title": "Bad segment", "body": "x" * 20,
                "channels": ["IN_APP"], "segment": "customers_no_booking",
            })
        self.assertFalse(Campaign.objects.filter(title="Bad segment").exists())

    def test_progress_never_renders_above_one_hundred_percent(self):
        job, _ = jobs.enqueue(kind="EXPORT", user=self.superuser, payload={}, dedupe_key="percent-test")
        jobs.mark_progress(job.id, 1200, total=100)
        job.refresh_from_db()
        payload = jobs.status_payload(job)
        self.assertEqual(payload["percent"], 100.0, payload)

    def test_a_redelivered_export_reuses_its_artifact(self):
        """Celery is at-least-once; the second delivery must not crash."""
        job, _created = jobs.enqueue(kind="EXPORT", user=self.superuser,
                                     payload={"spec": {"domain": "bookings", "measure": "count"}, "format": "csv"},
                                     dedupe_key="redelivery-test")
        first = export_service.build_export(job)
        self.assertIsNotNone(first)
        again = export_service.build_export(job)
        self.assertEqual(again.pk, first.pk)
        self.assertEqual(ExportArtifact.objects.filter(job=job).count(), 1)

    def test_reading_the_notification_is_what_counts_as_an_open(self):
        """`opens` must be fed by a real signal, not left structurally at zero."""
        from notifications.in_app import create_in_app_notification, mark_notification_read
        from django.utils import timezone

        campaign = Campaign(
            title="Readable", body="x" * 20, channels=["IN_APP"],
            audience_spec={"segment": "all_customers"}, audience_count=1, created_by=self.superuser,
            status=Campaign.Status.SENT,
        )
        campaign.save()
        delivery = CampaignDelivery.objects.create(
            campaign=campaign, user=self.customer_user, channel="IN_APP",
            status=CampaignDelivery.Status.SENT, sent_at=timezone.now(),
        )
        notification = create_in_app_notification(
            user=self.customer_user, event_key=f"campaign:{campaign.id}:{self.customer_user.pk}",
            title=campaign.title, message=campaign.body,
        )
        self.assertEqual(campaign_service.analytics(campaign)["opens"], 0)

        mark_notification_read(notification_id=notification.pk, user=self.customer_user)

        delivery.refresh_from_db()
        self.assertIsNotNone(delivery.opened_at, "reading the notification records the open")
        payload = campaign_service.analytics(campaign)
        self.assertEqual(payload["opens"], 1)
        self.assertEqual(payload["open_rate"], 100.0)

        # An unrelated notification must not touch campaign metrics.
        other = create_in_app_notification(user=self.customer_user, event_key="booking:accepted",
                                           title="Booking", message="Accepted")
        mark_notification_read(notification_id=other.pk, user=self.customer_user)
        self.assertEqual(campaign_service.analytics(campaign)["opens"], 1)

    def test_a_dispatch_that_reaches_nobody_says_why(self):
        """A filled audience can still yield zero sends; the board must explain it."""
        campaign = Campaign(
            title="Frequency capped", body="x" * 20, channels=["IN_APP"],
            audience_spec={"segment": "all_customers"}, audience_count=1, created_by=self.superuser,
            status=Campaign.Status.SENT,
        )
        campaign.save()
        CampaignDelivery.objects.create(campaign=campaign, user=self.customer_user, channel="IN_APP",
                                        status=CampaignDelivery.Status.SENT)
        payload = campaign_service.analytics(campaign)
        self.assertEqual(payload["suppressed_at_dispatch"], 0)

        campaign.dispatch_summary = {"sent": 0, "skipped": 1200, "at": "2026-10-30T04:00:00+00:00"}
        campaign.save(update_fields=["dispatch_summary"])
        payload = campaign_service.analytics(campaign)
        self.assertEqual(payload["suppressed_at_dispatch"], 1200)
        self.assertEqual(payload["sent"], 1)


class StaffTests(ManagerTestCase):
    def test_a_manager_cannot_grant_permissions_they_do_not_hold(self):
        with self.assertRaises(staff.StaffError):
            staff.create_role(user=self.finance, slug="shadow-admin", name="Shadow",
                              permissions=["staff.manage", "system.manage"])

    def test_a_manager_cannot_change_their_own_role(self):
        profile = ManagerProfile.objects.get(user=self.finance)
        with self.assertRaises(staff.StaffError):
            staff.assign_role(user=self.finance, profile=profile, role_slug="finance-manager")

    def test_the_last_super_administrator_cannot_be_demoted(self):
        profile = ManagerProfile.objects.get(user=self.approver)
        with self.assertRaises(staff.StaffError):
            staff.assign_role(user=self.superuser, profile=profile, role_slug="support-agent")

    def test_suspending_staff_revokes_their_sessions(self):
        from manager.services import auth as auth_service

        session = auth_service.register_session(self.finance, "session-key-1")
        staff.suspend(user=self.superuser, profile=ManagerProfile.objects.get(user=self.finance), reason="test")
        session.refresh_from_db()
        self.assertIsNotNone(session.revoked_at)

    def test_staff_creation_requires_a_second_administrator_to_be_able_to_act(self):
        with self.assertRaises(staff.StaffError):
            staff.create_staff(user=self.finance, email="new@example.com", first_name="New",
                               last_name="Person", role_slug="finance-manager")


class PrivacyTests(ManagerTestCase):
    def test_retention_change_is_audited_and_bounded(self):
        from manager.services import governance

        governance.ensure_retention_policies()
        governance.set_retention(user=self.superuser, data_class="analytics_events", days=365)
        from core.models import AuditLog

        self.assertTrue(AuditLog.objects.filter(action="MANAGER_RETENTION_CHANGED").exists())
        with self.assertRaises(Exception):
            governance.set_retention(user=self.superuser, data_class="analytics_events", days=0)

    def test_purging_never_touches_financial_records(self):
        from datetime import timedelta

        from django.utils import timezone

        from manager.models import AnalyticsEvent
        from manager.services import governance

        governance.ensure_retention_policies()
        governance.set_retention(user=self.superuser, data_class="analytics_events", days=1)
        old = AnalyticsEvent.objects.create(
            name="booking.created", entity_id="x", dedupe_key="old-1",
            occurred_at=timezone.now() - timedelta(days=30), recorded_at=timezone.now(),
        )
        AnalyticsEvent.objects.filter(pk=old.pk).update(recorded_at=timezone.now() - timedelta(days=30))
        governance.purge_expired_retention()
        self.assertFalse(AnalyticsEvent.objects.filter(pk=old.pk).exists())
        self.payment.refresh_from_db()  # payments survive retention
        self.assertEqual(self.payment.total_amount_kobo, 3_365_000)

    def test_data_subject_request_records_the_action(self):
        from manager.services import governance

        request = governance.create_subject_request(user=self.superuser, subject_user_id=self.customer_user.pk,
                                                    request_type="ACCESS", details="Please send my data")
        self.assertTrue(request.reference.startswith("DSR-"))
        governance.advance_subject_request(user=self.superuser, request=request, status="COMPLETED",
                                           notes="Delivered by email")
        request.refresh_from_db()
        self.assertIsNotNone(request.completed_at)


class HealthTests(ManagerTestCase):
    def test_health_reports_real_probes(self):
        self.login_superuser()
        payload = self.client.get("/api/v1/manager/health/").json()
        self.assertEqual(payload["database"]["status"], "healthy")
        self.assertIn("overall", payload)
        self.assertIn("jobs", payload)
        self.assertNotIn("SECRET", str(payload).upper())
