from django.urls import include, path

urlpatterns = [
    path("api/v1/social-auth/", include("social_auth.api_urls")),
    path("auth/social/", include("social_auth.web_urls")),
    path("", include("Artisan_platform.urls")),
]
