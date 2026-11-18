"""Phase 2 regression: incremental ``since`` sync on the messages endpoint.

The conversation room used to re-fetch the newest 60 messages on every beat
and re-render from scratch. ``GET .../messages/?since=<iso>`` returns only
what changed — new messages at or after the instant, plus tombstoned messages
deleted at or after it — so the browser can merge by message id instead of
downloading the whole window. These tests pin the contract:

* the bound is inclusive (never skips a same-microsecond insert);
* tombstones for older messages are included even though ``created_at`` is
  older than the cursor — delete-for-everyone still propagates;
* ordering stays newest-first;
* a malformed cursor is a 400, never a silent full scan;
* naive datetimes are accepted (interpreted as UTC);
* omitting ``since`` is byte-for-byte the old behaviour.
"""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from rest_framework.test import APIClient

from conversations.models import Message
from conversations.test_read_marker import _mk_conversation, _mk_message


class MessagesSinceApiTests(TestCase):
    def setUp(self):
        self.api = APIClient()
        self.conversation, self.customer_user, self.artisan_user = _mk_conversation()
        self.url = f"/api/v1/conversations/{self.conversation.id}/messages/"
        self.api.force_authenticate(self.customer_user)

    def _fetch(self, **params):
        return self.api.get(self.url, params, format="json")

    def test_since_returns_only_messages_at_or_after_the_instant(self):
        _mk_message(self.conversation, self.artisan_user, "old one", minutes_ago=10)
        cutoff = timezone.now() - timedelta(minutes=5)
        newer = _mk_message(self.conversation, self.artisan_user, "fresh")
        response = self._fetch(since=cutoff.isoformat())
        self.assertEqual(response.status_code, 200, response.content)
        bodies = [item["body"] for item in response.data["results"]]
        self.assertIn("fresh", bodies)
        self.assertNotIn("old one", bodies)

    def test_since_is_inclusive_at_the_exact_cursor_instant(self):
        message = _mk_message(self.conversation, self.artisan_user, "exact")
        response = self._fetch(since=message.created_at.isoformat())
        self.assertEqual(response.status_code, 200, response.content)
        ids = [item["id"] for item in response.data["results"]]
        self.assertIn(str(message.id), ids)

    def test_since_surfaces_a_tombstone_for_an_older_message(self):
        old = _mk_message(self.conversation, self.customer_user, "delete me", minutes_ago=30)
        cutoff = timezone.now() - timedelta(minutes=1)
        # Tombstone the old message *after* the cursor: created_at is far
        # older, deleted_at is new, so the incremental sync must see it.
        Message.objects.filter(pk=old.pk).update(deleted_at=timezone.now())
        response = self._fetch(since=cutoff.isoformat())
        self.assertEqual(response.status_code, 200, response.content)
        ids = [item["id"] for item in response.data["results"]]
        self.assertIn(str(old.id), ids)

    def test_since_keeps_newest_first_ordering(self):
        first = _mk_message(self.conversation, self.artisan_user, "first", minutes_ago=3)
        second = _mk_message(self.conversation, self.artisan_user, "second", minutes_ago=2)
        cutoff = timezone.now() - timedelta(minutes=10)
        response = self._fetch(since=cutoff.isoformat())
        self.assertEqual(response.status_code, 200, response.content)
        created = [item["created_at"] for item in response.data["results"]]
        self.assertEqual(created, sorted(created, reverse=True))
        self.assertIn(str(first.id), [item["id"] for item in response.data["results"]])
        self.assertIn(str(second.id), [item["id"] for item in response.data["results"]])

    def test_since_rejects_a_malformed_cursor(self):
        response = self._fetch(since="not-a-date")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "INVALID_CURSOR")

    def test_since_accepts_a_naive_datetime(self):
        _mk_message(self.conversation, self.artisan_user, "hello")
        naive = (timezone.now() - timedelta(minutes=5)).replace(tzinfo=None).isoformat()
        response = self._fetch(since=naive)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertGreaterEqual(len(response.data["results"]), 1)

    def test_omitting_since_returns_the_full_page_unchanged(self):
        _mk_message(self.conversation, self.artisan_user, "one", minutes_ago=2)
        _mk_message(self.conversation, self.artisan_user, "two", minutes_ago=1)
        response = self._fetch()
        self.assertEqual(response.status_code, 200, response.content)
        bodies = [item["body"] for item in response.data["results"]]
        self.assertEqual(bodies, ["two", "one"])
