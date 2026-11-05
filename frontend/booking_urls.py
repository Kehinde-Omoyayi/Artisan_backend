from django.urls import path
from . import views

urlpatterns = [
    path('', views.bookings, name='bookings'),
    path('<uuid:pk>/', views.booking_detail, name='booking-detail'),
    path('<uuid:pk>/accept/', views.booking_accept, name='booking-accept'),
    path('<uuid:pk>/reject/', views.booking_reject, name='booking-reject'),
    path('<uuid:pk>/cancel/', views.booking_cancel, name='booking-cancel'),
    path('<uuid:pk>/start/', views.booking_start, name='booking-start'),
    path('<uuid:pk>/complete/', views.booking_complete, name='booking-complete'),
    path('<uuid:pk>/confirm-completion/', views.booking_confirm_completion, name='booking-confirm'),
    path('<uuid:pk>/dispute/', views.booking_dispute, name='booking-dispute'),
    path('<uuid:pk>/activity/', views.booking_activity, name='booking-activity'),
    path('<uuid:pk>/history/', views.booking_history, name='booking-history'),
]
