"""Settings for the API service (the whole of this project).

The browser UI is a separate Django project that calls this service over HTTP,
so nothing here renders marketplace pages: no ``frontend`` app, no in-process
transport, no UI static files. The only templates the API still renders are the
two staff-security pages used by the Django admin step-up flow, which are part
of the admin itself.

``Artisan_platform.settings`` already carries this shape; this module is kept as
the explicit, named entry point so the API project has one obvious settings
module to point ``manage.py``, ``wsgi.py`` and ``asgi.py`` at.
"""

from .settings import *  # noqa: F401,F403

# Belt and braces: if an older deployment still lists the in-process UI app,
# drop it here so the API can never boot with UI wiring attached.
INSTALLED_APPS = [app for app in INSTALLED_APPS if app != "frontend"]  # noqa: F405

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
                if not processor.startswith("frontend.")
            ],
        },
    }
    for engine in TEMPLATES  # noqa: F405
]

# Frontend-only transport configuration, removed so a stale environment value
# can never make the API call itself over HTTP.
for _frontend_setting in (
    "ARTISAN_API_BASE_URL",
    "ARTISAN_API_ROOT_URL",
    "ARTISAN_API_TRANSPORT",
    "ARTISAN_API_TIMEOUT",
):
    globals().pop(_frontend_setting, None)
del _frontend_setting
