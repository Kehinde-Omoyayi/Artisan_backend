from django.urls import path
from .views import ReviewCreateView

urlpatterns = [
    path("", ReviewCreateView.as_view(), name="review-create"),
]
