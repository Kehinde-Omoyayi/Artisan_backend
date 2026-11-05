"""The tunnel host must pass the host check and the CSRF origin check.

ngrok hands out three shapes of free address: ``*.ngrok-free.app``,
``*.ngrok-free.dev`` and, on paid plans, ``*.ngrok.app`` / ``*.ngrok.dev``.
All of them have to work without editing .env after every restart — a
DisallowedHost (400) or a CSRF 403 on every form post is what "nothing works"
looks like from the browser.
"""

from django.conf import settings
from django.http.request import validate_host
from django.middleware.csrf import CsrfViewMiddleware
from django.test import RequestFactory, SimpleTestCase

HOSTS = (
    "deprecate-folic-getting.ngrok-free.app",
    "deprecate-folic-getting.ngrok-free.dev",
    "deprecate-folic-getting.ngrok.app",
    "deprecate-folic-getting.ngrok.dev",
    "localhost",
)


class NgrokHostTests(SimpleTestCase):
    def test_every_ngrok_host_shape_is_allowed(self):
        for host in HOSTS:
            self.assertTrue(validate_host(host, settings.ALLOWED_HOSTS), host)

    def test_foreign_hosts_are_still_refused(self):
        for host in ("evil.example", "ngrok-free.dev.evil.example", "10.0.0.7"):
            self.assertFalse(validate_host(host, settings.ALLOWED_HOSTS), host)

    def test_a_form_post_from_the_tunnel_origin_passes_csrf(self):
        middleware = CsrfViewMiddleware(lambda request: None)
        factory = RequestFactory()
        for host in HOSTS:
            if host == "localhost":
                continue
            request = factory.post(
                "/chat/", HTTP_HOST=host, HTTP_ORIGIN=f"https://{host}"
            )
            self.assertTrue(middleware._origin_verified(request), host)

        stranger = factory.post(
            "/chat/",
            HTTP_HOST="deprecate-folic-getting.ngrok-free.dev",
            HTTP_ORIGIN="https://evil.example",
        )
        self.assertFalse(middleware._origin_verified(stranger))
