from django.urls import path

from .views import (
    ArtisanActivationView,
    ArtisanServiceDetailView,
    ArtisanServiceListCreateView,
    ArtisanMatchedServiceRequestListView,
    ArtisanRequestDismissView,
    ArtisanVerificationStatusView,
    BVNDetailsView,
    BVNInitiateView,
    BVNMethodView,
    LocationDetailView,
    LocationListCreateView,
    InstitutionListView,
    NINVerificationView,
    ServiceCategoryListView,
    ServiceListView,
    ServiceRequestDetailView,
    ServiceRequestListCreateView,
)

urlpatterns = [
    path("artisan/profile/<uuid:pk>/activate/", ArtisanActivationView.as_view(), name="artisan-activation"),
    path("verification/", ArtisanVerificationStatusView.as_view(), name="artisan-verification-status"),
    path("verification/nin/", NINVerificationView.as_view(), name="artisan-nin-verification"),
    path("verification/bvn/initiate/", BVNInitiateView.as_view(), name="artisan-bvn-initiate"),
    path("verification/bvn/method/", BVNMethodView.as_view(), name="artisan-bvn-method"),
    path("verification/bvn/details/", BVNDetailsView.as_view(), name="artisan-bvn-details"),

    path("get_artisan_services/", ArtisanServiceListCreateView.as_view(), name="artisan-service-list-create"),
    path("get_artisan_services/<int:pk>/", ArtisanServiceDetailView.as_view(), name="artisan-service-detail"),
    path("artisan/requests/<int:pk>/dismiss/", ArtisanRequestDismissView.as_view(), name="artisan-request-dismiss"),
    path("artisan/requests/", ArtisanMatchedServiceRequestListView.as_view(), name="artisan-matched-service-requests"),

    path("categories/", ServiceCategoryListView.as_view(), name="service-category-list"),
    path("service_cat/", ServiceListView.as_view(), name="service-list"),
    
    path("requests/", ServiceRequestListCreateView.as_view(), name="service-request-list-create"),
    path("requests/<int:pk>/", ServiceRequestDetailView.as_view(), name="service-request-detail"),
    
    path("institutions/", InstitutionListView.as_view(), name="institution-list"),
    path("locations/", LocationListCreateView.as_view(), name="location-list-create"),
    path("locations/<uuid:pk>/", LocationDetailView.as_view(), name="location-detail"),
]
