from django.urls import path
from . import views

urlpatterns = [
    path('operations/', views.support_operations_dashboard, name='support-operations'),
    path('promotions/', views.support_promotion_applications, name='support-promotion-applications'),
    path('promotions/campaigns/', views.support_promotion_campaign_management, name='support-promotion-campaign-management'),
    path('promotions/<uuid:pk>/', views.support_promotion_application_detail, name='support-promotion-application-detail'),
    path('promotions/evidence/<uuid:application_id>/<uuid:evidence_id>/', views.support_promotion_evidence_proxy, name='support-promotion-evidence-proxy'),
    path('cases/', views.support_cases, name='support-cases'),
    path('cases/<int:pk>/', views.support_case_detail, name='support-case-detail'),
    path('cases/<int:pk>/events/', views.support_case_events, name='support-case-events'),
    path('cases/<int:pk>/claim/', views.support_case_claim, name='support-case-claim'),
    path('cases/<int:pk>/assign/', views.support_case_assign, name='support-case-assign'),
    path('cases/<int:pk>/notes/', views.support_case_note, name='support-case-note'),
    path('cases/<int:pk>/resolve/', views.support_case_resolve, name='support-case-resolve'),
    path('cases/<int:pk>/close/', views.support_case_close, name='support-case-close'),
    path('cases/refunds/', views.support_refunds, name='support-refunds'),
    path('cases/<int:pk>/refunds/', views.support_refund_request, name='support-refund-request'),
    path('cases/refunds/<uuid:refund_id>/', views.support_refund_detail, name='support-refund-detail'),
    path('cases/refunds/<uuid:refund_id>/approve/', views.support_refund_approve, name='support-refund-approve'),
    path('cases/refunds/<uuid:refund_id>/reject/', views.support_refund_reject, name='support-refund-reject'),
    path('cases/refunds/<uuid:refund_id>/execute/', views.support_refund_execute, name='support-refund-execute'),
    path('cases/refunds/<uuid:refund_id>/reconcile/', views.support_refund_reconcile, name='support-refund-reconcile'),
    path('cases/staff/', views.support_staff, name='support-staff'),
    path('cases/staff/<int:user_id>/roles/', views.support_staff_roles, name='support-staff-roles'),
    path('cases/staff/<int:user_id>/password-reset/', views.support_staff_password_reset, name='support-staff-password-reset'),
    path('cases/staff/<int:user_id>/security-setup/', views.support_staff_security_setup, name='support-staff-security-setup'),
]
