from django.urls import path

from .promotion_management_views import (
    MyPromotionCampaignsView,
    PromotionCampaignActionView,
    PromotionCampaignManagementListView,
    PromotionCheckoutView,
    PromotionInventoryCapacityView,
    PromotionInventoryManagementView,
    PromotionManagementAuditView,
    PromotionOfferView,
    PromotionPaymentStatusView,
    PromotionPlanManagementDetailView,
    PromotionPlansManagementView,
    PublicHomepagePromotionsView,
)
from .placement_views import (
    ArtisanMarketPicksManagementView,
    PromotionDiscoveryView,
    PromotionPerformanceEventView,
    PublicArtisanMarketPicksView,
)
from .views import (
    InternalPromotionApplicationDecisionView,
    InternalPromotionApplicationDetailView,
    InternalPromotionApplicationListView,
    PromotionApplicationDetailView,
    PromotionApplicationEvidenceView,
    PromotionApplicationListCreateView,
    PromotionApplicationWithdrawView,
    PromotionEligibilityView,
)


urlpatterns = [
    path("homepage/", PublicHomepagePromotionsView.as_view(), name="promotion-homepage-public"),
    path("discovery/", PromotionDiscoveryView.as_view(), name="promotion-discovery-public"),
    path("performance-events/", PromotionPerformanceEventView.as_view(), name="promotion-performance-event"),
    path("market-picks/", PublicArtisanMarketPicksView.as_view(), name="promotion-market-picks-public"),
    path("management/market-picks/", ArtisanMarketPicksManagementView.as_view(), name="promotion-market-picks-management"),
    path("applications/<uuid:pk>/offer/", PromotionOfferView.as_view(), name="promotion-application-offer"),
    path("applications/<uuid:pk>/checkout/", PromotionCheckoutView.as_view(), name="promotion-application-checkout"),
    path("campaigns/", MyPromotionCampaignsView.as_view(), name="promotion-my-campaigns"),
    path("payments/<uuid:pk>/", PromotionPaymentStatusView.as_view(), name="promotion-payment-status"),
    path("management/plans/", PromotionPlansManagementView.as_view(), name="promotion-management-plans"),
    path("management/plans/<uuid:pk>/", PromotionPlanManagementDetailView.as_view(), name="promotion-management-plan-detail"),
    path("management/inventories/", PromotionInventoryManagementView.as_view(), name="promotion-management-inventories"),
    path("management/inventories/<uuid:pk>/capacity/", PromotionInventoryCapacityView.as_view(), name="promotion-management-inventory-capacity"),
    path("management/audit-events/", PromotionManagementAuditView.as_view(), name="promotion-management-audit-events"),
    path("management/campaigns/", PromotionCampaignManagementListView.as_view(), name="promotion-management-campaigns"),
    path("review/campaigns/<uuid:pk>/actions/", PromotionCampaignActionView.as_view(), name="promotion-campaign-actions"),
    path("eligibility/", PromotionEligibilityView.as_view(), name="promotion-eligibility"),
    path("applications/", PromotionApplicationListCreateView.as_view(), name="promotion-application-list-create"),
    path("applications/<uuid:pk>/", PromotionApplicationDetailView.as_view(), name="promotion-application-detail"),
    path("applications/<uuid:pk>/withdraw/", PromotionApplicationWithdrawView.as_view(), name="promotion-application-withdraw"),
    path(
        "applications/<uuid:application_id>/evidence/<uuid:evidence_id>/",
        PromotionApplicationEvidenceView.as_view(),
        name="promotion-evidence",
    ),
    path("review/applications/", InternalPromotionApplicationListView.as_view(), name="promotion-review-list"),
    path("review/applications/<uuid:pk>/", InternalPromotionApplicationDetailView.as_view(), name="promotion-review-detail"),
    path(
        "review/applications/<uuid:pk>/decision/",
        InternalPromotionApplicationDecisionView.as_view(),
        name="promotion-review-decision",
    ),
]
