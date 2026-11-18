from django.conf import settings
from django.db import models

class InAppNotification(models.Model):
    id = models.BigAutoField(primary_key=True)
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="in_app_notifications")
    event_key = models.CharField(max_length=255)
    title = models.CharField(max_length=255)
    message = models.TextField(max_length=5000)
    data = models.JSONField(default=dict, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["recipient", "event_key"], name="inapp_notification_recipient_event_unique")]
        indexes = [models.Index(fields=["recipient", "read_at", "created_at"], name="inapp_notification_inbox_idx")]

    def __str__(self): return f"Notification {self.id} for {self.recipient_id}"
