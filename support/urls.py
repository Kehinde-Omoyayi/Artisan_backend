from django.urls import path

from .views import (
    SupportCapabilitiesView,
    SupportCaseEligiblePaymentsView,
    SupportCaseAssignView,
    SupportCaseClaimView,
    SupportCaseCloseView,
    SupportCaseDetailView,
    SupportCaseEventsView,
    SupportCaseListCreateView,
    SupportCaseMessageView,
    SupportCaseNoteView,
    SupportCaseResolveView,
    SupportRefundApproveView,
    SupportRefundDetailView,
    SupportRefundExecuteView,
    SupportRefundReconcileView,
    SupportRefundRejectView,
    SupportRefundListView,
    SupportRefundRequestView,
    SupportStaffListView,
    SupportStaffRoleView,
)


urlpatterns = [
    # Operator context and support cases
    path("capabilities/", SupportCapabilitiesView.as_view(), name="support-capabilities"),
    path("", SupportCaseListCreateView.as_view(), name="support-case-list-create"),
    path("staff/", SupportStaffListView.as_view(), name="support-staff-list"),
    path("staff/<int:user_id>/roles/", SupportStaffRoleView.as_view(), name="support-staff-roles"),

    path("<int:pk>/eligible-payments/", SupportCaseEligiblePaymentsView.as_view(), name="support-case-eligible-payments"),
    path("<int:pk>/", SupportCaseDetailView.as_view(), name="support-case-detail"),
    path("<int:pk>/events/", SupportCaseEventsView.as_view(), name="support-case-events"),
    path("<int:pk>/messages/", SupportCaseMessageView.as_view(), name="support-case-message"),
    path("<int:pk>/claim/", SupportCaseClaimView.as_view(), name="support-case-claim"),
    path("<int:pk>/assign/", SupportCaseAssignView.as_view(), name="support-case-assign"),
    path("<int:pk>/notes/", SupportCaseNoteView.as_view(), name="support-case-note"),
    path("<int:pk>/resolve/", SupportCaseResolveView.as_view(), name="support-case-resolve"),
    path("<int:pk>/close/", SupportCaseCloseView.as_view(), name="support-case-close"),

    # Refunds
    path("refunds/", SupportRefundListView.as_view(), name="support-refund-list"),
    path("<int:pk>/refunds/", SupportRefundRequestView.as_view(), name="support-refund-request"),
    path("refunds/<uuid:refund_id>/", SupportRefundDetailView.as_view(), name="support-refund-detail"),
    path("refunds/<uuid:refund_id>/approve/", SupportRefundApproveView.as_view(), name="support-refund-approve"),
    path("refunds/<uuid:refund_id>/reject/", SupportRefundRejectView.as_view(), name="support-refund-reject"),
    path("refunds/<uuid:refund_id>/execute/", SupportRefundExecuteView.as_view(), name="support-refund-execute"),
    path("refunds/<uuid:refund_id>/reconcile/", SupportRefundReconcileView.as_view(), name="support-refund-reconcile"),
]