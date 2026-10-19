from django.urls import path
from .views import *

urlpatterns = [
    path("", BookingListCreateView.as_view(), name="booking-list-create"),
    path("<uuid:booking_id>/additional-orders/", BookingAdditionalOrderCreateView.as_view(), name="booking-additional-order-create"),
    path("<uuid:booking_id>/additional-orders/<uuid:order_id>/offers/<uuid:offer_id>/respond/", BookingAdditionalOrderRespondView.as_view(), name="booking-additional-order-respond"),
    path("<uuid:pk>/", BookingDetailView.as_view(), name="booking-detail"),
    path("<uuid:pk>/accept/", BookingAcceptView.as_view(), name="booking-accept"),
    path("<uuid:pk>/reject/", BookingRejectView.as_view(), name="booking-reject"),
    path("<uuid:pk>/cancel/", BookingCancelView.as_view(), name="booking-cancel"),
    path("<uuid:pk>/start/", BookingStartView.as_view(), name="booking-start"),
    path("<uuid:pk>/complete/", BookingCompleteView.as_view(), name="booking-artisan-complete"),
    path("<uuid:pk>/confirm-completion/", BookingCustomerCompleteView.as_view(), name="booking-customer-complete"),
    path("<uuid:pk>/dispute/", BookingDisputeView.as_view(), name="booking-dispute"),
    path("<uuid:pk>/history/", BookingHistoryView.as_view(), name="booking-history"),
]
