from django.urls import path

from .views import (
    NotificationDeleteAllView,
    NotificationDeleteView,
    NotificationListView,
    NotificationReadAllView,
    NotificationReadView,
    NotificationUnreadCountView,
)

urlpatterns = [
    path("", NotificationListView.as_view(), name="notification-list"),
    path("unread-count/", NotificationUnreadCountView.as_view(), name="notification-unread-count"),
    path("read-all/", NotificationReadAllView.as_view(), name="notification-read-all"),
    path("delete-all/", NotificationDeleteAllView.as_view(), name="notification-delete-all"),
    path("<int:pk>/read/", NotificationReadView.as_view(), name="notification-read"),
    path("<int:pk>/", NotificationDeleteView.as_view(), name="notification-delete"),
]
