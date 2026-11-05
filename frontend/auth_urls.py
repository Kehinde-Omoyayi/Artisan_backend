from django.urls import path
from . import views

urlpatterns = [
    path('login/', views.login, name='login'),
    path('register/', views.register, name='register'),
    path('verify-otp/', views.verify_otp, name='verify-otp'),
    path('request-otp/', views.request_otp, name='request-otp'),
    path('forgot-password/', views.forgot_password, name='forgot-password'),
    path('verify-reset-otp/', views.verify_reset_otp, name='verify-reset-otp'),
    path('reset-password/', views.reset_password, name='reset-password'),
    path('staff-mfa/', views.staff_mfa, name='staff-mfa'),
    path('staff-security/setup/', views.staff_security_setup, name='staff-security-setup'),
    path('staff-password-reset/', views.staff_password_reset, name='staff-password-reset'),
    path('staff-password-reset/complete/', views.staff_password_reset_complete, name='staff-password-reset-complete'),
    path('staff-password-change/', views.staff_password_change, name='staff-password-change'),
    path('staff-security-questions/', views.staff_security_questions, name='staff-security-questions'),
    path('logout/', views.logout, name='logout'),
]
