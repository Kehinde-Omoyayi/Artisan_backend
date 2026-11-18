from django.urls import path

from .web_views import social_web_callback, social_web_profile, social_web_start

urlpatterns = [
    path("<str:provider>/start/", social_web_start, name="social-web-start"),
    path("<str:provider>/callback/", social_web_callback, name="social-web-callback"),
    path("profile/", social_web_profile, name="social-auth-profile"),
]
