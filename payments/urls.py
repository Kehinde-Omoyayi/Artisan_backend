from django.urls import path

from .views import (
    ArtisanPayoutAccountStatusView,
    ArtisanPayoutAccountView,
    PayoutBankListView,
    ArtisanEarningsSummaryView,
    FlutterwaveCallbackView,
    InitializeBookingPaymentView,
    InitializeAdditionalOrderPaymentView,
    PaymentDetailView,
    flutterwave_webhook,
)

urlpatterns = [
    path("artisan/earnings/summary/", ArtisanEarningsSummaryView.as_view(), name="artisan-earnings-summary"),
    path("booking/<uuid:booking_id>/initialize/", InitializeBookingPaymentView.as_view(), name="initialize-booking-payment"),
    path("additional-orders/<uuid:order_id>/initialize/", InitializeAdditionalOrderPaymentView.as_view(), name="initialize-additional-order-payment"),
    path("<uuid:payment_id>/", PaymentDetailView.as_view(), name="payment-detail"),
    path("payout-account/banks/", PayoutBankListView.as_view(), name="payout-account-banks"),
    path("payout-account/", ArtisanPayoutAccountView.as_view(), name="artisan-payout-account"),
    path("payout-account/<uuid:pk>/verify/", ArtisanPayoutAccountStatusView.as_view(), name="artisan-payout-account-verify"),
    path("flutterwave/callback/", FlutterwaveCallbackView.as_view(), name="flutterwave-callback"),
    path("flutterwave/webhook/", flutterwave_webhook, name="flutterwave-webhook"),
]
