from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from home.models import ArtisanProfile
from .models import ArtisanVerification

UserModel = get_user_model()


@transaction.atomic
def sync_artisan_profile_status(artisan_or_id):
    """Synchronize one artisan's profile status and return whether it is active.

    VERIFIED + NORMAL -> ACTIVE
    SUSPENDED profile -> remains SUSPENDED
    Otherwise, an ACTIVE profile becomes INACTIVE.
    DRAFT and INACTIVE remain unchanged until the artisan is eligible.
    """
    artisan_id = getattr(artisan_or_id, "pk", artisan_or_id)
    if artisan_id is None:
        return False

    artisan = (
        ArtisanProfile.objects.select_for_update()
        .select_related("user")
        .filter(pk=artisan_id)
        .first()
    )
    if artisan is None:
        return False

    # Never automatically remove a suspension.
    if artisan.profile_status == "SUSPENDED":
        return False

    verification_status = (
        ArtisanVerification.objects
        .filter(artisan_id=artisan.pk)
        .values_list("verification_status", flat=True)
        .first()
    )

    # SECURITY_STATUS is normally stored on the user. Support it on the
    # artisan profile too, if that is where the project's model defines it.
    security_status = getattr(artisan.user, "security_status", None)
    if security_status is None:
        security_status = getattr(artisan, "security_status", None)

    eligible = (
        verification_status == "VERIFIED"
        and security_status == "NORMAL"
    )

    if eligible:
        target_status = "ACTIVE"
    elif artisan.profile_status == "ACTIVE":
        target_status = "INACTIVE"
    else:
        return False

    if artisan.profile_status != target_status:
        # QuerySet.update avoids recursively firing the ArtisanProfile signal.
        ArtisanProfile.objects.filter(pk=artisan.pk).update(
            profile_status=target_status,
            updated_at=timezone.now(),
        )
        artisan.profile_status = target_status

    return target_status == "ACTIVE"


def _schedule_sync(artisan_id):
    """Run synchronization after the current transaction commits."""
    if artisan_id is not None:
        transaction.on_commit(
            lambda artisan_id=artisan_id: sync_artisan_profile_status(artisan_id)
        )


@receiver(
    post_save,
    sender=ArtisanVerification,
    dispatch_uid="services.active.sync_after_verification_save",
)
def _sync_after_verification_save(sender, instance, **kwargs):
    # Runs after verification is created, set to PENDING/REJECTED/VERIFIED,
    # reset, or suspended. Only VERIFIED + NORMAL can produce ACTIVE.
    _schedule_sync(instance.artisan_id)


@receiver(
    post_save,
    sender=ArtisanProfile,
    dispatch_uid="services.active.sync_after_artisan_profile_save",
)
def _sync_after_profile_save(sender, instance, **kwargs):
    # Also corrects manual or administrative profile-status changes.
    _schedule_sync(instance.pk)


@receiver(
    post_save,
    sender=UserModel,
    dispatch_uid="services.active.sync_after_user_save",
)
def _sync_after_user_save(sender, instance, **kwargs):
    # When security_status is on the user model, any saved security change
    # triggers a fresh eligibility check for the associated artisan.
    artisan_id = (
        ArtisanProfile.objects
        .filter(user_id=instance.pk)
        .values_list("pk", flat=True)
        .first()
    )
    _schedule_sync(artisan_id)
