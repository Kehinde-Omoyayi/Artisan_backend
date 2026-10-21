"""Filter an already eligible request queryset; never widen artisan visibility."""
from datetime import date
from decimal import Decimal, InvalidOperation
from uuid import UUID
from django.db.models import Case, When, IntegerField, Q
from rest_framework.exceptions import ValidationError
from home.models import Institution


def filter_matched_requests(queryset, params):
    search = str(params.get('q') or '').strip()[:160]
    if search:
        query = Q()
        for field in ('description', 'category__name', 'service__name', 'location__country', 'location__state', 'location__area', 'location__institutions__name', 'location__institutions__abbreviation'):
            query |= Q(**{field + '__icontains': search})
        queryset = queryset.filter(query).distinct()
    for key, field in (
        ('category_name', 'category__name'), ('service_name', 'service__name'),
        ('location_country', 'location__country'), ('location_state', 'location__state'),
        ('location_area', 'location__area'),
    ):
        value = str(params.get(key) or '').strip()[:160]
        if value:
            queryset = queryset.filter(**{field + '__icontains': value})
    institution_id = str(params.get('institution_id') or '').strip()
    if institution_id:
        try:
            identifier = UUID(institution_id)
        except ValueError:
            raise ValidationError({'institution_id': 'Choose an active institution.'})
        institution = Institution.objects.filter(pk=identifier, is_active=True).select_related('location').first()
        if institution is None:
            raise ValidationError({'institution_id': 'Choose an active institution.'})
        # As in discovery, institution filtering means its saved service locality,
        # not unverified physical proximity or a GPS radius.
        location = institution.location
        queryset = queryset.filter(location__country__iexact=location.country, location__state__iexact=location.state, location__area__iexact=location.area)
    urgency = str(params.get('urgency') or '').strip().upper()
    if urgency:
        if urgency not in {'LOW', 'NORMAL', 'HIGH', 'EMERGENCY'}:
            raise ValidationError({'urgency': 'Choose a valid urgency.'})
        queryset = queryset.filter(urgency=urgency)
    dates = {}
    for key, lookup in [('preferred_date_from', 'preferred_date__gte'), ('preferred_date_to', 'preferred_date__lte')]:
        if params.get(key):
            try:
                dates[key] = date.fromisoformat(str(params[key]))
            except ValueError:
                raise ValidationError({key: 'Use a valid YYYY-MM-DD date.'})
            queryset = queryset.filter(**{lookup: dates[key]})
    if dates.get('preferred_date_from') and dates.get('preferred_date_to') and dates['preferred_date_from'] > dates['preferred_date_to']:
        raise ValidationError({'preferred_date_to': 'End date must be on or after the start date.'})
    budgets = {}
    for key, lookup in [('min_budget', 'budget__gte'), ('max_budget', 'budget__lte')]:
        if params.get(key) not in ('', None):
            try:
                amount = Decimal(str(params[key]))
                if not amount.is_finite() or amount < 0 or amount > Decimal('9999999999.99'):
                    raise InvalidOperation
            except (InvalidOperation, ValueError):
                raise ValidationError({key: 'Enter a non-negative budget within the supported range.'})
            budgets[key] = amount
            queryset = queryset.filter(**{lookup: amount})
    if 'min_budget' in budgets and 'max_budget' in budgets and budgets['min_budget'] > budgets['max_budget']:
        raise ValidationError({'max_budget': 'Maximum budget must be at least the minimum.'})
    ordering = str(params.get('ordering') or 'newest')
    if ordering == 'urgency':
        queryset = queryset.annotate(urgency_rank=Case(When(urgency='EMERGENCY', then=0), When(urgency='HIGH', then=1), When(urgency='NORMAL', then=2), default=3, output_field=IntegerField()))
        return queryset.order_by('urgency_rank', '-created_at', '-pk')
    fields = {'newest': '-created_at', 'oldest': 'created_at', 'preferred_date': 'preferred_date', 'budget_low': 'budget', 'budget_high': '-budget'}
    return queryset.order_by(fields.get(ordering, '-created_at'), '-pk')
