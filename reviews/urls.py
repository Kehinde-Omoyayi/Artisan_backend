from django.urls import path
from .views import PlatformFeedbackCreateView, ReviewCreateView

urlpatterns = [
    path("platform-feedback/", PlatformFeedbackCreateView.as_view(), name="platform-feedback-create"),
    path("", ReviewCreateView.as_view(), name="review-create"),
]
