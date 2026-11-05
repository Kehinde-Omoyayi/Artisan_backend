"""Additional checks for the explicitly disclosed Drop 38 reconstruction."""
import importlib
import io
import json
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from .models import Conversation, MediaPolicyIdentity, Message
from .tests import ConversationTestMixin


class RecoveryCommandTests(TestCase, ConversationTestMixin):
    def setUp(self):
        self.customer=self.make_user('rebuild-owner@example.invalid')
        self.artisan_user,self.artisan=self.make_artisan('rebuild-artisan@example.invalid')
        self.room=self.make_conversation()
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'ledger'
        self.settings_override=override_settings(ENVIRONMENT='production',CONVERSATION_PURGE_LEDGER_PATH=str(self.path))
        self.settings_override.enable();self.addCleanup(self.settings_override.disable)

    def initialize(self):
        call_command('initialize_media_policy',stdout=io.StringIO())

    def test_initialize_preserves_tombstones_and_identity_on_retry(self):
        self.initialize()
        identity=Path(str(self.path)+'.identity').read_bytes()
        tombstone=json.dumps({'request_id':self.room.service_request_id,'request_identity':str(self.room.service_request.media_policy_id)})+'\n'
        self.path.write_text(tombstone)
        self.initialize()
        self.assertEqual(self.path.read_text(),tombstone)
        self.assertEqual(Path(str(self.path)+'.identity').read_bytes(),identity)

    def test_mismatched_receipt_is_never_overwritten(self):
        self.initialize()
        receipt=Path(str(self.path)+'.identity');receipt.write_text(str(uuid.uuid4()))
        before=receipt.read_bytes()
        with self.assertRaises(CommandError):self.initialize()
        self.assertEqual(receipt.read_bytes(),before)

    def test_nonempty_ledger_without_receipt_requires_review(self):
        self.path.write_text('{"request_id":7}\n');self.path.chmod(0o600)
        with self.assertRaises(CommandError):self.initialize()
        self.assertEqual(self.path.read_text(),'{"request_id":7}\n')
        self.assertFalse(Path(str(self.path)+'.identity').exists())

    def test_corrupt_ledger_is_not_truncated_or_silently_repaired(self):
        self.path.write_text('corrupt ledger');self.path.chmod(0o600)
        with self.assertRaises(CommandError):self.initialize()
        self.assertEqual(self.path.read_text(),'corrupt ledger')

    def test_report_only_legacy_media_command_has_no_storage_side_effect(self):
        with patch('conversations.management.commands.migrate_legacy_conversation_media.migrate_attachment') as migrate:
            output=io.StringIO();call_command('migrate_legacy_conversation_media',stdout=output)
        migrate.assert_not_called()
        self.assertIn('Report only',output.getvalue())

    def test_preflight_fails_closed_and_does_not_initialize_ledger(self):
        with self.assertRaises(CommandError):call_command('conversation_media_preflight',stdout=io.StringIO())
        self.assertFalse(self.path.exists())

    def test_room_audit_is_read_only(self):
        before=(Conversation.objects.count(),Message.objects.count())
        output=io.StringIO();call_command('audit_legacy_room_links',stdout=output)
        self.assertIn('No data changed',output.getvalue())
        self.assertEqual(before,(Conversation.objects.count(),Message.objects.count()))

    def test_synthetic_historical_quote_numbering_changes_only_version(self):
        from types import SimpleNamespace
        migration=importlib.import_module('conversations.migrations.0010_offer_materials_details_offer_version')
        updates=[]
        quotes=[SimpleNamespace(pk=1,conversation_id=10),SimpleNamespace(pk=2,conversation_id=10),SimpleNamespace(pk=3,conversation_id=20)]
        class Query:
            def using(self,alias):return self
            def order_by(self,*fields):return self
            def iterator(self):return iter(quotes)
            def filter(self,**filters):self.pk=filters['pk'];return self
            def update(self,**kwargs):updates.append((self.pk,kwargs))
        class Offer:objects=Query()
        class Apps:
            def get_model(self,*args):return Offer
        migration.number_historical_quotes(Apps(),SimpleNamespace(connection=SimpleNamespace(alias='default')))
        self.assertEqual(updates,[(1,{'version':1}),(2,{'version':2}),(3,{'version':1})])
