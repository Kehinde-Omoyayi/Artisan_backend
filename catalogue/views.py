"""Read-only catalogue endpoints.

Public data, no authentication, no database: the geography and the institution
register are published JSON that the browser app fetches little by little.
"""

from django.http import JsonResponse
from django.views.decorators.http import require_GET
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from . import sources


@require_GET
def geography(request):
    """The whole world list. Large (~2 MB): for the server, not the browser."""
    response = JsonResponse(sources.world_geography())
    response["Cache-Control"] = "public, max-age=86400"
    return response


@require_GET
def geography_index(request):
    """country -> states (~80 KB), with the file that holds each area list."""
    payload = sources.geography_index()
    files = {
        country: (
            "/api/v1/catalogue/geography/countries/"
            + str(path).rsplit("/", 1)[-1].removesuffix(".json")
            + "/"
        )
        for country, path in (payload.get("files") or {}).items()
    }
    response = JsonResponse({"countries": payload.get("countries") or {}, "files": files})
    response["Cache-Control"] = "public, max-age=86400"
    return response


@require_GET
def geography_country(request, slug):
    """One country's areas, keyed by state."""
    payload = sources.country_areas(slug)
    if not payload:
        return JsonResponse({"detail": "Not found."}, status=404)
    response = JsonResponse(payload)
    response["Cache-Control"] = "public, max-age=86400"
    return response


class InstitutionList(APIView):
    """The paged institution search the pickers use.

    Query: q, country, state, area, id, offset. Fifty rows per page, with
    `count` so the picker can offer "Show more (N left)".
    """

    permission_classes = [AllowAny]
    authentication_classes: list = []

    def get(self, request):
        response = JsonResponse(sources.catalogue_page(request.query_params))
        response["Cache-Control"] = "public, max-age=600"
        return response
