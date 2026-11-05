from django.urls import path
from . import views
urlpatterns = [
    path('manager/reveal-values/', views.reveal_manager_values, name='frontend-manager-values-reveal'),
    path('manager/snapshot-schedule/', views.manager_snapshot_schedule, name='frontend-manager-snapshot-schedule'),
    path('promotions/performance-events/', views.promotion_performance_event, name='frontend-promotion-performance-event'),
    path('health/live/', views.health_live, name='health-live'),
    path('health/ready/', views.health_ready, name='health-ready'),
]
