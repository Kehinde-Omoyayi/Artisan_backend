from django.db.models import Q, Exists, OuterRef

from home.models import ArtisanService

def get_matching_artisans(service_request):
    from services.models import ArtisanRequestDismissal
    hidden = ArtisanRequestDismissal.objects.filter(request=service_request, artisan_id=OuterRef('artisan_id'))
    return (
        ArtisanService.objects
        .filter(
            service__category_id=service_request.category_id,
            service__is_active=True,
            artisan__profile_status="ACTIVE",
            artisan__verification__verification_status="VERIFIED",
            artisan__user__is_active=True,
        )
        .filter(Q(location_id=service_request.location_id) | Q(location__isnull=True))
        .filter(~Exists(hidden))
        .select_related("artisan__user")
        .order_by("-artisan__average_rating", "-artisan__completion_rate")
        .distinct()
    )
