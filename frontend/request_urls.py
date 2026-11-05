from django.urls import path
from . import views

urlpatterns = [
    path('', views.service_requests, name='service-requests'),
    path('<int:pk>/responders/', views.service_request_responders, name='service-request-responders'),
    path('<int:pk>/', views.service_request_detail, name='service-request-detail'),
]
