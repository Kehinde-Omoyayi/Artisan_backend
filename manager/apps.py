from django.apps import AppConfig


class ManagerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "manager"
    verbose_name = "Manager console"

    def ready(self):
        # Importing the module registers the post_save receivers that mirror real
        # lifecycle transitions into the analytics event store.
        from manager import signals  # noqa: F401
