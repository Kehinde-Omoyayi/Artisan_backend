from django.urls import path

from . import social_views

urlpatterns = [
    path("<str:provider>/start/", social_views.social_auth_start, name="social-api-start"),
    path("<str:provider>/callback/", social_views.social_auth_callback, name="social-api-callback"),
    path("profile/", social_views.social_auth_profile, name="social-api-profile"),
]
