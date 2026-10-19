"""Additive authorization-code endpoints for native clients and API-consuming browser frontends."""

from django.urls import path

from .api_views import (
    NativeSocialCompleteProfileView,
    NativeSocialCompleteView,
    NativeSocialStartView,
    WebSocialCompleteView,
    WebSocialStartView,
)

urlpatterns = [
    path("<str:provider>/native/start/", NativeSocialStartView.as_view(), name="social-native-start"),
    path("<str:provider>/native/complete/", NativeSocialCompleteView.as_view(), name="social-native-complete"),
    path("native/complete-profile/", NativeSocialCompleteProfileView.as_view(), name="social-native-complete-profile"),
    # The shorter alias is shared by browser frontends; keep the original native path stable.
    path("complete-profile/", NativeSocialCompleteProfileView.as_view(), name="social-complete-profile"),
    path("<str:provider>/web/start/", WebSocialStartView.as_view(), name="social-web-api-start"),
    path("<str:provider>/web/complete/", WebSocialCompleteView.as_view(), name="social-web-api-complete"),
]
