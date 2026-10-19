"""Settings for the frontend-free API service.

The repository's original ``settings`` module still supports the integrated
legacy application. This small overlay reuses its backend configuration while
removing the in-process UI app and its template processor.
"""

from .settings import *  # noqa: F401,F403

# The browser UI is deployed as a separate Django project and is not installed
# in, or served by, the API process.
INSTALLED_APPS = [app for app in INSTALLED_APPS if app != "frontend"]
ROOT_URLCONF = "Artisan_platform.api_urls"
STATICFILES_DIRS = []

TEMPLATES = [
    {
        **engine,
        "OPTIONS": {
            **engine.get("OPTIONS", {}),
            "context_processors": [
                processor
                for processor in engine.get("OPTIONS", {}).get("context_processors", [])
                if processor != "frontend.context_processors.notifications"
            ],
        },
    }
    for engine in TEMPLATES
]

# These settings are consumed only by the integrated in-process browser app.
for _frontend_setting in (
    "ARTISAN_API_BASE_URL",
    "ARTISAN_API_ROOT_URL",
    "ARTISAN_API_TIMEOUT",
    "ARTISAN_API_TRANSPORT",
):
    globals().pop(_frontend_setting, None)
del _frontend_setting
