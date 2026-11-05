"""Manager Console routes.

Every page on this blueprint is rendered by Django on the server; the browser
never calls ``/api/v1/...`` directly. The console pages talk to the manager API
through :class:`frontend.api.APIClient`, which forwards the signed-in user's
token, so the API remains the only place where permissions, prices, fees and
transaction state are decided.
"""
from django.urls import path

from . import console_views as views

urlpatterns = [
    path("", views.overview, name="console-overview"),

    # Analytics departments
    path("financial/", views.financial, name="console-financial"),
    path("business/", views.business, name="console-business"),
    path("services/", views.services, name="console-services"),
    path("customers/", views.customers, name="console-customers"),
    path("artisans/", views.artisans, name="console-artisans"),
    path("funnel/", views.funnel, name="console-funnel"),
    path("marketing/", views.marketing, name="console-marketing"),
    path("geography/", views.geography, name="console-geography"),
    path("insights/", views.insights, name="console-insights"),
    path("data-quality/", views.data_quality, name="console-data-quality"),
    path("support/", views.support_intelligence, name="console-support"),
    path("risk/", views.risk, name="console-risk"),

    # Reporting
    path("reports/", views.reports, name="console-reports"),
    path("reports/save/", views.report_save, name="console-report-save"),
    path("reports/<uuid:report_id>/run/", views.report_run, name="console-report-run"),
    path("reports/<uuid:report_id>/delete/", views.report_delete, name="console-report-delete"),
    path("exports/", views.export_request, name="console-export"),
    path("exports/<uuid:job_id>/download/", views.export_download, name="console-export-download"),

    # Campaign board
    path("campaigns/", views.campaigns, name="console-campaigns"),
    path("campaigns/create/", views.campaign_create, name="console-campaign-create"),
    path("campaigns/estimate/", views.campaign_estimate, name="console-campaign-estimate"),
    path("campaigns/<uuid:campaign_id>/approve/", views.campaign_approve, name="console-campaign-approve"),
    path("campaigns/<uuid:campaign_id>/dispatch/", views.campaign_dispatch, name="console-campaign-dispatch"),
    path("campaigns/<uuid:campaign_id>/cancel/", views.campaign_cancel, name="console-campaign-cancel"),

    # People and permissions
    path("staff/", views.staff, name="console-staff"),
    path("staff/<uuid:profile_id>/role/", views.staff_role, name="console-staff-role"),
    path("staff/<uuid:profile_id>/suspend/", views.staff_suspend, name="console-staff-suspend"),
    path("roles/<slug:slug>/permissions/", views.role_permissions, name="console-role-permissions"),

    # Configuration centre
    path("config/", views.config_centre, name="console-config"),
    path("config/preview/", views.config_preview, name="console-config-preview"),
    path("config/change/", views.config_change, name="console-config-change"),
    path("config/rollback/", views.config_rollback, name="console-config-rollback"),
    path("config/changes/<uuid:change_id>/approve/", views.config_approve, name="console-config-approve"),
    path("config/changes/<uuid:change_id>/reject/", views.config_reject, name="console-config-reject"),

    # Snapshots, jobs, health, audit
    path("snapshots/interval/", views.snapshot_interval, name="console-snapshot-interval"),
    path("snapshots/capture/", views.snapshot_capture, name="console-snapshot-capture"),
    path("jobs/", views.jobs, name="console-jobs"),
    path("jobs/<uuid:job_id>/cancel/", views.job_cancel, name="console-job-cancel"),
    path("system-health/", views.system_health, name="console-system-health"),
    path("audit/", views.audit, name="console-audit"),

    # Privacy, risk and security self-service
    path("privacy/", views.privacy, name="console-privacy"),
    path("privacy/retention/", views.privacy_retention, name="console-privacy-retention"),
    path("privacy/requests/", views.privacy_request, name="console-privacy-request"),
    path("privacy/requests/<str:reference>/complete/", views.privacy_request_complete, name="console-privacy-request-complete"),
    path("risk/reviews/<uuid:review_id>/", views.risk_decision, name="console-risk-decision"),
    path("insights/action/", views.insight_action, name="console-insight-action"),
    path("step-up/", views.step_up, name="console-step-up"),
    path("sessions/<uuid:session_id>/revoke/", views.session_revoke, name="console-session-revoke"),
]
