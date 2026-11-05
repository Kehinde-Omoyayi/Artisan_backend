from django.urls import include, path
from frontend import views

urlpatterns = [
    path("catalogue/institutions/", views.institution_catalogue, name="institution-catalogue"),
    path("service-worker.js", views.service_worker, name="service-worker"),
    path("", views.home, name="home"),
    path("dashboard/", views.home, name="dashboard"),
    path("acknowledge-payment-notice/", views.acknowledge_payment_notice, name="acknowledge-payment-notice"),
    path("auth/", include("frontend.auth_urls")),
    path("auth/social/", include("frontend.social_urls")),
    path("account/", include("frontend.account_urls")),
    path("discover/", include("frontend.discovery_urls")),
    path("requests/", include("frontend.request_urls")),
    path("bookings/", include("frontend.booking_urls")),
    path("payments/", include("frontend.payment_urls")),
    path("reviews/", include("frontend.review_urls")),
    path("notifications/", include("frontend.notification_urls")),
    path("support/", include("frontend.support_urls")),
    path("system/", include("frontend.system_urls")),
    path("console/", include("frontend.console_urls")),
]
