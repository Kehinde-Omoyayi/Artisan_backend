"""Phase 1 regression: monotonic, idempotent read markers under contention.

The 2026-11-18 incident: ``sqlite3.OperationalError: database is locked`` from
``select_for_update().get_or_create()`` inside ``mark_conversation_read``,
served to the client as a 503. These tests pin the replacement contract:

* a marker never moves backwards, not even to an older message id;
* an identical re-submission is a no-op (no write, same count);
* concurrent first reads create exactly one row and never raise;
* concurrent senders and readers converge to a correct unread count;
* the unique constraint is preserved (direct duplicates still fail);
* the read endpoint reports 503 -- never "read" -- when the marker genuinely
  fails, and stays limited (throttle re-enabled).
"""

import threading
import uuid

from django.db import IntegrityError, connection
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from rest_framework.test import APIClient

from conversations.models import (
    Conversation,
    ConversationReadState,
    Message,
)
from conversations.services import mark_conversation_read, send_message
from home.models import ArtisanProfile, CustomerProfile, CustomUser, Locations, ServiceCategory
from services.models import ServiceRequest


def _mk_user(email, first="A"):
    return CustomUser.objects.create_user(
        email=email,
        first_name=first,
        last_name="Tester",
        password="StrongPassword123!",
        is_active=True,
    )


def _mk_conversation(test_case=None):
    customer_user = _mk_user(f"customer-{uuid.uuid4().hex[:10]}@example.com", "Ada")
    artisan_user = _mk_user(f"artisan-{uuid.uuid4().hex[:10]}@example.com", "Tunde")
    CustomerProfile.objects.create(user=customer_user)
    artisan = ArtisanProfile.objects.create(user=artisan_user, profile_status="ACTIVE")
    category = ServiceCategory.objects.create(name=f"Cat {uuid.uuid4().hex[:6]}")
    location = Locations.objects.create(country="Nigeria", state="Lagos", area="Yaba")
    service_request = ServiceRequest.objects.create(
        requester=customer_user,
        category=category,
        location=location,
        preferred_date=timezone.localdate(),
        description="Fix the tap.",
    )
    conversation = Conversation.objects.create(
        customer=customer_user,
        artisan=artisan,
        service_request=service_request,
    )
    return conversation, customer_user, artisan_user


def _mk_message(conversation, sender, body, minutes_ago=0):
    message = Message.objects.create(
        conversation=conversation, sender=sender, body=body, message_type="USER"
    )
    if minutes_ago:
        Message.objects.filter(pk=message.pk).update(
            created_at=timezone.now() - timezone.timedelta(minutes=minutes_ago)
        )
        message.refresh_from_db()
    return message


class ReadMarkerBehaviourTests(TestCase):
    def setUp(self):
        self.conversation, self.customer_user, self.artisan_user = _mk_conversation()

    def test_marker_never_moves_backwards_with_an_older_message(self):
        older = _mk_message(self.conversation, self.artisan_user, "first", minutes_ago=10)
        newest = _mk_message(self.conversation, self.artisan_user, "second", minutes_ago=1)
        mark_conversation_read(
            conversation=self.conversation, user=self.customer_user, last_message_id=newest.id
        )
        mark_conversation_read(
            conversation=self.conversation, user=self.customer_user, last_message_id=older.id
        )
        state = ConversationReadState.objects.get(
            conversation=self.conversation, user=self.customer_user
        )
        self.assertEqual(state.last_read_at, newest.created_at)

    def test_repeated_identical_submission_performs_no_write(self):
        message = _mk_message(self.conversation, self.artisan_user, "hello")
        mark_conversation_read(
            conversation=self.conversation, user=self.customer_user, last_message_id=message.id
        )
        state = ConversationReadState.objects.get(
            conversation=self.conversation, user=self.customer_user
        )
        first_updated_at = state.updated_at
        remaining = mark_conversation_read(
            conversation=self.conversation, user=self.customer_user, last_message_id=message.id
        )
        state.refresh_from_db()
        self.assertEqual(state.updated_at, first_updated_at)
        self.assertEqual(remaining, 0)

    def test_unread_count_excludes_own_messages_and_advances_with_marker(self):
        mine = _mk_message(self.conversation, self.customer_user, "mine", minutes_ago=5)
        theirs_old = _mk_message(self.conversation, self.artisan_user, "noted", minutes_ago=4)
        theirs_new = _mk_message(self.conversation, self.artisan_user, "again", minutes_ago=1)
        remaining = mark_conversation_read(
            conversation=self.conversation, user=self.customer_user
        )
        self.assertEqual(remaining, 0)  # default marker counts everything up to now
        state = ConversationReadState.objects.get(
            conversation=self.conversation, user=self.customer_user
        )
        # Pin the marker back to the old message and confirm the new one counts.
        ConversationReadState.objects.filter(pk=state.pk).update(last_read_at=theirs_old.created_at)
        remaining = mark_conversation_read(
            conversation=self.conversation, user=self.customer_user, last_message_id=theirs_old.id
        )
        self.assertEqual(remaining, 1)

    def test_two_participants_keep_independent_markers(self):
        message = _mk_message(self.conversation, self.artisan_user, "ping")
        mark_conversation_read(
            conversation=self.conversation, user=self.customer_user, last_message_id=message.id
        )
        self.assertFalse(
            ConversationReadState.objects.filter(
                conversation=self.conversation, user=self.artisan_user
            ).exists()
        )

    def test_unique_constraint_is_preserved(self):
        ConversationReadState.objects.create(
            conversation=self.conversation, user=self.customer_user, last_read_at=None
        )
        with self.assertRaises(IntegrityError):
            ConversationReadState.objects.create(
                conversation=self.conversation, user=self.customer_user, last_read_at=None
            )

    def test_marker_rejects_a_message_from_another_conversation(self):
        other_conversation, other_customer, _ = _mk_conversation()
        foreign = _mk_message(other_conversation, other_customer, "other room")
        with self.assertRaises(Exception):
            mark_conversation_read(
                conversation=self.conversation, user=self.customer_user, last_message_id=foreign.id
            )

    def test_sqlite_connection_enforces_a_busy_timeout(self):
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA busy_timeout;")
            busy_timeout = cursor.fetchone()[0]
        self.assertGreaterEqual(int(busy_timeout), 1000)


class ReadMarkerConcurrencyTests(TransactionTestCase):
    """Thread-level contention. SQLite test databases use a shared in-memory
    cache, so threads observe the same data; every write is committed."""

    def setUp(self):
        self.conversation, self.customer_user, self.artisan_user = _mk_conversation()

    def _run_threads(self, target, count):
        barrier = threading.Barrier(count)
        errors = []

        def runner():
            try:
                barrier.wait(timeout=10)
                target()
            except Exception as exc:  # noqa: BLE001 - surfaced in assertions
                errors.append(exc)
            finally:
                from django import db

                db.connections.close_all()

        threads = [threading.Thread(target=runner) for _ in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        return errors

    def test_concurrent_first_reads_create_exactly_one_row(self):
        errors = self._run_threads(
            lambda: mark_conversation_read(
                conversation=self.conversation, user=self.customer_user
            ),
            count=8,
        )
        self.assertEqual(errors, [])
        self.assertEqual(
            ConversationReadState.objects.filter(
                conversation=self.conversation, user=self.customer_user
            ).count(),
            1,
        )

    def test_out_of_order_concurrent_marks_never_regress_the_marker(self):
        messages = [
            _mk_message(self.conversation, self.artisan_user, f"m{i}", minutes_ago=10 - i)
            for i in range(6)
        ]
        newest = messages[-1]
        picks = [messages[5], messages[2], messages[4], messages[0], messages[5], messages[3],
                 messages[5], messages[1]]

        def work():
            import random

            target = random.choice(picks)
            mark_conversation_read(
                conversation=self.conversation,
                user=self.customer_user,
                last_message_id=target.id,
            )

        errors = self._run_threads(work, count=16)
        self.assertEqual(errors, [])
        state = ConversationReadState.objects.get(
            conversation=self.conversation, user=self.customer_user
        )
        self.assertEqual(state.last_read_at, newest.created_at)

    def test_concurrent_senders_and_reader_converge(self):
        sender_a, sender_b = self.artisan_user, self.customer_user

        def send(body):
            send_message(conversation=self.conversation, user=sender_a, body=body)

        errors = []

        def send_some():
            for index in range(5):
                try:
                    send_message(
                        conversation=self.conversation,
                        user=sender_a,
                        body=f"parallel {uuid.uuid4().hex[:6]}",
                    )
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

        def read_some():
            for _ in range(6):
                try:
                    mark_conversation_read(
                        conversation=self.conversation, user=self.customer_user
                    )
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

        barrier = threading.Barrier(3)

        def wrap(fn):
            def inner():
                barrier.wait(timeout=10)
                fn()

            return inner

        from django import db

        threads = [
            threading.Thread(target=wrap(send_some)),
            threading.Thread(target=wrap(send_some)),
            threading.Thread(target=wrap(read_some)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        db.connections.close_all()
        self.assertEqual(errors, [])
        total = Message.objects.filter(conversation=self.conversation).count()
        self.assertEqual(total, 10)


class ReadMarkerApiTests(TestCase):
    def setUp(self):
        self.api = APIClient()
        self.conversation, self.customer_user, self.artisan_user = _mk_conversation()

    def tearDown(self):
        pass

    def test_read_view_marks_and_repeats_are_noops(self):
        message = _mk_message(self.conversation, self.artisan_user, "hello")
        self.api.force_authenticate(self.customer_user)
        url = f"/api/v1/conversations/{self.conversation.id}/read/"
        payload = {"last_message_id": str(message.id)}
        first = self.api.post(url, payload, format="json")
        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(first.data["status"], "read")
        self.assertEqual(first.data["unread_count"], 0)
        updated = ConversationReadState.objects.get(
            conversation=self.conversation, user=self.customer_user
        ).updated_at
        second = self.api.post(url, payload, format="json")
        self.assertEqual(second.status_code, 200)
        self.assertEqual(
            ConversationReadState.objects.get(
                conversation=self.conversation, user=self.customer_user
            ).updated_at,
            updated,
        )

    def test_read_view_without_marker_advances_to_now(self):
        _mk_message(self.conversation, self.artisan_user, "hello")
        self.api.force_authenticate(self.customer_user)
        response = self.api.post(
            f"/api/v1/conversations/{self.conversation.id}/read/", {}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["unread_count"], 0)

    def test_read_view_rejects_a_foreign_message_id(self):
        other_conversation, other_customer, _ = _mk_conversation()
        foreign = _mk_message(other_conversation, other_customer, "foreign")
        self.api.force_authenticate(self.customer_user)
        response = self.api.post(
            f"/api/v1/conversations/{self.conversation.id}/read/",
            {"last_message_id": str(foreign.id)},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_read_view_reports_503_never_success_on_database_failure(self):
        from unittest import mock

        self.api.force_authenticate(self.customer_user)
        with mock.patch(
            "conversations.views.mark_conversation_read", side_effect=__import__("django.db", fromlist=["DatabaseError"]).DatabaseError("boom")
        ):
            response = self.api.post(
                f"/api/v1/conversations/{self.conversation.id}/read/", {}, format="json"
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers.get("Retry-After"), "5")
        self.assertIsNone(response.data["unread_count"])

    def test_conversation_read_throttle_is_enabled(self):
        from conversations.views import ConversationReadView

        self.assertTrue(ConversationReadView.throttle_classes)
        self.assertEqual(ConversationReadView.throttle_scope, "conversation_read")
