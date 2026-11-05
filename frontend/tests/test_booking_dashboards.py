"""Coverage for the booking dashboards, action visibility and offer draft safety."""
from django.template.loader import render_to_string
from django.test import SimpleTestCase

from frontend.api import APIError
from frontend.views import (
    _api_error_items,
    _booking_actions,
    _offer_draft_context,
    _offer_error_context,
    _offer_payload_from_post,
)

BOOKING = {
    "id": "9d1b7a44-0f0e-4b0e-9b3e-1f6a2c5d7e88",
    "request_id": 7,
    "customer_id": "3f0e2d10-1111-4222-8333-444455556666",
    "artisan_id": 12,
    "conversation_id": "aaaabbbb-cccc-4ddd-8eee-ffff00001111",
    "viewer_role": "customer",
    "scheduled_time_start": "2026-11-04T10:00:00Z",
    "scheduled_time_end": "2026-11-06T10:00:00Z",
    "price_agreed": "27000.00",
    "status": "ACCEPTED",
    "duration_label": "2 days",
}


def booking(**overrides):
    data = dict(BOOKING)
    data.update(overrides)
    return data


class BookingActionVisibilityTests(SimpleTestCase):
    def render(self, actions, **extra):
        context = {
            "booking": booking(available_actions=actions),
            "booking_actions": _booking_actions(booking(available_actions=actions)),
            "service_summary": {"service": "Electrical repair", "location": "Ikeja"},
            "role": "customer",
            "nav_role": "customer",
            "csrf_token": "test",
        }
        context.update(extra)
        return render_to_string("bookings/detail.html", context)

    def test_only_the_actions_the_api_allows_are_rendered(self):
        html = self.render(
            [
                {"code": "CANCEL_BOOKING", "label": "Cancel booking", "variant": "danger"},
                {"code": "PAY_BOOKING", "label": "Pay for booking", "variant": "primary"},
                {"code": "VIEW_HISTORY", "label": "View history", "variant": "ghost"},
            ]
        )
        self.assertIn("CANCEL_BOOKING", html)
        self.assertIn("PAY_BOOKING", html)
        self.assertNotIn("START_WORK", html)
        self.assertNotIn("CONFIRM_COMPLETION", html)
        self.assertNotIn("ACCEPT_BOOKING", html)
        self.assertNotIn("Start work", html)
        self.assertNotIn("Confirm completion", html)
        self.assertIn("Pay ₦27,000.00 securely", html)

    def test_a_customer_never_sees_artisan_only_actions(self):
        html = self.render(
            [{"code": "CONFIRM_COMPLETION", "label": "Confirm completion", "variant": "primary"}]
        )
        self.assertIn("Confirm completion", html)
        self.assertNotIn("Accept booking", html)
        self.assertNotIn("Decline booking", html)
        self.assertNotIn("Start work", html)
        self.assertNotIn("Submit completion", html)

    def test_no_available_action_says_so_instead_of_showing_buttons(self):
        html = self.render([])
        self.assertIn("No action is available to you", html)
        self.assertNotIn('class="booking-action-link', html)

    def test_action_links_map_to_real_routes_and_unknown_codes_are_dropped(self):
        actions = _booking_actions(
            booking(
                available_actions=[
                    {"code": "CANCEL_BOOKING", "label": "Cancel"},
                    {"code": "PAY_BOOKING", "label": "Pay"},
                    {"code": "OPEN_CONVERSATION", "label": "Open"},
                    {"code": "SOMETHING_NEW", "label": "Ignore me"},
                ]
            )
        )
        by_code = {item["code"]: item for item in actions}
        self.assertEqual(
            by_code["CANCEL_BOOKING"]["url"],
            f"/bookings/{BOOKING['id']}/cancel/",
        )
        self.assertEqual(
            by_code["PAY_BOOKING"]["url"],
            f"/payments/booking/{BOOKING['id']}/initialize/",
        )
        self.assertEqual(
            by_code["OPEN_CONVERSATION"]["url"],
            f"/discover/conversations/{BOOKING['conversation_id']}/",
        )
        self.assertEqual(by_code["SOMETHING_NEW"]["url"], "")

    def test_the_customer_dashboard_links_every_booking_to_its_detail_page(self):
        item = booking(
            actions=_booking_actions(booking(available_actions=[])),
            primary_action=None,
            can_cancel=False,
        )
        html = render_to_string(
            "customer/bookings.html",
            {
                "bookings": [item],
                "role": "customer",
                "nav_role": "customer",
                "csrf_token": "test",
            },
        )
        self.assertIn(f'/bookings/{BOOKING["id"]}/', html)
        self.assertIn("₦27,000.00", html)
        self.assertIn("Accepted", html)
        self.assertIn("2 days", html)


class OfferDraftPreservationTests(SimpleTestCase):
    def payload(self):
        return {
            "labour_amount": "25000",
            "materials_json": '[{"name":"Socket","quantity":"2","unit":"pcs","unit_price":"1500"}]',
            "materials_amount": "3000",
            "transport_amount": "1500",
            "additional_amount": "2500",
            "additional_charge_details": "Replace the burnt back-box",
            "scheduled_start": "2026-11-05T09:00",
            "duration_value": "3",
            "duration_unit": "DAYS",
            "total_amount": "32000",
        }

    def test_the_payload_contains_only_the_itemised_fields(self):
        payload = _offer_payload_from_post(self.payload())
        self.assertEqual(
            set(payload),
            {
                "labour_amount",
                "materials_amount",
                "total_amount",
                "materials",
                "transport_amount",
                "additional_amount",
                "additional_charge_details",
                "materials_included",
                "scheduled_start",
                "duration_value",
                "duration_unit",
            },
        )
        self.assertEqual(payload["duration_unit"], "DAYS")
        self.assertEqual(len(payload["materials"]), 1)
        self.assertIn("+01:00", payload["scheduled_start"])

    def test_a_failed_send_reports_the_field_and_keeps_every_entry(self):
        error = APIError(
            400,
            {
                "code": "INVALID",
                "detail": "Validation failed.",
                "details": {
                    "total_amount": [
                        "Payable total must equal labour + materials + transport + additional."
                    ],
                    "duration_value": ["State how long the work will take."],
                },
            },
        )
        messages, per_field = _offer_error_context(error)
        self.assertTrue(any("Payable total" in message for message in messages))
        self.assertEqual(
            per_field["duration_value"], "State how long the work will take."
        )
        draft = _offer_draft_context(self.payload())
        self.assertEqual(draft["labour_amount"], "25000")
        self.assertEqual(draft["transport_amount"], "1500")
        self.assertEqual(draft["additional_charge_details"], "Replace the burnt back-box")
        self.assertEqual(draft["duration_value"], "3")
        self.assertEqual(draft["materials"][0]["name"], "Socket")

        html = render_to_string(
            "conversations/detail.html",
            {
                "conversation": {
                    "id": "12345678-1234-4234-8234-123456789abc",
                    "conversation_type": "SERVICE",
                    "viewer_role": "artisan",
                    "viewer_id": 2,
                    "available_actions": ["SEND_OFFER"],
                    "journey": [],
                },
                "offer_errors": messages,
                "offer_field_errors": per_field,
                "offer_draft": draft,
                "offer_form": "new",
                "offer_action_key": "key-1",
                "duration_unit_options": [("HOURS", "Hours"), ("DAYS", "Days")],
                "current_phase": {"step": 1, "title": "Request & Chat", "description": ""},
                "request_brief": {},
                "role": "artisan",
                "nav_role": "artisan",
                "csrf_token": "test",
            },
        )
        # The error is explained in plain words...
        self.assertIn("Nothing was sent.", html)
        self.assertIn("Payable total", html)
        # ...and the entries are still there, including the idempotency key.
        self.assertIn('value="25000"', html)
        self.assertIn('value="Replace the burnt back-box"', html)
        self.assertIn('value="key-1"', html)
        self.assertIn("offer-field-errors", html)

    def test_the_numeric_api_error_shape_is_understood(self):
        pairs = _api_error_items(
            APIError(409, {"detail": "This booking has an associated payment."})
        )
        self.assertEqual(pairs[0][1], "This booking has an associated payment.")
