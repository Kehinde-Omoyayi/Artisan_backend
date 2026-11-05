from django.urls import path

from . import views

urlpatterns = [
    path('', views.notifications_page, name='notifications-page'),
    path('popups/', views.notification_popups, name='notification-popups'),
    path('<int:pk>/acknowledge/', views.notification_acknowledge, name='notification-acknowledge'),
]
