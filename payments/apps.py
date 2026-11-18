from django.apps import AppConfig


class PaymentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "payments"

    def ready(self):
        # Configuration checks that would otherwise only appear as a failed
        # checkout ("Payment could not be confirmed") after a payer tried.
        from . import checks  # noqa: F401
