"""
ASGI config for Artisan_platform project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.2/howto/deployment/asgi/
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'Artisan_platform.api_settings')

#: Force this project's settings. A ``DJANGO_SETTINGS_MODULE`` left in the
#: environment by the other project must never be honoured here, or the process
#: boots with the wrong URLconf and answers 404 to every request. An override
#: that clearly belongs to this project (the test settings, for example) is kept.
if not os.environ.get("DJANGO_SETTINGS_MODULE", "").startswith("Artisan_platform."):
    os.environ["DJANGO_SETTINGS_MODULE"] = "Artisan_platform.api_settings"


application = get_asgi_application()
