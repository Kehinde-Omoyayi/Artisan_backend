from django.apps import AppConfig


class CatalogueConfig(AppConfig):
    name = "catalogue"
    verbose_name = "Marketplace catalogue"

    def ready(self):
        """Warm the register in the background.

        The first search has to read 250 country files (~8 MB); doing that on a
        person's first keystroke made the picker feel broken. Tests skip it.
        """
        import os
        import sys
        import threading

        if any(arg in sys.argv for arg in ("test", "makemigrations", "migrate", "collectstatic")):
            return
        if os.environ.get("CATALOGUE_WARM", "1") == "0":
            return

        def warm():
            try:
                from . import sources

                sources.catalogue_page({"q": "a"})
            except Exception:  # pragma: no cover - a warm-up must never break boot
                pass

        threading.Thread(target=warm, name="catalogue-warm", daemon=True).start()
