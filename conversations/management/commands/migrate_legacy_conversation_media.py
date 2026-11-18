"""Report by default; apply only through the existing checksum-verified journal."""
from django.core.management.base import BaseCommand
from conversations.models import ConversationAttachment, MediaRelocation
from conversations.legacy_media import migrate_attachment


class Command(BaseCommand):
    help = 'Report legacy attachments; --apply migrates bounded batches without deleting unverified source copies.'

    def add_arguments(self,parser):
        parser.add_argument('--apply',action='store_true')
        parser.add_argument('--limit',type=int,default=500)

    def handle(self,*args,**options):
        qs=ConversationAttachment.objects.filter(private_storage=False).order_by('pk')
        self.stdout.write(f'Legacy attachments remaining: {qs.count()}; retry journals: {MediaRelocation.objects.count()}')
        if not options['apply']:
            self.stdout.write('Report only. No database or storage objects changed. Use --apply after reviewed storage/CDN preflight.')
            return
        migrated=failed=0
        for pk in list(qs.values_list('pk',flat=True)[:max(1,min(options['limit'],5000))]):
            try:
                if migrate_attachment(pk)=='migrated':migrated+=1
            except Exception:
                # Do not print object keys, filenames, URLs or provider exception data.
                failed+=1
        self.stdout.write(f'Migrated: {migrated}; retry required: {failed}; remaining: {qs.count()}')
        if failed:
            from django.core.management.base import CommandError
            raise CommandError('Some copies need retry/review. Existing sources and retry journals were not forcibly removed.')
