from rest_framework import generics, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers
from .serializers import NotificationSerializer
from .in_app import mark_all_notifications_read, mark_notification_read
from .models import InAppNotification

class NotificationListView(generics.ListAPIView):
    permission_classes = [IsAuthenticated]
    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return InAppNotification.objects.none()
        queryset = InAppNotification.objects.filter(recipient=self.request.user)
        if str(self.request.query_params.get("unread", "")).lower() in {"1", "true", "yes"}: queryset = queryset.filter(read_at__isnull=True)
        return queryset.only("id", "event_key", "title", "message", "data", "read_at", "created_at")
    @extend_schema(tags=["Notifications"], responses={200: NotificationSerializer(many=True)})
    def get(self, request, *args, **kwargs): return super().get(request, *args, **kwargs)
    serializer_class = NotificationSerializer

class NotificationReadView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "notification_read"
    @extend_schema(
        tags=["Notifications"],
        request=None,
        responses={
            200: inline_serializer(
                name="NotificationReadResponse",
                fields={"status": serializers.CharField()},
            ),
            404: OpenApiResponse(description="Notification not found or already read."),
        },
    )
    def post(self, request, pk):
        updated = mark_notification_read(notification_id=pk, user=request.user)
        if not updated: return Response({"detail": "Notification not found or already read."}, status=status.HTTP_404_NOT_FOUND)
        return Response({"status": "read"})


class NotificationUnreadCountView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "notification_read"

    @extend_schema(tags=["Notifications"], responses={200: OpenApiResponse(description="Number of unread notifications for the authenticated user.")})
    def get(self, request):
        return Response({"unread_count": InAppNotification.objects.filter(recipient=request.user, read_at__isnull=True).count()})


class NotificationReadAllView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "notification_read"

    @extend_schema(tags=["Notifications"], request=None, responses={200: OpenApiResponse(description="All unread notifications of the authenticated user marked as read.")})
    def post(self, request):
        updated = mark_all_notifications_read(user=request.user)
        return Response({"status": "read", "updated": updated})


class NotificationDeleteView(APIView):
    """Delete only the authenticated user's notification; foreign IDs are indistinguishable from missing IDs."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "notification_read"

    @extend_schema(
        tags=["Notifications"],
        request=None,
        responses={
            204: OpenApiResponse(description="Owned notification deleted."),
            404: OpenApiResponse(description="Notification not found."),
        },
    )
    def delete(self, request, pk):
        deleted, _ = InAppNotification.objects.filter(
            pk=pk,
            recipient=request.user,
        ).delete()
        if not deleted:
            return Response({"detail": "Notification not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(status=status.HTTP_204_NO_CONTENT)


class NotificationDeleteAllView(APIView):
    """Bulk-delete the authenticated user's inbox without touching other recipients."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "notification_read"

    @extend_schema(
        tags=["Notifications"],
        request=None,
        responses={200: OpenApiResponse(description="Owned notifications deleted, with the number of rows removed.")},
    )
    def delete(self, request):
        deleted, _ = InAppNotification.objects.filter(recipient=request.user).delete()
        return Response({"status": "deleted", "deleted": deleted})
