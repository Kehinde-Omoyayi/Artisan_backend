"""Invalidate public artisan discovery after result-affecting data commits."""
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from booking.models import Booking
from home.models import ArtisanProfile, ArtisanService, Institution, Locations, Service, ServiceCategory
from reviews.models import Review
from services.models import ArtisanVerification

from .discovery_cache import invalidate_public_discovery_cache

UserModel = get_user_model()


def _invalidate_after_commit(using):
    transaction.on_commit(invalidate_public_discovery_cache, using=using)


@receiver(pre_save, sender=UserModel, dispatch_uid="profiles.discovery_user_pre_save")
def _remember_discovery_user_fields(sender, instance, using, **kwargs):
    if not instance.pk:
        instance._discovery_fields_changed = True
        return
    previous = (
        sender.objects.using(using)
        .filter(pk=instance.pk)
        .values("first_name", "last_name", "is_active", "security_status")
        .first()
    )
    instance._discovery_fields_changed = bool(
        previous is None
        or any(previous[field] != getattr(instance, field) for field in previous)
    )


@receiver(post_save, sender=UserModel, dispatch_uid="profiles.discovery_user_saved")
def _invalidate_for_user_discovery_changes(sender, instance, using, **kwargs):
    if getattr(instance, "_discovery_fields_changed", False):
        _invalidate_after_commit(using)


for model in (
    ArtisanProfile,
    ArtisanService,
    Service,
    ServiceCategory,
    Locations,
    Institution,
    ArtisanVerification,
    Review,
    Booking,
):
    post_save.connect(
        lambda sender, using, **kwargs: _invalidate_after_commit(using),
        sender=model,
        weak=False,
        dispatch_uid=f"profiles.discovery.{model._meta.label_lower}.saved",
    )
    post_delete.connect(
        lambda sender, using, **kwargs: _invalidate_after_commit(using),
        sender=model,
        weak=False,
        dispatch_uid=f"profiles.discovery.{model._meta.label_lower}.deleted",
    )
