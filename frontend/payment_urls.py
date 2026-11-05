from django.urls import path
from . import views

urlpatterns = [
    path('booking/<uuid:booking_id>/initialize/', views.payment_initialize, name='payment-initialize'),
    # Single-tunnel setup: the provider is pointed at this host, and these two
    # routes hand the request to the API over localhost.
    path('callback/', views.payment_provider_return, name='payment-provider-return'),
    path('flutterwave/webhook/', views.payment_webhook_proxy, name='payment-webhook-proxy'),
    path('<uuid:payment_id>/', views.payment_detail, name='payment-detail'),
    path('<uuid:payment_id>/status/', views.payment_status, name='payment-status'),
    path('<uuid:payment_id>/return/', views.payment_return, name='payment-return'),
    path('<uuid:payment_id>/receipt/', views.payment_receipt, name='payment-receipt'),
    path('<uuid:payment_id>/receipt.pdf/', views.payment_receipt_pdf, name='payment-receipt-pdf'),
    path('payout-account/', views.payout_account, name='payout-account'),
    path('payout-account/<uuid:pk>/verify/', views.payout_verify, name='payout-verify'),
]
