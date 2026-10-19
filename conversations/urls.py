from django.urls import path

from .views import (
    ConversationCreateView,
    ConversationDirectArtisanView,
    ConversationDetailView,
    ConversationDisputeView,
    ConversationListView,
    ConversationOfferAcceptView,
    ConversationOfferDeclineView,
    ConversationOfferListView,
    ConversationOfferCounterView,
    ConversationPaymentInitializeView,
    ConversationReadView,
    ConversationRequestView,
    ConversationStartView,
    ConversationMessageListCreateView,
)

urlpatterns = [
    path("", ConversationCreateView.as_view(), name="conversation-list"),
    path("start/", ConversationStartView.as_view(), name="conversation-start"),
    path("direct/artisan/<uuid:artisan_id>/", ConversationDirectArtisanView.as_view(), name="conversation-direct-artisan"),
    path("inbox/", ConversationListView.as_view(), name="conversation-inbox"),
    path("<uuid:conversation_id>/", ConversationDetailView.as_view(), name="conversation-detail"),
    path("<uuid:conversation_id>/messages/", ConversationMessageListCreateView.as_view(), name="conversation-messages"),
    path("<uuid:conversation_id>/request/", ConversationRequestView.as_view(), name="conversation-request"),
    path("<uuid:conversation_id>/offers/", ConversationOfferListView.as_view(), name="conversation-offers"),
    path("<uuid:conversation_id>/offers/counter/", ConversationOfferCounterView.as_view(), name="conversation-offer-counter"),
    path("<uuid:conversation_id>/offers/<uuid:offer_id>/accept/", ConversationOfferAcceptView.as_view(), name="conversation-offer-accept"),
    path("<uuid:conversation_id>/offers/<uuid:offer_id>/decline/", ConversationOfferDeclineView.as_view(), name="conversation-offer-decline"),
    path("<uuid:conversation_id>/read/", ConversationReadView.as_view(), name="conversation-read"),
    path("<uuid:conversation_id>/payment/initialize/", ConversationPaymentInitializeView.as_view(), name="conversation-payment-initialize"),
    path("<uuid:conversation_id>/dispute/", ConversationDisputeView.as_view(), name="conversation-dispute"),
]
