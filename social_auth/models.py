from django.conf import settings
from django.db import models


class SocialIdentity(models.Model):
    """A verified provider subject linked to one marketplace account.

    Provider tokens and client secrets are deliberately not persisted.
    """

    GOOGLE = "google"
    APPLE = "apple"
    PROVIDERS = ((GOOGLE, "Google"), (APPLE, "Apple"))

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="social_identities",
    )
    provider = models.CharField(max_length=12, choices=PROVIDERS)
    subject = models.CharField(max_length=255)
    email = models.EmailField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("provider", "subject"),
                name="social_identity_provider_subject_uniq",
            ),
            models.UniqueConstraint(
                fields=("user", "provider"),
                name="social_identity_user_provider_uniq",
            ),
        ]
        indexes = [models.Index(fields=("provider", "email"), name="social_identity_email_idx")]

    def __str__(self):
        return f"{self.provider} identity for user {self.user_id}"
