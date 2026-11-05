"""Only a checkout link the API calls live may reach a payer's browser.

A Flutterwave link keeps the payment methods it was minted with. A stored link
from before checkout was limited to card and bank transfer still opens the old
page (PayPal included), so the web frontend must not hand it out just because
`authorization_url` is non-empty.
"""

from django.test import SimpleTestCase

from frontend.views import _live_checkout_url

LINK = "https://checkout.flutterwave.com/v3/hosted/pay/flwlnk-test"


def payment(state, url=LINK):
    payload = {"authorization_url": url}
    if state is not None:
        payload["checkout"] = {"state": state, "methods": "card,banktransfer"}
    return payload


class LiveCheckoutGateTests(SimpleTestCase):
    def test_a_live_link_is_returned(self):
        self.assertEqual(_live_checkout_url(payment("LIVE")), LINK)

    def test_stale_or_unusable_links_are_withheld(self):
        for state in ("EXPIRED", "OLD_METHODS", "NOT_MINTED", ""):
            self.assertEqual(_live_checkout_url(payment(state)), "", state)

    def test_an_api_without_a_state_still_takes_payment(self):
        # Half-deployed pair: the older API reports no `checkout` object at all.
        # Payments must keep working rather than dead-end on an empty button.
        self.assertEqual(_live_checkout_url(payment(None)), LINK)

    def test_a_withheld_link_is_not_reachable_by_any_other_route(self):
        # The provider host check still applies on top of the state check.
        self.assertEqual(
            _live_checkout_url(payment("LIVE", "https://evil.example/pay")), ""
        )
