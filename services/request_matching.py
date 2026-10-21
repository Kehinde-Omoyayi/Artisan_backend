"""One eligibility check for the artisan board and its personal hide action."""
from django.db.models import Exists, OuterRef, Q
from home.models import ArtisanService
from .models import ArtisanRequestDismissal, ServiceRequest


def matched_requests_for(artisan, *, include_dismissed=False):
    offerings = ArtisanService.objects.filter(
        artisan=artisan, artisan__profile_status='ACTIVE',
        artisan__verification__verification_status='VERIFIED',
        artisan__user__is_active=True, service__is_active=True,
        service__category_id=OuterRef('category_id'),
    ).filter(Q(location_id=OuterRef('location_id')) | Q(location__isnull=True))
    queryset = ServiceRequest.objects.filter(
        Exists(offerings), status=ServiceRequest.Status.OPEN,
    ).select_related('requester', 'service', 'category', 'location')
    if not include_dismissed:
        hidden = ArtisanRequestDismissal.objects.filter(artisan=artisan, request_id=OuterRef('pk'))
        queryset = queryset.filter(~Exists(hidden))
    # Age is deliberately NOT an eligibility condition. Requests do not expire
    # after 48 hours, and a preferred date passing does not auto-hide a request.
    return queryset
