from django.urls import path
from . import views

urlpatterns = [
    path('customer/', views.customer_profile, name='customer-profile'),
    path('customer/bookings/', views.customer_bookings, name='customer-bookings'),
    path('artisan/', views.artisan_profile, name='artisan-profile'),
    path('artisan/requests/', views.artisan_requests, name='artisan-requests'),
    path('artisan/promotions/', views.promotion_applications, name='promotion-applications'),
    path('artisan/promotions/me/', views.my_promotions, name='my-promotions'),
    path('artisan/promotions/<uuid:pk>/checkout/', views.promotion_checkout, name='promotion-checkout'),
    path('artisan/promotions/payment/<uuid:pk>/continue/', views.promotion_payment_redirect, name='promotion-payment-redirect'),
    path('artisan/promotions/evidence/<uuid:application_id>/<uuid:evidence_id>/', views.promotion_evidence_proxy, name='promotion-evidence-proxy'),
    path('artisan/verification/', views.verification, name='verification'),
    path('artisan/verification/nin/', views.nin_verification, name='nin-verification'),
    path('artisan/verification/bvn/', views.bvn_initiate, name='bvn-initiate'),
    path('artisan/verification/bvn/method/', views.bvn_method, name='bvn-method'),
    path('artisan/verification/bvn/details/', views.bvn_details, name='bvn-details'),
    path('artisan/services/', views.artisan_services, name='artisan-services'),
    path('artisan/services/<int:pk>/', views.artisan_service_detail, name='artisan-service-detail'),
    path('artisan/activate/<uuid:pk>/', views.artisan_activate, name='artisan-activate'),
    path('risk/cancellation/', views.cancellation_risk, name='cancellation-risk'),
]
