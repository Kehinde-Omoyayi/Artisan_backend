"""Escrow payment is completed inside the chat, never on a booking page."""

from django.template.loader import render_to_string
from django.test import SimpleTestCase

from frontend.views import _request_still_being_written, _safe_checkout_url

ROOM = {
    "id": "12345678-1234-4234-8234-123456789abc",
    "conversation_type": "SERVICE",
    "viewer_role": "customer",
    "viewer_id": 1,
    "title": "Chinedu Okafor",
    "state": "BOOKING",
    "available_actions": ["SEND_MESSAGE", "PAY", "CANCEL"],
    "booking": {"id": "9d1b7a44-0f0e-4b0e-9b3e-1f6a2c5d7e88", "status": "ACCEPTED"},
    "payment": {
        "id": "8a1f2c44-1111-4a11-9b22-33cc44dd55ee",
        "status": "PAYMENT_PENDING",
        "total_amount": "33500.00",
    },
    "journey": [],
}


def render(**overrides):
    context = {
        "conversation": ROOM,
        "conversation_messages": [],
        "offer_history": [],
        "role": "customer",
        "nav_role": "customer",
        "csrf_token": "test-only",
        "current_phase": {"step": 3, "key": "PAY", "title": "Booking & Escrow"},
        "payment_id": "8a1f2c44-1111-4a11-9b22-33cc44dd55ee",
        "payment_checkout_url": "",
        "payment_status_url": "/payments/8a1f2c44-1111-4a11-9b22-33cc44dd55ee/status/",
        "bank_transfer": {},
        "request_is_draft": False,
        "request_brief": {},
        "payment_paid": False,
    }
    context.update(overrides)
    return render_to_string("conversations/detail.html", context)


class RequestLockTests(SimpleTestCase):
    """The composer stays shut until the customer actually sends the request.

    The API reports a submitted request under `service_request` and an unwritten
    one under `request_reference`; reading the wrong key left the message box
    open, which is exactly the bug this pins.
    """

    def room(self, **overrides):
        payload = {
            "viewer_role": "customer",
            "service_request": None,
            "request_reference": {"id": 3, "status": "DRAFT", "closed_at": None},
            "booking": None,
        }
        payload.update(overrides)
        return payload

    def test_an_unsubmitted_request_locks_the_box(self):
        self.assertTrue(_request_still_being_written(self.room()))

    def test_a_conversation_with_nothing_written_yet_is_locked(self):
        self.assertTrue(_request_still_being_written(self.room(request_reference=None)))

    def test_a_submitted_request_opens_the_box(self):
        self.assertFalse(
            _request_still_being_written(
                self.room(service_request={"id": 3, "status": "OPEN", "description": "Tap drips"})
            )
        )

    def test_a_closed_request_stays_open_for_reading(self):
        self.assertFalse(
            _request_still_being_written(
                self.room(request_reference={"id": 3, "status": "CLOSED", "closed_at": "2026-10-31T10:00:00Z"})
            )
        )

    def test_a_booking_always_opens_the_box(self):
        self.assertFalse(
            _request_still_being_written(
                self.room(booking={"id": "9d1b7a44-0f0e-4b0e-9b3e-1f6a2c5d7e88", "status": "ACCEPTED"})
            )
        )

    def test_the_artisan_is_never_locked(self):
        self.assertFalse(_request_still_being_written(self.room(viewer_role="artisan")))


class CheckoutUrlTests(SimpleTestCase):
    def test_only_a_flutterwave_https_checkout_is_accepted(self):
        self.assertEqual(
            _safe_checkout_url("https://checkout.flutterwave.com/v3/hosted/pay/abc"),
            "https://checkout.flutterwave.com/v3/hosted/pay/abc",
        )
        for bad in (
            "http://checkout.flutterwave.com/v3/hosted/pay/abc",
            "https://evil.example.com/pay",
            "https://flutterwave.com.evil.example/pay",
            "javascript:alert(1)",
            "",
            None,
        ):
            self.assertEqual(_safe_checkout_url(bad), "", bad)


class ChatPaymentTests(SimpleTestCase):
    def test_the_pay_button_lives_in_the_chat(self):
        html = render(payment_id="", payment_status_url="")
        self.assertIn("Pay ₦33500.00 into escrow", html)
        self.assertIn('name="action" value="pay_transfer"', html)

    def test_a_queued_payment_polls_from_the_chat(self):
        html = render()
        self.assertIn("data-payment-watch", html)
        self.assertIn("/payments/8a1f2c44-1111-4a11-9b22-33cc44dd55ee/status/", html)

    def test_a_ready_checkout_url_is_a_quiet_card_fallback(self):
        """Bank transfer is the path in the chat; the hosted page is only a fallback."""
        html = render(
            payment_checkout_url="https://checkout.flutterwave.com/v3/hosted/pay/abc"
        )
        self.assertIn("https://checkout.flutterwave.com/v3/hosted/pay/abc", html)
        self.assertIn("Pay by card instead", html)

    def test_a_queued_payment_still_pays_in_place(self):
        html = render(payment_checkout_url="")
        self.assertIn("Pay ₦33500.00 into escrow", html)
        self.assertNotIn("Finish payment", html)

    def test_the_account_to_transfer_into_is_shown_in_the_chat(self):
        html = render(
            bank_transfer={
                "account_number": "9755152912",
                "bank": "Flutterwave MFB",
                "account_name": "Kehinde Omoyayi",
                "amount": "33500.00",
                "note": "Please make a bank transfer to Kehinde Omoyayi",
                "expires_at": "2026-06-10 19:00:16",
                "seconds_left": 2820,
                "live": True,
            }
        )
        self.assertIn("9755152912", html)
        self.assertIn("Flutterwave MFB", html)
        self.assertIn("Kehinde Omoyayi", html)
        self.assertIn("Beneficiary", html)
        self.assertIn("I have made this bank transfer", html)
        self.assertIn('data-copy="9755152912"', html)
        self.assertIn("chat-transfer-track-fill", html)
        # The card states the timer, and what happens when it runs out: the
        # account number is replaced by a way to get a fresh one.
        self.assertIn("Time left", html)
        self.assertIn("data-transfer-count", html)
        self.assertIn('data-transfer-expires="2026-06-10 19:00:16"', html)
        self.assertIn("Get a new transfer account", html)
        self.assertIn("data-transfer-renew hidden", html)
        self.assertIn("Escrow total", html)

    def test_the_pay_button_is_hidden_while_a_transfer_account_is_open(self):
        """One method at a time: a live account hides the way to start another."""
        html = render(
            bank_transfer={
                "account_number": "9755152912",
                "bank": "Flutterwave MFB",
                "account_name": "Kehinde Omoyayi",
                "amount": "33500.00",
                "note": "",
                "expires_at": "2026-06-10 19:00:16",
                "seconds_left": 600,
                "live": True,
                "live_until": "2026-06-10T19:00:16+00:00",
            }
        )
        self.assertIn('data-pay-live="true"', html)
        self.assertIn("data-pay-start hidden", html)
        self.assertIn("data-pay-wait", html)
        self.assertIn("A transfer account is open for this payment", html)

    def test_the_pay_button_returns_once_the_account_has_closed(self):
        html = render(
            bank_transfer={
                "account_number": "9755152912",
                "bank": "Flutterwave MFB",
                "account_name": "Kehinde Omoyayi",
                "amount": "33500.00",
                "note": "",
                "expires_at": "2026-06-10 19:00:16",
                "seconds_left": 0,
                "live": False,
                "live_until": "",
            }
        )
        self.assertIn('data-pay-live="false"', html)
        self.assertIn("data-pay-start>", html)
        self.assertIn("Pay ₦33500.00 into escrow", html)
        self.assertIn("data-pay-wait hidden", html)

    def test_a_payment_with_no_account_yet_still_offers_the_button(self):
        html = render()
        self.assertIn('data-pay-live="false"', html)
        self.assertIn("Pay ₦33500.00 into escrow", html)

    def test_no_account_panel_until_the_provider_returns_one(self):
        html = render()
        self.assertNotIn("I have made this bank transfer", html)

    def test_a_paid_booking_offers_the_receipt(self):
        html = render(payment_paid=True)
        self.assertIn("View receipt", html)
        self.assertIn(
            "/payments/8a1f2c44-1111-4a11-9b22-33cc44dd55ee/receipt/", html
        )
        self.assertIn("Download receipt", html)

    def test_an_unpaid_booking_offers_no_receipt(self):
        html = render(payment_paid=False)
        self.assertNotIn("View receipt", html)
        self.assertNotIn("receipt.pdf", html)

    def test_a_draft_request_locks_the_message_box(self):
        html = render(request_is_draft=True)
        self.assertIn("Describe what you need", html)
        self.assertIn('name="action" value="request_submit"', html)
        self.assertIn("Request service", html)
        self.assertIn("chat-composer is-locked", html)

    def test_an_open_request_keeps_the_composer(self):
        html = render(request_is_draft=False)
        self.assertNotIn("chat-composer is-locked", html)
        self.assertNotIn("Request service", html)

    def test_a_secured_payment_hides_the_pay_button(self):
        room = dict(ROOM)
        room["available_actions"] = ["SEND_MESSAGE", "CANCEL"]
        room["payment"] = dict(ROOM["payment"], status="HELD")
        html = render(conversation=room)
        self.assertNotIn("Pay ₦33500.00 securely", html)
        self.assertNotIn("Finish payment", html)
