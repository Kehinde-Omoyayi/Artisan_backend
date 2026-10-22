"""Exercise historical backfill against the migration graph, never live data."""

from django.test import TransactionTestCase
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


class HistoricalRoomMigrationTests(TransactionTestCase):
    before = [
        ("conversations", "0007_remove_offer_offer_total_matches_parts_and_more"),
        ("services", "0008_servicerequest_closed_at_and_more"),
    ]

    def setUp(self):
        self.executor = MigrationExecutor(connection)
        self.leaves = self.executor.loader.graph.leaf_nodes()
        self.executor.migrate(self.before)
        self.old = self.executor.loader.project_state(self.before).apps
        User = self.old.get_model("home", "CustomUser")
        Customer = self.old.get_model("home", "CustomerProfile")
        Artisan = self.old.get_model("home", "ArtisanProfile")
        self.customer = User.objects.create(
            email="historical-owner@example.invalid",
            first_name="Real",
            last_name="Owner",
            password="test-hash",
            is_active=True,
        )
        Customer.objects.create(user_id=self.customer.pk)
        self.artisan_user = User.objects.create(
            email="historical-artisan@example.invalid",
            first_name="Real",
            last_name="Artisan",
            password="test-hash",
            is_active=True,
        )
        self.artisan = Artisan.objects.create(user_id=self.artisan_user.pk)

    def tearDown(self):
        MigrationExecutor(connection).migrate(self.leaves)
        super().tearDown()

    def test_links_legitimate_history_preserving_peers_messages_and_owners(self):
        Room = self.old.get_model("conversations", "Conversation")
        Message = self.old.get_model("conversations", "Message")
        User = self.old.get_model("home", "CustomUser")
        legacy = Room.objects.create(
            customer_id=self.customer.pk,
            artisan_id=self.artisan.pk,
            conversation_type="SERVICE",
        )
        msg = Message.objects.create(
            conversation_id=legacy.pk,
            sender_id=self.customer.pk,
            body="Keep the original history.",
        )
        direct = Room.objects.create(
            conversation_type="DIRECT", direct_key="historical-pair"
        )
        direct.participants.add(self.customer.pk, self.artisan_user.pk)
        peer = User.objects.create(
            email="peer@example.invalid",
            first_name="Peer",
            last_name="User",
            password="test-hash",
            is_active=True,
        )
        generic = Room.objects.create(
            conversation_type="DIRECT", direct_key="unrelated-peers"
        )
        generic.participants.add(self.customer.pk, peer.pk)
        executor = MigrationExecutor(connection)
        executor.migrate(self.leaves)
        apps = executor.loader.project_state(self.leaves).apps
        NewRoom = apps.get_model("conversations", "Conversation")
        Request = apps.get_model("services", "ServiceRequest")
        for pk in (legacy.pk, direct.pk):
            room = NewRoom.objects.get(pk=pk)
            req = Request.objects.get(pk=room.service_request_id)
            self.assertEqual(room.customer_id, self.customer.pk)
            self.assertEqual(room.conversation_type, "SERVICE")
            self.assertEqual(req.requester_id, self.customer.pk)
            self.assertEqual(req.status, "DRAFT")
        self.assertEqual(NewRoom.objects.get(pk=generic.pk).conversation_type, "DIRECT")
        self.assertEqual(
            apps.get_model("conversations", "Message").objects.get(pk=msg.pk).body,
            "Keep the original history.",
        )
        self.assertEqual(Request.objects.count(), 2)
        self.assertEqual(
            Request.objects.values("media_policy_id").distinct().count(), 2
        )

    def test_corrupt_ownerless_service_room_stops_migration_instead_of_inventing_owner(
        self,
    ):
        Room = self.old.get_model("conversations", "Conversation")
        bad = Room.objects.create(conversation_type="SERVICE")
        try:
            with self.assertRaisesRegex(RuntimeError, "no legitimate customer owner"):
                MigrationExecutor(connection).migrate(self.leaves)
        finally:
            Room.objects.filter(pk=bad.pk).delete()
