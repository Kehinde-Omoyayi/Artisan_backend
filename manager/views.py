"""Manager console API.

Every endpoint on this blueprint requires, in this order:

1. an authenticated session (``IsAuthenticated``);
2. an active manager profile (``IsManager`` - a customer or artisan can never pass);
3. the specific permission code declared on the view (``ManagerPermission``);
4. for sensitive writes, a single-use step-up token (checked in the service layer).

The frontend receives permission codes so it can hide controls, but every request is
authorised again here. Nothing is trusted from the client: no price, no commission,
no audience count, no financial total.
"""
from __future__ import annotations

from django.core.cache import cache
from django.db.models import Count, Q
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import PermissionDenied, Throttled, ValidationError
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.services import create_audit_log
from manager.models import (
    BackgroundJob,
    Campaign,
    ConfigChangeRequest,
    DataSubjectRequest,
    ExportArtifact,
    ManagerProfile,
    ManagerRole,
    PlatformConfigVersion,
    RiskReview,
    SavedReport,
)
from manager.permissions import PERMISSIONS, STEP_UP_SCOPES
from manager.services import auth as auth_service
from manager.services import campaigns as campaign_service
from manager.services import config_engine, data_explorer, events as event_service
from manager.services import exports as export_service
from manager.services import governance, health
from manager.services import jobs as job_service
from manager.services import metrics
from manager.services import snapshots as snapshot_service
from manager.services import staff as staff_service


class IsManager(BasePermission):
    message = "The manager console is restricted to active managers."

    def has_permission(self, request, view):
        user = getattr(request, "user", None)
        if not (user and user.is_authenticated and user.is_active):
            return False
        return auth_service.is_manager(user)


class ManagerAPIView(APIView):
    """Base class: manager identity, permission code, and a per-user rate limit."""

    permission_classes = [IsAuthenticated, IsManager]
    required_permission = ""
    rate_limit = "default"

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        self.check_rate_limit(request)
        if self.required_permission:
            auth_service.require_perm(request.user, self.required_permission)

    def check_rate_limit(self, request):
        limit, window = {
            "default": (240, 60),
            "export": (6, 60),
            "campaign": (20, 60),
            "write": (60, 60),
        }.get(self.rate_limit, (240, 60))
        import hashlib

        bucket = f"manager:throttle:{self.rate_limit}:{request.user.pk}:{int(timezone.now().timestamp()) // window}"
        key = "mgr:" + hashlib.sha256(bucket.encode()).hexdigest()
        count = cache.get(key, 0) + 1
        cache.set(key, count, window + 5)
        if count > limit:
            raise Throttled(detail=f"Too many requests. Try again in {window} seconds.")

    def audit(self, request, action, resource_type, resource_id="", metadata=None):
        create_audit_log(action=action, resource_type=resource_type, resource_id=str(resource_id),
                         actor=request.user, metadata={**(metadata or {}), "via": "manager-api"},
                         request_id=getattr(request, "request_id", ""))


def _params(request):
    data = {}
    for source in (request.query_params, getattr(request, "data", {})):
        if hasattr(source, "items"):
            data.update({key: value for key, value in source.items()})
    return data


# --------------------------------------------------------------------------- #
# Session / identity
# --------------------------------------------------------------------------- #
class ManagerSessionView(ManagerAPIView):
    """Everything the shell needs before it can render a page."""

    def get(self, request):
        user = request.user
        payload = auth_service.manager_bootstrap_payload(user)
        payload["permissions_catalogue"] = [
            {"code": code, "label": label, "category": category}
            for code, (label, category) in PERMISSIONS.items()
        ]
        payload["step_up_scopes"] = STEP_UP_SCOPES
        payload["sessions"] = auth_service.active_sessions(user)
        payload["server_time"] = timezone.now().isoformat()
        profile = ManagerProfile.objects.select_related("role").filter(user=user).first()
        payload["security"] = {
            "totp_enrolled": bool(profile and profile.totp_enrolled),
            "email_otp_enabled": bool(profile and profile.email_otp_enabled),
            "must_change_password": bool(profile and profile.must_change_password),
            "last_authenticated_at": profile.last_authenticated_at.isoformat() if profile and profile.last_authenticated_at else None,
        }
        return Response(payload)


class TotpEnrollView(ManagerAPIView):
    def post(self, request):
        data = auth_service.begin_totp_enrollment(request.user)
        self.audit(request, "MANAGER_TOTP_ENROLLMENT_STARTED", "MANAGER_PROFILE", request.user.pk)
        return Response(data)


class TotpConfirmView(ManagerAPIView):
    def post(self, request):
        code = str(request.data.get("code") or "").strip()
        try:
            confirmed = auth_service.confirm_totp_enrollment(request.user, code)
        except auth_service.SecondFactorUnavailable:
            raise PermissionDenied(
                "The stored authenticator secret cannot be read. Ask a super administrator "
                "to reset it, then start enrolment again."
            )
        if not confirmed:
            raise ValidationError({"code": "That code is not valid. Check the clock on your device."})
        return Response({"totp_enrolled": True})


class StepUpView(ManagerAPIView):
    """Exchange password (+ second factor) for a short-lived, scoped grant."""

    rate_limit = "write"

    def post(self, request):
        scope = str(request.data.get("scope") or "").strip()
        if scope not in STEP_UP_SCOPES:
            raise ValidationError({"scope": "Unknown step-up scope."})
        password = str(request.data.get("password") or "")
        code = str(request.data.get("code") or "").strip()
        user = request.user
        if not password or not user.check_password(password):
            auth_service.register_login_failure(email=user.email, ip=self._ip(request))
            raise ValidationError({"password": "That password is not correct."})
        profile = auth_service.ensure_profile(user)
        if profile and profile.totp_enrolled:
            try:
                totp_ok = auth_service.verify_manager_totp(user, code)
            except auth_service.SecondFactorUnavailable:
                raise PermissionDenied(
                    "This account's authenticator secret cannot be read, so it cannot be "
                    "verified. Ask a super administrator to reset it and enrol again."
                )
            if not totp_ok:
                raise ValidationError({"code": "That authenticator code is not valid."})
        elif profile and profile.email_otp_enabled and code:
            if not auth_service.verify_email_challenge(user, "STEP_UP", code):
                raise ValidationError({"code": "That verification code is not valid or has expired."})
        elif profile and profile.email_otp_enabled:
            # No code supplied and no authenticator yet: tell the caller to request a
            # challenge rather than silently issuing a grant.
            raise PermissionDenied(
                "Enrol an authenticator app, or request an email verification code first."
            )
        else:
            raise PermissionDenied("Enrol an authenticator app before performing sensitive actions.")
        grant = auth_service.issue_step_up(user, scope, ip_hash=self._ip_hash(request))
        return Response(grant)

    def _ip(self, request):
        from risk.network import get_client_ip

        return get_client_ip(request) or request.META.get("REMOTE_ADDR", "")

    def _ip_hash(self, request):
        import hashlib

        return hashlib.sha256(str(self._ip(request)).encode()).hexdigest()


class StepUpChallengeView(ManagerAPIView):
    """Send an email fallback code for a step-up when TOTP is not enrolled."""

    rate_limit = "write"

    def post(self, request):
        auth_service.issue_email_challenge(request.user, "STEP_UP")
        return Response({"sent": True, "expires_in": 300})


class ManagerSessionRevokeView(ManagerAPIView):
    rate_limit = "write"

    def post(self, request, session_id):
        if not auth_service.revoke_session(request.user, session_id):
            raise ValidationError({"detail": "That session no longer exists."})
        return Response({"revoked": True})


# --------------------------------------------------------------------------- #
# Analytics pages
# --------------------------------------------------------------------------- #
class AnalyticsView(ManagerAPIView):
    """One class for every read-only analytics page; ``section`` picks the builder."""

    required_permission = "analytics.view"
    section = None

    def get(self, request):
        params = _params(request)
        builder = {
            "overview": metrics.overview,
            "financial": metrics.financial,
            "business": metrics.business,
            "services": metrics.services,
            "customers": metrics.customers,
            "artisans": metrics.artisans,
            "funnel": metrics.funnel,
            "marketing": metrics.marketing,
            "geography": metrics.geography,
            "insights": metrics.insights,
            "data_quality": metrics.data_quality,
            "support": metrics.support,
            "risk": metrics.risk,
        }[self.section]
        extra_permission = {
            "financial": "analytics.financial",
            "customers": "analytics.customer",
            "artisans": "analytics.artisan",
            "risk": "risk.view",
            "support": "support.view",
        }.get(self.section)
        if extra_permission:
            auth_service.require_perm(request.user, extra_permission)
        try:
            payload = builder(request.user, params)
        except metrics.FilterError as exc:
            raise ValidationError({"detail": str(exc)})
        payload["viewer_permissions"] = sorted(auth_service.effective_permissions(request.user))
        return Response(payload)


class HealthView(ManagerAPIView):
    required_permission = "system.health"

    def get(self, request):
        payload = health.snapshot()
        payload["latency"] = health.latency_percentiles()
        return Response(payload)


class AuditView(ManagerAPIView):
    required_permission = "audit.view"

    def get(self, request):
        try:
            page = max(1, int(request.query_params.get("page") or 1))
        except ValueError:
            page = 1
        payload = governance.audit_page(
            page=page,
            size=min(200, int(request.query_params.get("size") or 50)),
            action=request.query_params.get("action") or "",
            actor_id=request.query_params.get("actor") or "",
            resource_type=request.query_params.get("resource_type") or "",
        )
        payload["filters"] = governance.audit_filters()
        return Response(payload)


# --------------------------------------------------------------------------- #
# Configuration centre
# --------------------------------------------------------------------------- #
def _step_up_token(request):
    return str(request.data.get("step_up_token") or request.headers.get("X-Step-Up-Token") or "")


class ConfigIndexView(ManagerAPIView):
    required_permission = "config.view"

    def get(self, request):
        rows = config_engine.all_settings()
        visible = [
            row for row in rows
            if auth_service.has_perm(request.user, row["permission"]) or auth_service.has_perm(request.user, "config.view")
        ]
        pending = ConfigChangeRequest.objects.filter(status=ConfigChangeRequest.Status.PENDING).select_related(
            "requested_by").order_by("-created_at")[:20]
        return Response({
            "settings": visible,
            "pending_changes": [
                {
                    "id": str(row.id), "key": row.key, "proposed_value": row.proposed_value,
                    "current_value": row.current_value.get("value") if isinstance(row.current_value, dict) else None,
                    "reason": row.reason, "requires_approval": row.requires_approval,
                    "requested_by": row.requested_by.get_full_name() or row.requested_by.email,
                    "requested_by_id": str(row.requested_by_id),
                    "created_at": row.created_at.isoformat(),
                }
                for row in pending
            ],
            "step_up_scope": "config:finance",
            "note": "Financial settings change only for new bookings. Accepted agreements keep the terms they were accepted with.",
        })


class ConfigPreviewView(ManagerAPIView):
    required_permission = "config.view"
    rate_limit = "write"

    def post(self, request, key):
        try:
            return Response(config_engine.preview(key, request.data.get("value")))
        except config_engine.ConfigError as exc:
            raise ValidationError({"value": str(exc)})


class ConfigChangeView(ManagerAPIView):
    required_permission = "config.view"
    rate_limit = "write"

    def post(self, request, key):
        from manager.config_spec import SENSITIVE, ConfigError, spec_for
        from manager.services.auth import has_perm

        try:
            spec = spec_for(key)
        except ConfigError as exc:
            # A mistyped key is the caller's mistake, never a server fault.
            raise ValidationError({"key": str(exc)})
        if spec["category"] == SENSITIVE:
            auth_service.require_step_up(request.user, "config:finance", _step_up_token(request))
        elif not has_perm(request.user, spec["permission"]):
            raise PermissionDenied("Your role cannot change this setting.")
        try:
            change = config_engine.request_change(
                user=request.user,
                key=key,
                value=request.data.get("value"),
                reason=request.data.get("reason") or "",
                effective_from=None,
            )
        except config_engine.ConfigPermissionError as exc:
            raise PermissionDenied(str(exc))
        except config_engine.ConfigError as exc:
            raise ValidationError({"value": str(exc)})
        self.audit(request, "MANAGER_CONFIG_CHANGE_REQUESTED", "PLATFORM_CONFIG", key,
                   {"requires_approval": change.requires_approval})
        return Response(_change_payload(change), status=status.HTTP_201_CREATED)


def _change_payload(change):
    return {
        "id": str(change.id),
        "key": change.key,
        "status": change.status,
        "requires_approval": change.requires_approval,
        "proposed_value": change.proposed_value,
        "preview": change.preview,
        "applied_version": change.applied_version,
        "created_at": change.created_at.isoformat(),
    }


class ConfigApproveView(ManagerAPIView):
    required_permission = "config.approve"
    rate_limit = "write"

    def post(self, request, change_id):
        auth_service.require_step_up(request.user, "config:approve", _step_up_token(request))
        change = get_object_or_404(ConfigChangeRequest, pk=change_id)
        try:
            config_engine.approve_change(user=request.user, change=change)
        except config_engine.ConfigPermissionError as exc:
            raise PermissionDenied(str(exc))
        except config_engine.ConfigError as exc:
            raise ValidationError({"detail": str(exc)})
        return Response(_change_payload(change))


class ConfigRejectView(ManagerAPIView):
    required_permission = "config.approve"

    def post(self, request, change_id):
        change = get_object_or_404(ConfigChangeRequest, pk=change_id)
        try:
            config_engine.reject_change(user=request.user, change=change, reason=request.data.get("reason") or "")
        except config_engine.ConfigError as exc:
            raise ValidationError({"detail": str(exc)})
        return Response(_change_payload(change))


class ConfigHistoryView(ManagerAPIView):
    required_permission = "config.view"

    def get(self, request, key):
        return Response({
            "key": key,
            "current": config_engine.current_with_meta(key),
            "history": config_engine.history(key),
        })


class ConfigRollbackView(ManagerAPIView):
    required_permission = "config.finance"
    rate_limit = "write"

    def post(self, request, key):
        from manager.config_spec import SENSITIVE, ConfigError, spec_for

        try:
            spec = spec_for(key)
        except ConfigError as exc:
            raise ValidationError({"key": str(exc)})
        if spec["category"] == SENSITIVE:
            auth_service.require_step_up(request.user, "config:finance", _step_up_token(request))
        try:
            change = config_engine.rollback(
                user=request.user, key=key,
                to_version=int(request.data.get("version") or 0),
                reason=request.data.get("reason") or "",
            )
        except PlatformConfigVersion.DoesNotExist:
            raise ValidationError({"version": "That version does not exist."})
        except config_engine.ConfigError as exc:
            raise ValidationError({"detail": str(exc)})
        return Response(_change_payload(change), status=status.HTTP_201_CREATED)


# --------------------------------------------------------------------------- #
# Snapshots
# --------------------------------------------------------------------------- #
class SnapshotStatusView(ManagerAPIView):
    required_permission = "analytics.snapshot"

    def get(self, request):
        payload = snapshot_service.status()
        payload["note"] = "One snapshot per configured interval, kept as history. Never one row overwritten."
        return Response(payload)


class SnapshotIntervalView(ManagerAPIView):
    required_permission = "config.snapshot_interval"
    rate_limit = "write"

    def post(self, request):
        try:
            row = snapshot_service.set_interval(int(request.data.get("seconds") or 300), user=request.user)
        except (TypeError, ValueError) as exc:
            raise ValidationError({"seconds": str(exc)})
        self.audit(request, "MANAGER_SNAPSHOT_INTERVAL_CHANGED", "SNAPSHOT_SETTING", "operational",
                   {"seconds": row.interval_seconds})
        return Response(snapshot_service.status())


class SnapshotCaptureView(ManagerAPIView):
    required_permission = "analytics.snapshot"
    rate_limit = "write"

    def post(self, request):
        job, created = job_service.enqueue(kind="SNAPSHOT", payload={}, user=request.user,
                                           dedupe_key="snapshot:manual")
        return Response(job_service.status_payload(job), status=status.HTTP_202_ACCEPTED)


class SnapshotHistoryView(ManagerAPIView):
    required_permission = "analytics.snapshot"

    def get(self, request, metric_key):
        return Response({"metric": metric_key, "points": snapshot_service.history(metric_key)})


# --------------------------------------------------------------------------- #
# Data explorer, saved reports, exports
# --------------------------------------------------------------------------- #
class ExplorerCatalogueView(ManagerAPIView):
    required_permission = "analytics.build"

    def get(self, request):
        return Response(data_explorer.catalogue())


class ExplorerRunView(ManagerAPIView):
    required_permission = "analytics.build"
    rate_limit = "write"

    def post(self, request):
        try:
            payload = data_explorer.cached_run(request.data if hasattr(request.data, "items") else {})
        except data_explorer.ExplorerError as exc:
            raise ValidationError({"detail": str(exc)})
        except metrics.FilterError as exc:
            raise ValidationError({"detail": str(exc)})
        return Response(payload)


class SavedReportListView(ManagerAPIView):
    required_permission = "analytics.build"

    def get(self, request):
        rows = (SavedReport.objects.filter(owner=request.user) | SavedReport.objects.filter(is_shared=True))
        return Response({
            "generated_at": timezone.now().isoformat(),
            "results": [
                # A shared report has to say whose it is; the console cannot infer it.
                {"id": str(row.id), "name": row.name, "description": row.description, "spec": row.spec,
                 "is_shared": row.is_shared, "is_mine": row.owner_id == request.user.pk,
                 "owner": row.owner.get_full_name() or row.owner.email,
                 "updated_at": row.updated_at.isoformat()}
                for row in rows.select_related("owner").distinct().order_by("-updated_at")[:100]
            ]
        })

    def post(self, request):
        name = str(request.data.get("name") or "").strip()[:120]
        if len(name) < 3:
            raise ValidationError({"name": "Give the report a name."})
        spec = request.data.get("spec") or {}
        try:
            data_explorer.run(spec, limit=5)  # validate before saving
        except (data_explorer.ExplorerError, metrics.FilterError) as exc:
            raise ValidationError({"spec": str(exc)})
        report, _ = SavedReport.objects.update_or_create(
            owner=request.user, name=name,
            defaults={"description": str(request.data.get("description") or "")[:300],
                      "spec": spec, "is_shared": bool(request.data.get("is_shared"))},
        )
        self.audit(request, "MANAGER_REPORT_SAVED", "SAVED_REPORT", report.id, {"name": name})
        return Response({"id": str(report.id), "name": report.name}, status=status.HTTP_201_CREATED)


class SavedReportDetailView(ManagerAPIView):
    required_permission = "analytics.build"

    def post(self, request, report_id):
        report = get_object_or_404(SavedReport, pk=report_id)
        if report.owner_id != request.user.pk and not report.is_shared:
            raise PermissionDenied("That report is private.")
        try:
            return Response(data_explorer.cached_run(report.spec))
        except (data_explorer.ExplorerError, metrics.FilterError) as exc:
            raise ValidationError({"detail": str(exc)})

    def delete(self, request, report_id):
        report = get_object_or_404(SavedReport, pk=report_id)
        if report.owner_id != request.user.pk:
            raise PermissionDenied("Only the owner can delete a report.")
        report.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ExportRequestView(ManagerAPIView):
    required_permission = "export.data"
    rate_limit = "export"

    def post(self, request):
        spec = request.data.get("spec") or {}
        fmt = str(request.data.get("format") or "csv")
        sensitive = bool(request.data.get("sensitive"))
        if sensitive:
            if not auth_service.has_perm(request.user, "export.sensitive"):
                raise PermissionDenied("Your role cannot export sensitive data.")
            auth_service.require_step_up(request.user, "export:sensitive", _step_up_token(request))
        try:
            job = export_service.request_export(user=request.user, spec=spec, fmt=fmt, sensitive=sensitive)
        except (data_explorer.ExplorerError, metrics.FilterError) as exc:
            raise ValidationError({"detail": str(exc)})
        except job_service.JobError as exc:
            raise ValidationError({"detail": str(exc)})
        return Response(job_service.status_payload(job), status=status.HTTP_202_ACCEPTED)


class ExportDownloadView(ManagerAPIView):
    required_permission = "export.data"

    def get(self, request, job_id):
        artifact = ExportArtifact.objects.filter(job_id=job_id).first()
        if not artifact:
            raise ValidationError({"detail": "That export is not ready."})
        if artifact.requested_by_id != request.user.pk and not auth_service.has_perm(request.user, "export.sensitive"):
            raise PermissionDenied("That export belongs to another manager.")
        if artifact.expires_at <= timezone.now():
            raise ValidationError({"detail": "That export has expired. Run it again."})
        try:
            path, content_type, filename = export_service.download_payload(artifact)
        except FileNotFoundError as exc:
            raise ValidationError({"detail": str(exc)})
        self.audit(request, "MANAGER_EXPORT_DOWNLOADED", "EXPORT_ARTIFACT", artifact.id, {"rows": artifact.row_count})
        return FileResponse(open(path, "rb"), content_type=content_type, as_attachment=True, filename=filename)


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #
class JobListView(ManagerAPIView):
    required_permission = "analytics.view"

    def get(self, request):
        # A manager sees their own jobs plus system jobs (no requester); a staff
        # manager with staff.view sees the whole queue.
        rows = BackgroundJob.objects.all()
        if not auth_service.has_perm(request.user, "staff.view"):
            rows = rows.filter(Q(requested_by=request.user) | Q(requested_by__isnull=True))
        rows = rows.order_by("-created_at")[:50]
        return Response({"results": [job_service.status_payload(job) for job in rows],
                         "queue": job_service.queue_health()})


class JobDetailView(ManagerAPIView):
    required_permission = "analytics.view"

    def get(self, request, job_id):
        job = get_object_or_404(BackgroundJob, pk=job_id)
        if job.requested_by_id not in {None, request.user.pk} and not auth_service.has_perm(request.user, "staff.view"):
            raise PermissionDenied("That job belongs to another manager.")
        return Response(job_service.status_payload(job))


class JobCancelView(ManagerAPIView):
    required_permission = "analytics.view"

    def post(self, request, job_id):
        job = get_object_or_404(BackgroundJob, pk=job_id)
        if job.requested_by_id != request.user.pk and not auth_service.has_perm(request.user, "staff.manage"):
            raise PermissionDenied("That job belongs to another manager.")
        try:
            job_service.cancel(job, user=request.user)
        except job_service.JobError as exc:
            raise ValidationError({"detail": str(exc)})
        return Response(job_service.status_payload(job))


class ReconciliationRunView(ManagerAPIView):
    required_permission = "finance.reconcile"
    rate_limit = "write"

    def post(self, request):
        job, _ = job_service.enqueue(kind="RECONCILIATION", payload={"filters": _params(request)},
                                     user=request.user)
        return Response(job_service.status_payload(job), status=status.HTTP_202_ACCEPTED)


# --------------------------------------------------------------------------- #
# Campaigns
# --------------------------------------------------------------------------- #
class CampaignListView(ManagerAPIView):
    required_permission = "campaign.view"

    def get(self, request):
        rows = Campaign.objects.select_related("created_by").order_by("-created_at")[:100]
        return Response({
            "generated_at": timezone.now().isoformat(),
            "segments": [{"key": key, "label": value["label"]} for key, value in campaign_service.SEGMENTS.items()],
            "channels": [{"key": key, "label": value} for key, value in Campaign.Channel.choices],
            "results": [campaign_service.analytics(row) for row in rows],
        })

    def post(self, request):
        if not auth_service.has_perm(request.user, "campaign.create"):
            raise PermissionDenied("Your role cannot create campaigns.")
        try:
            campaign = campaign_service.create(user=request.user, payload=request.data)
        except campaign_service.CampaignError as exc:
            raise ValidationError({"detail": str(exc)})
        return Response(campaign_service.analytics(campaign), status=status.HTTP_201_CREATED)


class CampaignEstimateView(ManagerAPIView):
    required_permission = "campaign.view"
    rate_limit = "campaign"

    def post(self, request):
        try:
            return Response(campaign_service.estimate(request.data))
        except campaign_service.CampaignError as exc:
            raise ValidationError({"detail": str(exc)})


class CampaignDetailView(ManagerAPIView):
    required_permission = "campaign.view"

    def get(self, request, campaign_id):
        campaign = get_object_or_404(Campaign, pk=campaign_id)
        payload = campaign_service.analytics(campaign)
        payload["body"] = campaign.body
        payload["subject"] = campaign.subject
        payload["channels"] = campaign.channels
        payload["audience_spec"] = campaign.audience_spec
        payload["scheduled_for"] = campaign.scheduled_for.isoformat() if campaign.scheduled_for else None
        payload["can_approve"] = auth_service.has_perm(request.user, "campaign.approve")
        return Response(payload)


class CampaignApproveView(ManagerAPIView):
    required_permission = "campaign.approve"
    rate_limit = "campaign"

    def post(self, request, campaign_id):
        campaign = get_object_or_404(Campaign, pk=campaign_id)
        try:
            campaign_service.approve(user=request.user, campaign=campaign)
        except campaign_service.CampaignError as exc:
            raise ValidationError({"detail": str(exc)})
        return Response(campaign_service.analytics(campaign))


class CampaignDispatchView(ManagerAPIView):
    required_permission = "campaign.send"
    rate_limit = "campaign"

    def post(self, request, campaign_id):
        campaign = get_object_or_404(Campaign, pk=campaign_id)
        try:
            job = campaign_service.queue_dispatch(user=request.user, campaign=campaign)
        except campaign_service.CampaignError as exc:
            raise ValidationError({"detail": str(exc)})
        return Response({"job": job_service.status_payload(job), "campaign": campaign_service.analytics(campaign)},
                        status=status.HTTP_202_ACCEPTED)


class CampaignCancelView(ManagerAPIView):
    required_permission = "campaign.create"

    def post(self, request, campaign_id):
        campaign = get_object_or_404(Campaign, pk=campaign_id)
        if campaign.status in {Campaign.Status.SENT}:
            raise ValidationError({"detail": "A sent campaign cannot be cancelled."})
        campaign.status = Campaign.Status.CANCELLED
        campaign.save(update_fields=["status", "updated_at"])
        self.audit(request, "MANAGER_CAMPAIGN_CANCELLED", "CAMPAIGN", campaign.id)
        return Response(campaign_service.analytics(campaign))


class CampaignOptOutView(APIView):
    """Available to every signed-in user: honoured by every future campaign."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        channel = str(request.data.get("channel") or "EMAIL")
        if channel not in {"IN_APP", "EMAIL", "PUSH", "SMS"}:
            raise ValidationError({"channel": "Unknown channel."})
        campaign_service.opt_out(user=request.user, channel=channel, reason=request.data.get("reason") or "")
        return Response({"opted_out": True, "channel": channel})


# --------------------------------------------------------------------------- #
# Staff and roles
# --------------------------------------------------------------------------- #
class StaffListView(ManagerAPIView):
    required_permission = "staff.view"

    def get(self, request):
        return Response({
            "staff": staff_service.list_staff(),
            "roles": staff_service.list_roles(),
            "permission_catalogue": staff_service.permission_catalogue(),
            "can_manage": auth_service.has_perm(request.user, "staff.manage"),
        })

    def post(self, request):
        auth_service.require_step_up(request.user, "staff:manage", _step_up_token(request))
        try:
            account, profile, temporary_password = staff_service.create_staff(
                user=request.user,
                email=request.data.get("email"),
                first_name=request.data.get("first_name"),
                last_name=request.data.get("last_name"),
                role_slug=request.data.get("role") or "",
                job_title=request.data.get("job_title") or "",
            )
        except staff_service.StaffError as exc:
            raise ValidationError({"detail": str(exc)})
        return Response({
            "id": str(profile.id),
            "email": account.email,
            "temporary_password": temporary_password,
            "note": "Share this password through a secure channel. The account must change it at first sign-in "
                    "and cannot use the console until a second factor is enrolled.",
        }, status=status.HTTP_201_CREATED)


class StaffRoleAssignView(ManagerAPIView):
    required_permission = "staff.manage"
    rate_limit = "write"

    def post(self, request, profile_id):
        auth_service.require_step_up(request.user, "staff:manage", _step_up_token(request))
        profile = get_object_or_404(ManagerProfile, pk=profile_id)
        try:
            staff_service.assign_role(user=request.user, profile=profile, role_slug=request.data.get("role") or "")
        except staff_service.StaffError as exc:
            raise ValidationError({"detail": str(exc)})
        return Response({"id": str(profile.id), "role": profile.role.slug})


class StaffSuspendView(ManagerAPIView):
    required_permission = "staff.manage"

    def post(self, request, profile_id):
        profile = get_object_or_404(ManagerProfile, pk=profile_id)
        action = str(request.data.get("action") or "suspend")
        try:
            if action == "restore":
                staff_service.restore(user=request.user, profile=profile)
            else:
                staff_service.suspend(user=request.user, profile=profile, reason=request.data.get("reason") or "")
        except staff_service.StaffError as exc:
            raise ValidationError({"detail": str(exc)})
        return Response({"id": str(profile.id), "is_suspended": profile.is_suspended})


class RoleListView(ManagerAPIView):
    required_permission = "staff.view"

    def get(self, request):
        return Response({"roles": staff_service.list_roles(), "system_roles": staff_service.system_role_summary()})

    def post(self, request):
        try:
            role = staff_service.create_role(
                user=request.user,
                slug=request.data.get("slug") or "",
                name=request.data.get("name") or "",
                description=request.data.get("description") or "",
                permissions=request.data.get("permissions") or [],
            )
        except staff_service.StaffError as exc:
            raise ValidationError({"detail": str(exc)})
        return Response(staff_service.role_payload(role), status=status.HTTP_201_CREATED)


class RolePermissionsView(ManagerAPIView):
    required_permission = "staff.manage"

    def post(self, request, slug):
        role = get_object_or_404(ManagerRole, slug=slug)
        try:
            staff_service.set_permissions(user=request.user, role=role,
                                          permissions=request.data.get("permissions") or [])
        except staff_service.StaffError as exc:
            raise ValidationError({"detail": str(exc)})
        return Response(staff_service.role_payload(role))


# --------------------------------------------------------------------------- #
# Privacy, retention, risk
# --------------------------------------------------------------------------- #
class PrivacyView(ManagerAPIView):
    required_permission = "privacy.view"

    def get(self, request):
        return Response(governance.privacy_overview())


class RetentionUpdateView(ManagerAPIView):
    required_permission = "privacy.manage"
    rate_limit = "write"

    def post(self, request, data_class):
        auth_service.require_step_up(request.user, "privacy:manage", _step_up_token(request))
        try:
            governance.set_retention(user=request.user, data_class=data_class,
                                     days=int(request.data.get("days") or 0),
                                     enabled=bool(request.data.get("enabled", True)))
        except (governance.GovernanceError, ValueError) as exc:
            raise ValidationError({"detail": str(exc)})
        return Response({"data_class": data_class, "days": int(request.data.get("days") or 0)})


class DataSubjectRequestView(ManagerAPIView):
    required_permission = "privacy.view"

    def get(self, request):
        rows = DataSubjectRequest.objects.order_by("-created_at")[:100]
        return Response({"results": [governance.subject_request_payload(row) for row in rows]})

    def post(self, request):
        try:
            row = governance.create_subject_request(
                user=request.user,
                subject_user_id=request.data.get("user_id"),
                request_type=request.data.get("type") or "",
                details=request.data.get("details") or "",
            )
        except governance.GovernanceError as exc:
            raise ValidationError({"detail": str(exc)})
        return Response(governance.subject_request_payload(row), status=status.HTTP_201_CREATED)


class DataSubjectRequestDetailView(ManagerAPIView):
    required_permission = "privacy.manage"

    def post(self, request, reference):
        auth_service.require_step_up(request.user, "privacy:manage", _step_up_token(request))
        row = get_object_or_404(DataSubjectRequest, reference=reference)
        try:
            governance.advance_subject_request(user=request.user, request=row,
                                               status=request.data.get("status") or "",
                                               notes=request.data.get("notes") or "")
        except governance.GovernanceError as exc:
            raise ValidationError({"detail": str(exc)})
        return Response(governance.subject_request_payload(row))


class RiskReviewListView(ManagerAPIView):
    required_permission = "risk.manage"

    def get(self, request):
        rows = RiskReview.objects.select_related("subject_user").order_by("-created_at")[:100]
        return Response({"results": [
            {"id": str(row.id), "signal": row.signal, "severity": row.severity, "status": row.status,
             "summary": row.summary, "created_at": row.created_at.isoformat()}
            for row in rows
        ]})

    def post(self, request):
        user_id = request.data.get("user_id")
        if not user_id:
            raise ValidationError({"user_id": "Choose the account to review."})
        review = RiskReview.objects.create(
            subject_user_id=user_id,
            signal=str(request.data.get("signal") or "manual_review")[:60],
            severity=str(request.data.get("severity") or "MEDIUM")[:10],
            summary=str(request.data.get("summary") or "")[:300],
            evidence={"source": "manager", "actor": str(request.user.pk)},
        )
        self.audit(request, "MANAGER_RISK_REVIEW_OPENED", "RISK_REVIEW", review.id, {"signal": review.signal})
        return Response({"id": str(review.id), "status": review.status}, status=status.HTTP_201_CREATED)


class RiskReviewDecisionView(ManagerAPIView):
    required_permission = "risk.manage"

    def post(self, request, review_id):
        review = get_object_or_404(RiskReview, pk=review_id)
        new_status = str(request.data.get("status") or "")
        if new_status not in RiskReview.Status.values:
            raise ValidationError({"status": "Unknown status."})
        review.status = new_status
        review.decision_notes = str(request.data.get("notes") or "")[:4000]
        review.reviewed_by = request.user
        review.save(update_fields=["status", "decision_notes", "reviewed_by", "updated_at"])
        self.audit(request, "MANAGER_RISK_REVIEW_DECIDED", "RISK_REVIEW", review.id, {"status": new_status})
        return Response({"id": str(review.id), "status": review.status})


# --------------------------------------------------------------------------- #
# Exports of the audit trail and insight export
# --------------------------------------------------------------------------- #
class InsightActionView(ManagerAPIView):
    required_permission = "analytics.view"

    def post(self, request):
        action = str(request.data.get("action") or "")
        if action not in {"acknowledge", "dismiss"}:
            raise ValidationError({"action": "Unknown action."})
        key = str(request.data.get("key") or "")[:60]
        event_service.record(
            "manager.insight_acknowledged" if action == "acknowledge" else "manager.insight_dismissed",
            entity_type="insight",
            entity_id=key,
            actor_id=request.user.pk,
            properties={"action": action},
            suffix=f"{action}:{timezone.now():%Y-%m-%d}",
        )
        self.audit(request, "MANAGER_INSIGHT_REVIEWED", "INSIGHT", key, {"action": action})
        return Response({"recorded": True})
