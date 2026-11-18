from django.urls import path

from manager import views

app_name = "manager"

urlpatterns = [
    # Session and second factor
    path("session/", views.ManagerSessionView.as_view(), name="session"),
    path("auth/totp/enroll/", views.TotpEnrollView.as_view(), name="totp-enroll"),
    path("auth/totp/confirm/", views.TotpConfirmView.as_view(), name="totp-confirm"),
    path("auth/step-up/", views.StepUpView.as_view(), name="step-up"),
    path("auth/step-up/challenge/", views.StepUpChallengeView.as_view(), name="step-up-challenge"),
    path("sessions/<uuid:session_id>/revoke/", views.ManagerSessionRevokeView.as_view(), name="session-revoke"),

    # Analytics departments
    # Note: /api/v1/manager/overview/ is the legacy dashboard contract served by
    # home.manager_views.ManagerOverviewView, so the console overview lives here.
    path("analytics/overview/", views.AnalyticsView.as_view(section="overview"), name="overview"),
    path("financial/", views.AnalyticsView.as_view(section="financial"), name="financial"),
    path("business/", views.AnalyticsView.as_view(section="business"), name="business"),
    path("services/", views.AnalyticsView.as_view(section="services"), name="services"),
    path("customers/", views.AnalyticsView.as_view(section="customers"), name="customers"),
    path("artisans/", views.AnalyticsView.as_view(section="artisans"), name="artisans"),
    path("funnel/", views.AnalyticsView.as_view(section="funnel"), name="funnel"),
    path("marketing/", views.AnalyticsView.as_view(section="marketing"), name="marketing"),
    path("geography/", views.AnalyticsView.as_view(section="geography"), name="geography"),
    path("insights/", views.AnalyticsView.as_view(section="insights"), name="insights"),
    path("data-quality/", views.AnalyticsView.as_view(section="data_quality"), name="data-quality"),
    path("support/", views.AnalyticsView.as_view(section="support"), name="support"),
    path("risk/", views.AnalyticsView.as_view(section="risk"), name="risk"),
    path("health/", views.HealthView.as_view(), name="health"),
    path("audit/", views.AuditView.as_view(), name="audit"),

    # Configuration
    path("config/", views.ConfigIndexView.as_view(), name="config"),
    path("config/<str:key>/preview/", views.ConfigPreviewView.as_view(), name="config-preview"),
    path("config/<str:key>/change/", views.ConfigChangeView.as_view(), name="config-change"),
    path("config/<str:key>/history/", views.ConfigHistoryView.as_view(), name="config-history"),
    path("config/<str:key>/rollback/", views.ConfigRollbackView.as_view(), name="config-rollback"),
    path("config-changes/<uuid:change_id>/approve/", views.ConfigApproveView.as_view(), name="config-approve"),
    path("config-changes/<uuid:change_id>/reject/", views.ConfigRejectView.as_view(), name="config-reject"),

    # Snapshots
    path("snapshots/", views.SnapshotStatusView.as_view(), name="snapshots"),
    path("snapshots/interval/", views.SnapshotIntervalView.as_view(), name="snapshot-interval"),
    path("snapshots/capture/", views.SnapshotCaptureView.as_view(), name="snapshot-capture"),
    path("snapshots/<str:metric_key>/history/", views.SnapshotHistoryView.as_view(), name="snapshot-history"),

    # Data explorer, reports, exports
    path("explorer/catalogue/", views.ExplorerCatalogueView.as_view(), name="explorer-catalogue"),
    path("explorer/run/", views.ExplorerRunView.as_view(), name="explorer-run"),
    path("reports/", views.SavedReportListView.as_view(), name="reports"),
    path("reports/<uuid:report_id>/", views.SavedReportDetailView.as_view(), name="report-detail"),
    path("exports/", views.ExportRequestView.as_view(), name="exports"),
    path("exports/<uuid:job_id>/download/", views.ExportDownloadView.as_view(), name="export-download"),

    # Jobs
    path("jobs/", views.JobListView.as_view(), name="jobs"),
    path("jobs/<uuid:job_id>/", views.JobDetailView.as_view(), name="job-detail"),
    path("jobs/<uuid:job_id>/cancel/", views.JobCancelView.as_view(), name="job-cancel"),
    path("reconciliation/run/", views.ReconciliationRunView.as_view(), name="reconciliation-run"),

    # Campaigns
    path("campaigns/", views.CampaignListView.as_view(), name="campaigns"),
    path("campaigns/estimate/", views.CampaignEstimateView.as_view(), name="campaign-estimate"),
    path("campaigns/<uuid:campaign_id>/", views.CampaignDetailView.as_view(), name="campaign-detail"),
    path("campaigns/<uuid:campaign_id>/approve/", views.CampaignApproveView.as_view(), name="campaign-approve"),
    path("campaigns/<uuid:campaign_id>/dispatch/", views.CampaignDispatchView.as_view(), name="campaign-dispatch"),
    path("campaigns/<uuid:campaign_id>/cancel/", views.CampaignCancelView.as_view(), name="campaign-cancel"),
    path("campaigns/opt-out/", views.CampaignOptOutView.as_view(), name="campaign-opt-out"),

    # Staff
    path("staff/", views.StaffListView.as_view(), name="staff"),
    path("staff/roles/", views.RoleListView.as_view(), name="roles"),
    path("staff/roles/<slug:slug>/permissions/", views.RolePermissionsView.as_view(), name="role-permissions"),
    path("staff/<uuid:profile_id>/role/", views.StaffRoleAssignView.as_view(), name="staff-role"),
    path("staff/<uuid:profile_id>/suspend/", views.StaffSuspendView.as_view(), name="staff-suspend"),

    # Privacy and risk
    path("privacy/", views.PrivacyView.as_view(), name="privacy"),
    path("privacy/retention/<str:data_class>/", views.RetentionUpdateView.as_view(), name="retention-update"),
    path("privacy/requests/", views.DataSubjectRequestView.as_view(), name="privacy-requests"),
    path("privacy/requests/<str:reference>/", views.DataSubjectRequestDetailView.as_view(), name="privacy-request-detail"),
    path("risk/reviews/", views.RiskReviewListView.as_view(), name="risk-reviews"),
    path("risk/reviews/<uuid:review_id>/", views.RiskReviewDecisionView.as_view(), name="risk-review-decision"),
    path("insights/action/", views.InsightActionView.as_view(), name="insight-action"),
]
