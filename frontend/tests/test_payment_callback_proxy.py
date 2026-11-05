"""One public tunnel: the provider lands on the web host, which forwards to the API.

The ngrok free plan allows a single tunnel, so the API cannot be the public
address Flutterwave calls back. These routes live on the web host and pass the
request to the API over localhost.
"""

from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

PAYMENT_ID = "8a1f2c44-1111-4a11-9b22-33cc44dd55ee"


class FakeApi:
    def __init__(self, payment_id=PAYMENT_ID):
        self.payment_id = payment_id
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, params))
        if path == "/payments/flutterwave/callback/":
            payload = {"status": "received"}
            if self.payment_id:
                payload["payment_id"] = self.payment_id
            return 202, payload, {}
        if path.startswith("/payments/"):
            return 200, {"id": self.payment_id, "status": "HELD", "conversation_id": "c-1"}, {}
        return 200, {}, {}


class ProviderReturnTests(TestCase):
    def test_the_browser_is_handed_to_the_payment_return_page(self):
        api = FakeApi()
        with patch("frontend.views.APIClient", return_value=api):
            response = self.client.get(
                "/payments/callback/", {"tx_ref": "PAY-1", "transaction_id": "123456"}
            )

        self.assertRedirects(
            response,
            reverse("payment-return", args=[PAYMENT_ID]),
            fetch_redirect_response=False,
        )
        self.assertEqual(api.calls[0], ("/payments/flutterwave/callback/", {"tx_ref": "PAY-1", "transaction_id": "123456"}))

    def test_a_callback_that_matches_nothing_does_not_strand_the_payer(self):
        with patch("frontend.views.APIClient", return_value=FakeApi(payment_id="")):
            response = self.client.get(
                "/payments/callback/", {"tx_ref": "PAY-UNKNOWN", "transaction_id": "999999"}
            )

        self.assertRedirects(response, reverse("bookings"), fetch_redirect_response=False)

    def test_a_callback_without_identifiers_goes_back_to_the_booking_list(self):
        response = self.client.get("/payments/callback/")

        self.assertRedirects(response, reverse("bookings"), fetch_redirect_response=False)


class WebhookProxyTests(TestCase):
    @override_settings(ARTISAN_API_ROOT_URL="http://127.0.0.1:8000")
    def test_the_raw_body_and_the_signature_are_relayed(self):
        with patch("requests.post") as post:
            post.return_value = type("Upstream", (), {"status_code": 200})()
            response = self.client.post(
                "/payments/flutterwave/webhook/",
                data=b'{"event":"charge.completed"}',
                content_type="application/json",
                HTTP_VERIF_HASH="secret-hash",
            )

        self.assertEqual(response.status_code, 200)
        args, kwargs = post.call_args
        self.assertEqual(
            args[0], "http://127.0.0.1:8000/api/v1/payments/flutterwave/webhook/"
        )
        self.assertEqual(kwargs["data"], b'{"event":"charge.completed"}')
        self.assertEqual(kwargs["headers"]["verif-hash"], "secret-hash")

    @override_settings(ARTISAN_API_ROOT_URL="http://127.0.0.1:8000")
    def test_an_upstream_outage_is_not_acknowledged_to_the_provider(self):
        with patch("requests.post", side_effect=RuntimeError("upstream down")):
            response = self.client.post(
                "/payments/flutterwave/webhook/",
                data=b"{}",
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 502)
