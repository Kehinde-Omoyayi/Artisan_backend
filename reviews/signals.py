from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import Review
from .services import recalculate_artisan_rating

@receiver(post_save, sender=Review)
def review_saved(sender, instance, **kwargs):
    recalculate_artisan_rating(instance.artisan_id)

@receiver(post_delete, sender=Review)
def review_deleted(sender, instance, **kwargs):
    recalculate_artisan_rating(instance.artisan_id)
