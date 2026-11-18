from django.urls import path

from .views import (
    ArtisanDiscoveryDetailView,
    ArtisanDiscoveryListView,
    ArtisanProfessionalLinksReviewView,
    ArtisanProfileView,
    CustomerProfileView,
)

urlpatterns = [
    path("artisan/personal/profile/", ArtisanProfileView.as_view(), name="artisan-profile"),

    path("internal/artisans/<uuid:pk>/professional-links/", ArtisanProfessionalLinksReviewView.as_view(), name="artisan-professional-links-review"),
    path("artisan/details/<uuid:pk>/", ArtisanDiscoveryDetailView.as_view(), name="artisan-discovery-detail"),
    path("artisan/details/", ArtisanDiscoveryListView.as_view(), name="artisan-discovery-list"),

    path("customer/personal/profile/", CustomerProfileView.as_view(), name="customer-profile"),

]
