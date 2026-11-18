from django.urls import path

from . import views

urlpatterns = [
    path("geography/", views.geography, name="catalogue-geography"),
    path("geography/index/", views.geography_index, name="catalogue-geography-index"),
    path(
        "geography/countries/<slug:slug>/",
        views.geography_country,
        name="catalogue-geography-country",
    ),
    path("institutions/", views.InstitutionList.as_view(), name="catalogue-institutions"),
]
