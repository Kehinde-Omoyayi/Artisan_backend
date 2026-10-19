from django.db.models import Avg

from home.models import ArtisanProfile
from .models import Review

def recalculate_artisan_rating(artisan_id):
    average = Review.objects.filter(artisan_id=artisan_id).aggregate(
        average=Avg("rating")
    )["average"]

    ArtisanProfile.objects.filter(pk=artisan_id).update(
        average_rating=round(float(average), 2) if average is not None else 0.00
    )
