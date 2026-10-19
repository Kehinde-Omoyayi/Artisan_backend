from django.urls import path

from .views import CustomerCancellationRiskView

urlpatterns = [
    path("customer/cancellation-risk/", CustomerCancellationRiskView.as_view(), name="customer-cancellation-risk"),
]
