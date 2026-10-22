from django.contrib import admin

from .models import (
    Conversation,
    ConversationAttachment,
    ConversationReadState,
    Message,
    Offer,
)


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "conversation_type",
        "customer",
        "artisan",
        "service_request",
        "updated_at",
    )
    list_filter = ("conversation_type", "updated_at")
    search_fields = (
        "id",
        "customer__email",
        "artisan__business_name",
        "artisan__user__email",
    )
    filter_horizontal = ("participants",)

    def has_delete_permission(self, request, obj=None):
        return False

    readonly_fields = ("id", "direct_key", "created_at", "updated_at")


@admin.register(ConversationReadState)
class ConversationReadStateAdmin(admin.ModelAdmin):
    list_display = ("conversation", "user", "last_read_at", "updated_at")
    search_fields = ("conversation__id", "user__email")


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        return tuple(f.name for f in Message._meta.fields)

    list_display = (
        "id",
        "conversation",
        "sender",
        "message_type",
        "event_code",
        "created_at",
    )
    list_filter = ("message_type", "event_code")
    search_fields = ("id", "conversation__id", "sender__email", "body")
    readonly_fields = ("id", "created_at")


@admin.register(ConversationAttachment)
class ConversationAttachmentAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    exclude = ("file",)
    list_display = (
        "id",
        "message",
        "content_type",
        "size",
        "scan_status",
        "uploaded_at",
    )
    list_filter = ("scan_status", "content_type")
    search_fields = ("id", "message__conversation__id")
    readonly_fields = ("id", "uploaded_at")


@admin.register(Offer)
class OfferAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        return tuple(f.name for f in Offer._meta.fields)

    list_display = (
        "id",
        "conversation",
        "created_by",
        "total_amount",
        "status",
        "expires_at",
        "created_at",
    )
    list_filter = ("status", "materials_included")
    search_fields = ("id", "conversation__id", "created_by__email", "scope")
    readonly_fields = ("id", "created_at", "updated_at")


from .models import MediaPurge, MediaPreservationHold, MediaDerivative


@admin.register(MediaPurge)
class MediaPurgeAdmin(admin.ModelAdmin):
    list_display = (
        "service_request",
        "status",
        "attempts",
        "audio_count",
        "video_count",
        "completed_at",
    )
    readonly_fields = tuple(f.name for f in MediaPurge._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(MediaPreservationHold)
class MediaPreservationHoldAdmin(admin.ModelAdmin):
    list_display = ("service_request", "kind", "active", "created_at", "released_at")
    readonly_fields = ("created_at", "released_at", "released_by", "provider_reference")

    def get_readonly_fields(self, request, obj=None):
        return self.readonly_fields + (("service_request", "kind") if obj else ())

    def save_model(self, request, obj, form, change):
        from django.utils import timezone

        if not obj.active:
            obj.released_at = timezone.now()
            obj.released_by = request.user
        else:
            obj.released_at = None
            obj.released_by = None
        super().save_model(request, obj, form, change)
        from core.models import AuditLog

        AuditLog.objects.create(
            actor=request.user,
            action="MEDIA_HOLD_ACTIVE" if obj.active else "MEDIA_HOLD_RELEASED",
            resource_type="ServiceRequest",
            resource_id=str(obj.service_request_id),
            metadata={"hold_id": obj.pk, "kind": obj.kind},
        )

    def has_delete_permission(self, request, obj=None):
        return False
