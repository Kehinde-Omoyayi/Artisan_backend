from django.urls import path

from .views import (
    CustomerCancellationRiskView,
    SupportCustomerRiskAdjustView,
    SupportCustomerRiskDetailView,
    SupportCustomerRiskSearchView,
)

urlpatterns = [
    path(
        "customer/cancellation-risk/",
        CustomerCancellationRiskView.as_view(),
        name="customer-cancellation-risk",
    ),
    # Customer support risk management (Phase 4).
    path(
        "support/customers/",
        SupportCustomerRiskSearchView.as_view(),
        name="support-risk-customer-search",
    ),
    path(
        "support/customers/<uuid:customer_id>/",
        SupportCustomerRiskDetailView.as_view(),
        name="support-risk-customer-detail",
    ),
    path(
        "support/customers/<uuid:customer_id>/adjust/",
        SupportCustomerRiskAdjustView.as_view(),
        name="support-risk-customer-adjust",
    ),
]
