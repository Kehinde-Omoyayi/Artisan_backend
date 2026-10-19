from rest_framework import serializers
from .models import InAppNotification
class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = InAppNotification
        fields = ["id", "event_key", "title", "message", "data", "read_at", "created_at"]
        read_only_fields = fields
