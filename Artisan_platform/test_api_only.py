"""Smoke tests for the separate API-only Django configuration."""

from django.apps import apps
from django.test import SimpleTestCase
from django.urls import Resolver404, resolve


class APIOnlyProjectTests(SimpleTestCase):
    def test_frontend_app_is_not_installed(self):
        self.assertFalse(apps.is_installed("frontend"))

    def test_api_admin_and_health_routes_are_available(self):
        for route in (
            "/admin/",
            "/admin/mfa/",
            "/api/v1/register/",
            "/api/v1/social-auth/google/web/start/",
            "/health/live/",
            "/health/ready/",
        ):
            with self.subTest(route=route):
                resolve(route)

    def test_browser_ui_routes_are_not_mounted(self):
        for route in (
            "/",
            "/dashboard/",
            "/auth/login/",
            "/discover/artisans/",
            "/bookings/",
            "/system/health/",
        ):
            with self.subTest(route=route), self.assertRaises(Resolver404):
                resolve(route)
