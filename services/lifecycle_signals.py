"""Keep the request reference when a booking takes over the work."""
from django.db.models.signals import post_save
from django.dispatch import receiver


@receiver(post_save, sender="booking.Booking", dispatch_uid="request_attended_on_booking")
def mark_booked_request_attended(sender, instance, raw=False, **kwargs):
    if raw or instance.status not in {
        "ACCEPTED", "IN_PROGRESS", "COMPLETION_REQUESTED", "COMPLETED", "DISPUTED"
    }:
        return
    instance.request.mark_attended()
