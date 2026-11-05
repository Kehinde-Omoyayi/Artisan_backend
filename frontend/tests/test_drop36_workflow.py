"""Regression coverage for the simple chat view, read-only request brief and phase track."""
from django.test import SimpleTestCase
from django.template.loader import render_to_string

from frontend.templatetags.site_tags import short_system_message
from frontend.views import (
    _conversation_request_summary,
    _conversation_submit_payload,
    _fallback_phase_steps,
    _private_conversation_messages,
)


class RequestAndChatViewTests(SimpleTestCase):
    # ---------------------------------------------------------------- payload
    def test_submission_payload_always_submits(self):
        result = _conversation_submit_payload({"description": "Fix my sockets"})
        self.assertIs(result["submit_request"], True)
        self.assertEqual(result["description"], "Fix my sockets")

    def test_payload_never_carries_draft_or_close_flags(self):
        payload = _conversation_submit_payload({"description": "Fix my sockets"})
        self.assertNotIn("close_request", payload)
        self.assertEqual(
            set(payload) - {"submit_request"},
            {"description"},
        )

    def test_nullable_request_details_can_be_cleared(self):
        result = _conversation_submit_payload(
            {"budget": "", "preferred_date": "", "budget_unknown": "true"}
        )
        self.assertIsNone(result["budget"])
        self.assertIsNone(result["preferred_date"])
        self.assertIs(result["budget_unknown"], True)

    # ----------------------------------------------------------- read-only brief
    def test_request_brief_reads_only_server_fields(self):
        brief = _conversation_request_summary(
            {
                "service_request": {
                    "service": "Electrical repair",
                    "location": "Lekki, Lagos",
                    "preferred_date": "2026-11-02",
                    "budget": "25000.00",
                    "description": "Sockets spark when switched on.",
                }
            }
        )
        self.assertEqual(brief["service"], "Electrical repair")
        self.assertEqual(brief["description"], "Sockets spark when switched on.")
        self.assertEqual(brief["budget"], "25000.00")

    def test_request_brief_is_empty_without_any_request(self):
        self.assertEqual(_conversation_request_summary({}), {})
        self.assertEqual(_conversation_request_summary({"service_request": None}), {})

    # ----------------------------------------------------------------- phases
    def test_fallback_phase_track_runs_from_start_to_conclusion(self):
        booking_steps = _fallback_phase_steps({"state": "BOOKING"})
        booking_current = [step for step in booking_steps if step["current"]]
        self.assertEqual(len(booking_current), 1)
        self.assertEqual(booking_current[0]["key"], "PAY")

        steps = _fallback_phase_steps({"state": "IN_PROGRESS"})
        self.assertEqual([step["key"] for step in steps][0], "REQUEST")
        self.assertEqual([step["key"] for step in steps][-1], "DISPUTE")
        current = [step for step in steps if step["current"]]
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["key"], "PROGRESS")
        self.assertTrue(all(step["complete"] for step in steps[:3]))

    # -------------------------------------------------------------- rendering
    def room(self, role="customer"):
        return {
            "id": "12345678-1234-4234-8234-123456789abc",
            "conversation_type": "SERVICE",
            "viewer_role": role,
            "viewer_id": 1,
            "available_actions": [],
            "journey": _fallback_phase_steps({"state": "OPEN"}),
            # Guidance is still returned by the API for older clients, but the
            # simple chat view must never print it (it may contain untrusted text).
            "guidance": {
                "role": role,
                "title": "Inspect the work" if role == "customer" else "Wait for customer inspection",
                "message": "<script>untrusted text</script>",
                "steps": ["Use explicit controls."],
                "action": None,
            },
        }

    def render(self, room, role="customer", **extra):
        context = {
            "conversation": room,
            "role": role,
            "nav_role": role,
            "csrf_token": "test-only",
            "current_phase": {"step": 1, "key": "REQUEST", "title": "Request & Chat",
                              "description": "Describe the work and agree the request details."},
            "request_brief": {},
        }
        context.update(extra)
        return render_to_string("conversations/detail.html", context)

    def test_chat_view_is_just_header_messages_tools_and_composer(self):
        for role in ["customer", "artisan"]:
            html = self.render(self.room(role), role)
            self.assertIn('id="conversation-stream"', html)
            self.assertIn('id="conversation-message-form"', html)
            self.assertIn('id="chat-service-tools"', html)
            self.assertNotIn('id="booking-guidance"', html)
            self.assertNotIn('class="conversation-side"', html)
            self.assertNotIn("Media cleanup:", html)
            self.assertNotIn("Request #", html)

    def test_request_editor_is_gone_from_the_chat(self):
        html = self.render(self.room())
        self.assertNotIn("request-assistant-card", html)
        self.assertNotIn("Review or edit your request", html)
        self.assertNotIn("REQUEST DETAILS</span>", html)
        self.assertNotIn("request-assistant.js", html)
        self.assertNotIn('id="request-assistant-form"', html)
        # No draft affordances anywhere in the chat.
        for phrase in ("Save draft", "Draft saved", "request_draft", "Send Request"):
            self.assertNotIn(phrase, html)

    def test_the_customer_never_fills_a_request_form(self):
        """The chat is the request; the artisan builds the booking."""
        for role in ("customer", "artisan"):
            html = self.render(self.room(role), role)
            self.assertNotIn("/request-details/", html)
            self.assertNotIn("Set the request details", html)
            self.assertNotIn('class="request-brief"', html)
            for phrase in (
                "Service category",
                "What needs to be done?",
                "Preferred date",
                "Budget (optional)",
                "Send request",
            ):
                self.assertNotIn(phrase, html)

    def test_customer_only_accepts_declines_or_counters(self):
        room = self.room()
        room["available_actions"] = ["ACCEPT_OFFER", "DECLINE_OFFER", "COUNTER_OFFER"]
        room["active_offer"] = {
            "id": "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa",
            "total_amount": "25000.00",
            "labour_amount": "20000.00",
            "materials_amount": "5000.00",
            "transport_amount": "0.00",
            "additional_amount": "0.00",
            "duration_value": 1,
            "duration_unit": "DAYS",
            "version": 1,
            "status": "PROPOSED",
            "materials": [],
        }
        html = self.render(room)
        self.assertIn("Accept offer", html)
        self.assertIn("Reject offer", html)
        self.assertIn("counter", html)
        # Customers never get the artisan's pricing form.
        self.assertNotIn('data-offer-form="new"', html)
        self.assertNotIn("Send itemized offer", html)

    def _room_with_offer(self):
        room = self.room()
        room["available_actions"] = ["ACCEPT_OFFER", "DECLINE_OFFER", "COUNTER_OFFER"]
        room["request_reference"] = {"status": "SUBMITTED"}
        room["active_offer"] = {
            "id": "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa",
            "total_amount": "25000.00",
            "labour_amount": "20000.00",
            "materials_amount": "5000.00",
            "transport_amount": "0.00",
            "additional_amount": "0.00",
            "duration_value": 1,
            "duration_unit": "DAYS",
            "version": 2,
            "status": "PROPOSED",
            "materials": [],
        }
        return room

    def test_the_offer_arrives_inside_the_conversation(self):
        """The itemized quote is part of the thread, not a panel in Service tools."""
        html = self.render(self._room_with_offer())
        scroll = html.index('id="conversation-scroll-area"')
        stream = html.index('id="conversation-stream"')
        card = html.index('id="offer-card"')
        tools = html.index('id="chat-service-tools"')
        self.assertLess(stream, card)
        self.assertLess(card, tools)
        self.assertGreater(card, scroll)
        # Nothing about the offer hides inside the collapsed panel.
        self.assertLess(html.index("Accept offer"), tools)
        inside_tools = html[tools : html.index('class="chat-composer"')]
        self.assertNotIn("Accept offer", inside_tools)
        self.assertIn('class="workflow-card offer-card chat-offer-bubble"', html)
        self.assertIn("data-offer-event=\"aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa:2:PROPOSED\"", html)

    def test_offer_events_are_the_card_not_a_repeated_bubble(self):
        room = self._room_with_offer()
        events = [
            {"id": "m1", "event_code": "OFFER_CREATED", "message_type": "SYSTEM",
             "body": "Offer created for NGN 25,000.00.", "sender_id": None, "attachments": []},
            {"id": "m2", "event_code": "OFFER_COUNTERED", "message_type": "SYSTEM",
             "body": "Counter offer created for NGN 24,000.00.", "sender_id": None, "attachments": []},
            {"id": "m3", "event_code": "OFFER_ACCEPTED", "message_type": "SYSTEM",
             "body": "Offer accepted.", "sender_id": None, "attachments": []},
        ]
        html = self.render(room, conversation_messages=events)
        self.assertNotIn("Offer created for NGN", html)
        self.assertNotIn("Counter offer created for NGN", html)
        # Outcomes still read as lines in the thread.
        self.assertIn("Offer accepted.", html)

    def test_cancelling_is_quiet_and_lives_at_the_bottom_of_service_tools(self):
        html = self.render(self._room_with_offer())
        self.assertNotIn("Close unbooked request", html)
        tools = html.index('id="chat-service-tools"')
        composer = html.index('class="chat-composer"')
        self.assertNotIn("Cancel this request", html[:tools])
        danger = html.index('class="chat-tools-danger"')
        self.assertGreater(danger, tools)
        self.assertLess(danger, composer)
        self.assertIn('class="chat-tools-danger-link"', html)
        # The strip may not render at all here, but it must never carry a cancel.
        if 'class="chat-action-strip"' in html:
            strip = html[html.index('class="chat-action-strip"') : composer]
            self.assertNotIn("Cancel this request", strip)
            self.assertNotIn("request_close", strip)

    def test_phase_track_spans_start_to_conclusion(self):
        html = self.render(self.room())
        self.assertIn('class="chat-phase"', html)
        self.assertIn("Where this request is now", html)
        for title in (
            "Request &amp; Chat",
            "Offer &amp; Negotiation",
            "Booking &amp; Escrow",
            "Service in Progress",
            "Separate add-on orders",
            "Completion &amp; Review",
            "Support (if needed)",
        ):
            self.assertIn(title, html)

    def test_phase_track_sits_below_the_chat_box(self):
        html = self.render(self.room())
        workspace_end = html.index('class="chat-composer"')
        phase_start = html.index('class="chat-phase"')
        self.assertGreater(phase_start, workspace_end, "phase card must follow the composer")
        # The workspace closes before the phase card starts, so it is not inside the chat box.
        self.assertIn("</div>", html[workspace_end:phase_start])

    def test_composer_has_camera_beside_the_mic_and_no_media_menu(self):
        room = self.room()
        room["recording_enabled"] = True
        html = self.render(room)
        composer = html[html.index('class="chat-composer"') : html.index('class="chat-phase"')]
        # Paperclip (files) + camera + mic + send; no three-item sheet to read.
        self.assertIn("data-composer-attach", composer)
        self.assertIn("data-composer-camera", composer)
        self.assertIn("chat-camera-button", composer)
        self.assertNotIn("chat-attach-menu", composer)
        self.assertNotIn("data-attach-kind", composer)
        self.assertNotIn("Photos &amp; videos", composer)
        # The camera button sits immediately before the mic, which becomes Send.
        self.assertLess(
            composer.index("data-composer-camera"),
            composer.index("data-voice-record"),
        )
        self.assertIn("chat-send-button", composer)
        self.assertIn('id="conversation-camera"', composer)
        self.assertIn('capture="environment"', composer)
        self.assertIn('accept="image/*,video/*"', composer)
        # One queue for photos, videos and documents.
        self.assertIn('name="attachments"', composer)
        self.assertIn("video/*", composer)
        self.assertIn("application/pdf", composer)
        self.assertIn("up to 30 MB", html)

    def test_audio_stays_hidden_when_the_deployment_disables_it(self):
        html = self.render(self.room())  # recording_enabled not set
        self.assertNotIn("data-voice-record", html)
        # Photos, videos and documents remain reachable from the paperclip/camera.
        self.assertIn("data-composer-attach", html)
        self.assertIn("data-composer-camera", html)

    def test_customer_requests_changes_in_words_not_in_a_quote_form(self):
        room = self.room()
        room["available_actions"] = ["ACCEPT_OFFER", "DECLINE_OFFER", "COUNTER_OFFER"]
        room["active_offer"] = {
            "id": "o1", "amount": "45000.00", "total_amount": "45000.00",
            "labour_amount": "30000.00", "materials_amount": "15000.00",
            "transport_amount": "0.00", "additional_amount": "0.00",
            "version": 1, "status": "PROPOSED", "scope": "Replace three sockets",
            "materials": [], "materials_included": False,
            "expires_at": "2030-01-01T10:00:00Z",
            "scheduled_start": "2030-01-02T10:00:00Z",
            "duration_value": 1, "duration_unit": "DAYS",
        }
        html = self.render(room)
        # Plain-words revision note, posted with the active offer.
        self.assertIn("chat-revision", html)
        self.assertIn('value="revision"', html)
        self.assertIn('name="note"', html)
        self.assertIn("What should change?", html)
        # The quote builder belongs to the artisan only.
        self.assertNotIn("Edit the quote and resend it", html)
        self.assertNotIn("Labour base price", html)
        self.assertNotIn('value="counter"', html)

    def test_artisan_can_edit_the_quote_and_add_other_items(self):
        room = self.room(role="artisan")
        room["available_actions"] = ["REVISE_OFFER"]
        room["active_offer"] = {
            "id": "o1", "amount": "45000.00", "total_amount": "45000.00",
            "labour_amount": "30000.00", "materials_amount": "15000.00",
            "transport_amount": "0.00", "additional_amount": "0.00",
            "version": 1, "status": "REVISION_REQUESTED", "scope": "Replace three sockets",
            "materials": [], "materials_included": False,
            "expires_at": "2030-01-01T10:00:00Z",
            "scheduled_start": "2030-01-02T10:00:00Z",
            "duration_value": 1, "duration_unit": "DAYS",
        }
        html = self.render(room)
        self.assertIn("Edit the quote and resend it", html)
        self.assertIn("Materials &amp; items", html)
        self.assertIn("Materials included in this price", html)
        self.assertIn("Add item", html)
        self.assertIn("Materials subtotal", html)
        self.assertIn("Scheduled start", html)
        self.assertIn("Duration value", html)
        self.assertIn("Transport (₦)", html)
        self.assertIn("Additional charges (₦)", html)
        # The retired free-text fields are gone from the interface completely.
        for retired in (
            "name=\"scope\"",
            "name=\"notes\"",
            "name=\"payment_terms\"",
            "name=\"exclusions\"",
            "name=\"workmanship_terms\"",
            "name=\"materials_details\"",
            "name=\"expires_at\"",
        ):
            self.assertNotIn(retired, html)
        self.assertNotIn("chat-revision", html)

    def test_documents_download_and_images_preview(self):
        room = self.room()
        room["title"] = "Chinedu Okafor"
        room["counterparty"] = {"first_name": "Chinedu", "role": "artisan"}
        html = self.render(
            room,
            conversation_messages=[
                {
                    "message_type": "USER", "sender_id": 2, "sender_name": "Chinedu",
                    "body": "", "created_at": "10:00",
                    "attachments": [
                        {"id": "a1", "content_type": "image/jpeg", "url": "/media/1/", "scan_status": "CLEAN"},
                        {"id": "a2", "content_type": "application/pdf", "url": "/media/2/", "scan_status": "CLEAN"},
                        {"id": "a3", "content_type": "image/png", "url": None, "scan_status": "PENDING"},
                    ],
                }
            ],
        )
        # Images preview inline, documents carry a real download link.
        self.assertIn('class="chat-attachment-image"', html)
        self.assertIn('src="/media/1/"', html)
        self.assertIn('download', html)
        self.assertIn("tap to download", html)
        # An unavailable attachment is explained instead of silently dropped.
        self.assertIn("chat-attachment-pending", html)
        self.assertIn("still being checked", html)

    def test_untrusted_guidance_copy_is_never_printed_in_chat(self):
        for role in ["customer", "artisan"]:
            html = self.render(self.room(role), role)
            self.assertNotIn("<script>untrusted text</script>", html)
            self.assertNotIn("untrusted text", html)

    def test_quoting_is_never_gated_on_a_description_confirmation(self):
        room = self.room("artisan")
        room["available_actions"] = ["SEND_OFFER"]
        html = self.render(room, "artisan")
        self.assertIn('value="offer"', html)
        self.assertNotIn("description_done", html)
        self.assertNotIn("Confirm the work description", html)
        self.assertNotIn("CONFIRM_DESCRIPTION", html)

    def test_chat_bubbles_show_the_time_only_never_a_date(self):
        room = self.room()
        room["title"] = "Chinedu Okafor"
        room["counterparty"] = {"first_name": "Chinedu", "role": "artisan"}
        html = self.render(
            room,
            conversation_messages=[
                {
                    "message_type": "USER", "sender_id": 2, "sender_name": "Chinedu",
                    "body": "On my way.", "created_at": "2026-10-28T11:22:00Z",
                    "attachments": [],
                }
            ],
        )
        self.assertIn('<time datetime="2026-10-28T11:22:00Z">12:22 pm</time>', html)
        # The visible stamp is the clock; the date only survives inside datetime="".
        self.assertNotIn(">2026-10-28", html)
        self.assertNotIn("28 October", html)

    def test_everything_but_the_chat_lives_inside_the_service_tools_dropdown(self):
        room = self.room()
        room["available_actions"] = ["SEND_OFFER"]
        html = self.render(room)
        panel = html.index('id="chat-service-tools"')
        body = html.index('class="chat-tools-body"')
        composer = html.index('class="chat-composer"')
        self.assertGreater(body, panel)
        self.assertGreater(composer, body)
        # After the collapsed panel closes, nothing but the composer (and, when a
        # decision is waiting, the one-line action strip) is between chat and send.
        panel_close = html.index("</details>", body)
        between = html[panel_close:composer]
        self.assertNotIn("workflow-card", between)
        self.assertNotIn("request-brief", between)
        for phrase in ("Set the request details", "Request details", "request-brief"):
            self.assertNotIn(phrase, html)

    def test_recording_has_a_visible_state_and_a_stop_glyph(self):
        room = self.room()
        room["recording_enabled"] = True
        html = self.render(room)
        composer = html[html.index('class="chat-composer"') : html.index('class="chat-phase"')]
        self.assertIn("chat-stop-glyph", composer)
        self.assertIn('aria-pressed="false"', composer)
        self.assertIn('data-voice-tools', composer)
        self.assertIn('data-voice-dot', composer)
        self.assertIn('data-voice-status', composer)
        self.assertIn('data-voice-discard', composer)
        # The old always-hidden stop button is gone; the mic itself stops.
        self.assertNotIn("data-voice-stop", composer)

    def test_workflow_controls_remain_available_behind_the_tools_bar(self):
        room = self.room()
        room["available_actions"] = ["PAY"]
        room["payment"] = {
            "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "status": "PAYMENT_PENDING",
            "total_amount": "12000",
        }
        html = self.render(room)
        self.assertIn('value="pay"', html)
        self.assertIn("Pay securely · card or bank transfer", html)

    def test_offer_review_controls_survive_the_slim_down(self):
        room = self.room()
        room["available_actions"] = ["ACCEPT_OFFER", "DECLINE_OFFER"]
        room["active_offer"] = {"id": "offer-1", "total_amount": "45000"}
        html = self.render(room)
        self.assertIn('value="accept"', html)
        self.assertIn('value="revision"', html)
        self.assertIn('value="decline"', html)
        self.assertIn("Review the offer · accept or request changes", html)

    def test_live_countdown_is_still_visible_in_the_chat(self):
        room = self.room()
        room["booking"] = {
            "id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "status": "IN_PROGRESS",
            "actual_started_at": "",
            "countdown_ends_at": "2026-10-27T10:00:00Z",
            "duration_label": "2 hours",
            "price_agreed": "45000",
        }
        html = self.render(room)
        self.assertIn('class="chat-status-strip"', html)
        self.assertIn("data-countdown", html)
        self.assertIn("data-countdown-value", html)

    def test_system_bubbles_drop_long_next_steps_instructions(self):
        self.assertEqual(
            short_system_message("Offer accepted.\n\nNext steps: Customer: review the offer carefully."),
            "Offer accepted.",
        )
        self.assertEqual(short_system_message(None), "")
        self.assertEqual(
            short_system_message("Hello, can we start tomorrow?"),
            "Hello, can we start tomorrow?",
        )

    def test_private_proxy_preserves_pagination(self):
        payload = {"count": 0, "next": None, "previous": None, "results": []}
        self.assertIs(
            _private_conversation_messages(
                payload, "12345678-1234-4234-8234-123456789abc"
            ),
            payload,
        )
        self.assertIn("results", payload)
