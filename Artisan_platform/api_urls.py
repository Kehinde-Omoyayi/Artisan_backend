from django.conf import settings
from django.contrib import admin
from django.urls import include, path
from django.conf.urls.static import static
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView, SpectacularSwaggerView

from home.admin_mfa_views import admin_staff_mfa, admin_staff_password_change

urlpatterns = [
    # Keep the protected Django admin and its staff security gates available.
    path("admin/mfa/", admin_staff_mfa, name="admin-staff-mfa"),
    path("admin/password_change/", admin_staff_password_change, name="admin-password-change"),
    path("admin/", admin.site.urls),
    # Versioned marketplace API.
    path("api/v1/", include("home.urls")),
    path("api/v1/services/", include("services.urls")),
    path("api/v1/booking/", include("booking.urls")),
    path("api/v1/payments/", include("payments.urls")),
    path("api/v1/reviews/", include("reviews.urls")),
    path("api/v1/support/cases/", include("support.urls")),
    path("api/v1/promotions/", include("promotions.urls")),
    path("api/v1/risk/", include("risk.urls")),
    path("api/v1/conversations/", include("conversations.urls")),
    path("api/v1/notifications/", include("notifications.urls")),
    path("api/v1/profile/", include("profiles.urls")),
    path("api/v1/social-auth/", include("social_auth.api_urls")),
    # Operational probes remain outside the versioned API.
    path("health/", include("health.urls")),
] + static(settings.STATIC_URL, document_root=settings.STATIC_ROOT) + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

if settings.PUBLIC_API_DOCS:
    urlpatterns += [
        path("api/v1/schema/", SpectacularAPIView.as_view(), name="schema"),
        path("api/v1/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
        path("api/v1/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
    ]
