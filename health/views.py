from django.core.cache import cache
from django.db import connection
from django.http import JsonResponse
from django.views.decorators.http import require_GET

@require_GET
def live(request):
    return JsonResponse({"status": "ok"})

@require_GET
def ready(request):
    try:
        connection.ensure_connection()
        cache.set("healthcheck", "ok", timeout=5)
        if cache.get("healthcheck") != "ok":
            raise RuntimeError("Redis unavailable")
    except Exception:
        return JsonResponse({"status": "unready"}, status=503)
    return JsonResponse({"status": "ready"})
