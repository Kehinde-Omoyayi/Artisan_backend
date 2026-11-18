from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

class Review(models.Model):
    booking = models.OneToOneField("booking.Booking", on_delete=models.CASCADE, related_name="review")
    customer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="reviews_given")
    artisan = models.ForeignKey("home.ArtisanProfile", on_delete=models.CASCADE, related_name="reviews_received")
    rating = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    comment = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.artisan} • {self.rating}/5 • Booking {self.booking_id}"


class PlatformFeedback(models.Model):
    """One thing the user thinks we could do better.

    Deliberately separate from Review: that one is about the artisan's work,
    this one is about the platform and is internal. Asking both in one box would
    poison the ratings with complaints about us.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="platform_feedback",
    )
    note = models.TextField()
    page = models.CharField(max_length=80, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "platform feedback"

    def __str__(self):
        return f"Feedback from {self.user_id}"
