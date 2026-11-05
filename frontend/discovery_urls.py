from django.urls import path
from . import views

urlpatterns = [
    path(
        "categories/<path:category_id>/chat/",
        views.start_category_conversation,
        name="start-category-conversation",
    ),
    path(
        "conversations/<uuid:conversation_id>/media/<uuid:attachment_id>/",
        views.conversation_media_proxy,
        name="conversation-media-proxy",
    ),
    path("categories/", views.categories, name="categories"),
    path("services/", views.services, name="services"),
    path("artisans/", views.artisans, name="artisans"),
    path("artisans/<uuid:pk>/", views.artisan_detail, name="artisan-detail"),
    path(
        "artisans/<uuid:pk>/request/",
        views.start_conversation,
        name="start-conversation",
    ),
    path("conversations/", views.conversation_inbox, name="conversation-inbox"),
    path(
        "conversations/<uuid:conversation_id>/",
        views.conversation_detail,
        name="conversation-detail",
    ),
    path(
        "conversations/<uuid:conversation_id>/messages/",
        views.conversation_messages,
        name="conversation-messages",
    ),
    path("locations/", views.locations, name="locations"),
    path("locations/<uuid:pk>/", views.location_detail, name="location-detail"),
]
