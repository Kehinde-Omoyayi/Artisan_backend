from django.contrib import admin

from .models import Conversation, ConversationAttachment, ConversationReadState, Message, Offer


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ("id", "conversation_type", "customer", "artisan", "service_request", "updated_at")
    list_filter = ("conversation_type", "updated_at")
    search_fields = ("id", "customer__email", "artisan__business_name", "artisan__user__email")
    filter_horizontal = ("participants",)
    readonly_fields = ("id", "direct_key", "created_at", "updated_at")


@admin.register(ConversationReadState)
class ConversationReadStateAdmin(admin.ModelAdmin):
    list_display = ("conversation", "user", "last_read_at", "updated_at")
    search_fields = ("conversation__id", "user__email")


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ("id", "conversation", "sender", "message_type", "event_code", "created_at")
    list_filter = ("message_type", "event_code")
    search_fields = ("id", "conversation__id", "sender__email", "body")
    readonly_fields = ("id", "created_at")


@admin.register(ConversationAttachment)
class ConversationAttachmentAdmin(admin.ModelAdmin):
    list_display = ("id", "message", "content_type", "size", "scan_status", "uploaded_at")
    list_filter = ("scan_status", "content_type")
    search_fields = ("id", "message__conversation__id")
    readonly_fields = ("id", "uploaded_at")


@admin.register(Offer)
class OfferAdmin(admin.ModelAdmin):
    list_display = ("id", "conversation", "created_by", "total_amount", "status", "expires_at", "created_at")
    list_filter = ("status", "materials_included")
    search_fields = ("id", "conversation__id", "created_by__email", "scope")
    readonly_fields = ("id", "created_at", "updated_at")
