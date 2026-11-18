from django.apps import AppConfig

class HomeConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'home'

    def ready(self):
        from . import schema  # noqa: F401  (registers the OpenAPI auth scheme)
