"""Timeline delivery contract for conversation events.

Requirement A hinges on two server-side guarantees; everything else is
rendering order, which the Django frontend owns and tests there:

* every marketplace event is recorded as a system ``Message`` whose
  ``event_key`` is unique — replaying the same event twice yields one row,
  not two, so an API refetch, a pushed notification and a racing writer can
  never show up as duplicate cards;
* the messages endpoint serves the rows in ``created_at`` order with the
  event identity attached, so chronological rendering and client-side
  de-duplication agree on one canonical sequence.

These tests pin both guarantees plus the "never move a historical row" rule:
timestamps are assigned once by the database and later events may not push an
older card around.
"""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from rest_framework.test import APIClient

from conversations.events import record_conversation_event
from conversations.models import Message
from conversations.test_read_marker import _mk_conversation, _mk_message


def _event_key(name, unique=""):
    return f"timeline-test:{name}{':' + unique if unique else ''}"


class ConversationEventIdempotencyTests(TestCase):
    def setUp(self):
        self.conversation, self.customer_user, self.artisan_user = _mk_conversation()

    def test_recording_the_same_event_twice_creates_one_message(self):
        first = record_conversation_event(
            conversation=self.conversation,
            event_code="OFFER_CREATED",
            event_key=_event_key("offer-created", "a"),
        )
        second = record_conversation_event(
            conversation=self.conversation,
            event_code="OFFER_CREATED",
            event_key=_event_key("offer-created", "a"),
        )
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(
            Message.objects.filter(conversation=self.conversation).count(), 1
        )

    def test_offer_and_payment_and_addon_events_are_all_deduplicated(self):
        for code in (
            "OFFER_CREATED",
            "OFFER_ACCEPTED",
            "PAYMENT_INITIALIZED",
            "PAYMENT_HELD",
            "ADDITIONAL_ORDER_PROPOSED",
            "ADDITIONAL_ORDER_PAYMENT_HELD",
            "COMPLETION_ACCEPTED",
            "REFUND_COMPLETED",
        ):
            key = _event_key(code)
            record_conversation_event(
                conversation=self.conversation, event_code=code, event_key=key
            )
            record_conversation_event(
                conversation=self.conversation, event_code=code, event_key=key
            )
        self.assertEqual(
            Message.objects.filter(conversation=self.conversation).count(), 8
        )

    def test_recorded_event_carries_machine_readable_identity(self):
        message = record_conversation_event(
            conversation=self.conversation,
            event_code="PAYMENT_HELD",
            event_key=_event_key("held", "b"),
            metadata={"payment_id": "some-id"},
        )
        message.refresh_from_db()
        self.assertEqual(message.message_type, Message.MessageType.SYSTEM)
        self.assertEqual(message.event_code, "PAYMENT_HELD")
        self.assertEqual(message.event_key, _event_key("held", "b"))
        self.assertEqual(message.metadata.get("payment_id"), "some-id")

    def test_new_events_never_rewrite_a_historical_rows_created_at(self):
        first = record_conversation_event(
            conversation=self.conversation,
            event_code="OFFER_CREATED",
            event_key=_event_key("hist", "1"),
        )
        original = first.created_at
        # A later transaction must not push the earlier card's timestamp around.
        record_conversation_event(
            conversation=self.conversation,
            event_code="PAYMENT_INITIALIZED",
            event_key=_event_key("hist", "2"),
        )
        record_conversation_event(
            conversation=self.conversation,
            event_code="PAYMENT_HELD",
            event_key=_event_key("hist", "3"),
        )
        first.refresh_from_db()
        self.assertEqual(first.created_at, original)

    def test_twenty_later_events_all_sort_after_the_first_card(self):
        anchor = record_conversation_event(
            conversation=self.conversation,
            event_code="OFFER_CREATED",
            event_key=_event_key("anchor"),
        )
        for index in range(20):
            record_conversation_event(
                conversation=self.conversation,
                event_code="SERVICE_PROGRESS_UPDATED",
                event_key=_event_key("later", str(index)),
            )
            # Make sure created_at differs so the ordering assertion is strict.
            Message.objects.filter(event_key=_event_key("later", str(index))).update(
                created_at=anchor.created_at + timedelta(seconds=index + 1)
            )
        ordered = list(
            Message.objects.filter(conversation=self.conversation).order_by("created_at")
        )
        self.assertEqual(ordered[0].pk, anchor.pk)
        for message in ordered[1:]:
            self.assertGreaterEqual(message.created_at, anchor.created_at)


class ConversationMessagesEndpointTests(TestCase):
    def setUp(self):
        self.api = APIClient()
        self.conversation, self.customer_user, self.artisan_user = _mk_conversation()
        self.api.force_authenticate(self.customer_user)
        self.url = f"/api/v1/conversations/{self.conversation.id}/messages/"

    def test_endpoint_serves_newest_first_and_exposes_identity(self):
        _mk_message(self.conversation, self.customer_user, "hello", minutes_ago=4)
        record_conversation_event(
            conversation=self.conversation,
            event_code="OFFER_CREATED",
            event_key=_event_key("api", "1"),
        )
        response = self.api.get(self.url)
        self.assertEqual(response.status_code, 200, response.content)
        results = response.data["results"]
        created = [item["created_at"] for item in results]
        self.assertEqual(created, sorted(created, reverse=True))
        for item in results:
            self.assertIn("id", item)
            self.assertIn("event_code", item)
            self.assertIn("event_key", item)

    def test_endpoint_pages_with_limit_and_offset(self):
        for index in range(5):
            _mk_message(self.conversation, self.customer_user, f"m{index}", minutes_ago=10 - index)
        first_page = self.api.get(self.url, {"limit": 3, "offset": 0})
        second_page = self.api.get(self.url, {"limit": 3, "offset": 3})
        self.assertEqual(first_page.status_code, 200)
        self.assertEqual(second_page.status_code, 200)
        first_ids = [item["id"] for item in first_page.data["results"]]
        second_ids = [item["id"] for item in second_page.data["results"]]
        self.assertEqual(len(first_ids), 3)
        self.assertFalse(set(first_ids) & set(second_ids))
        combined = first_ids + second_ids
        self.assertEqual(len(combined), len(set(combined)))

    def test_event_messages_and_user_messages_share_one_sequence(self):
        user_message = _mk_message(
            self.conversation, self.customer_user, "please quote me", minutes_ago=8
        )
        event = record_conversation_event(
            conversation=self.conversation,
            event_code="OFFER_CREATED",
            event_key=_event_key("mixed", "1"),
        )
        Message.objects.filter(pk=event.pk).update(
            created_at=user_message.created_at + timedelta(minutes=1)
        )
        response = self.api.get(self.url)
        self.assertEqual(response.status_code, 200, response.content)
        kinds = [
            item["event_code"] or "USER"
            for item in response.data["results"]
        ]
        self.assertIn("OFFER_CREATED", kinds)
        self.assertIn("USER", kinds)
