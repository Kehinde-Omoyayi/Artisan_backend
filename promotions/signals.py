"""Invalidate cached public placements whenever eligibility or curated inventory changes."""
from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from home.models import ArtisanProfile, ArtisanService, CustomUser, Institution, Locations, Service
from profiles.discovery_cache import invalidate_public_discovery_cache
from services.models import ArtisanVerification
from .models import ArtisanMarketPick, PromotionCampaign, PromotionInventory, PromotionPayment


_RELEVANT_FIELDS = {
    ArtisanProfile: {
        "profile_status", "business_name", "bio", "country", "state", "area", "profile_photo",
        "average_rating", "years_experience",
    },
    CustomUser: {"is_active", "security_status", "first_name", "last_name", "phone_number"},
    ArtisanVerification: {"verification_status", "verified_at"},
    ArtisanService: {"artisan", "service", "location", "starting_price"},
    Service: {"is_active", "name", "category", "description"},
    Locations: {"country", "state", "area"},
    Institution: {"name", "campus", "abbreviation", "location", "is_active"},
    PromotionCampaign: {
        "status", "payment_verified_at", "verified_payment", "started_at", "expires_at",
        "promotion_type", "inventory", "artisan", "services_snapshot", "target_snapshot",
    },
    PromotionPayment: {"status", "verified_at", "campaign"},
    PromotionInventory: {"capacity", "target_label", "target_key", "promotion_type"},
    ArtisanMarketPick: {"is_active", "position", "selected_at", "artisan"},
}


def _changed(sender, update_fields):
    if update_fields is None:
        return True
    return bool(_RELEVANT_FIELDS.get(sender, set()).intersection(update_fields))


def _invalidate(*, using=None):
    transaction.on_commit(invalidate_public_discovery_cache, using=using)


def _register_model_signals(model):
    def saved(sender, instance, update_fields=None, using=None, **kwargs):
        if _changed(sender, update_fields):
            _invalidate(using=using)

    def deleted(sender, instance, using=None, **kwargs):
        _invalidate(using=using)

    post_save.connect(saved, sender=model, weak=False, dispatch_uid=f"promotion-public-cache-save-{model._meta.label_lower}")
    post_delete.connect(deleted, sender=model, weak=False, dispatch_uid=f"promotion-public-cache-delete-{model._meta.label_lower}")


for _model in _RELEVANT_FIELDS:
    _register_model_signals(_model)
