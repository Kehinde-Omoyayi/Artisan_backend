"""Read-only fail-closed production configuration report, not live acceptance."""
from django.conf import settings
from django.core.management.base import BaseCommand,CommandError
from conversations.models import ConversationAttachment,MediaRelocation
from conversations.purge_ledger import verify_database_identity,verify_ledger_integrity
from conversations.readiness import require_production_readiness


class Command(BaseCommand):
    help='Check configured recording prerequisites without enabling recordings or modifying storage.'

    def handle(self,*args,**options):
        checks=[]
        def check(label,fn):
            try:
                outcome=fn()
                if outcome is False:raise ValueError()
                checks.append((label,True))
            except Exception:checks.append((label,False))
        check('No legacy public-origin attachment backlog',lambda:not ConversationAttachment.objects.filter(private_storage=False).exists())
        check('No incomplete relocation journals',lambda:not MediaRelocation.objects.exists())
        check('External ledger integrity',verify_ledger_integrity)
        check('Database/external identity agreement',verify_database_identity)
        for flag in ('CONVERSATION_MEDIA_BACKUP_POLICY_CONFIRMED','FLW_CHARGEBACK_WEBHOOKS_CONFIRMED','CONVERSATION_MEDIA_REPLICA_INVENTORY_CONFIRMED'):
            check(flag,lambda flag=flag:bool(getattr(settings,flag,False)))
        check('Production readiness, primary/replica private-access configuration',require_production_readiness)
        for label,passed in checks:self.stdout.write(('PASS: ' if passed else 'BLOCKED: ')+label)
        self.stdout.write('Read-only code/configuration checks. Not a real provider, restore, CDN, mobile-device or capacity acceptance test. Recordings were not enabled.')
        if not all(passed for _,passed in checks):raise CommandError('Recording prerequisites are incomplete. Keep recordings disabled and resolve the blocked checks.')
