"""Initialize the durable identity receipt without replacing deletion history."""
import fcntl
import os
from pathlib import Path
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from conversations.models import MediaPolicyIdentity
from conversations.purge_ledger import ledger_path, verify_ledger_integrity


class Command(BaseCommand):
    help = 'Initialize/verify the external media restore receipt; never reset an existing ledger.'

    def handle(self, *args, **options):
        path = ledger_path()
        if path is None:
            raise CommandError('Configure durable CONVERSATION_PURGE_LEDGER_PATH first.')
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink():
            raise CommandError('The ledger must not be a symbolic link.')
        receipt = Path(str(path) + '.identity')
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os,'O_NOFOLLOW',0), 0o600)
            with os.fdopen(fd, 'r+') as ledger:
                verify_ledger_integrity()
                fcntl.flock(ledger, fcntl.LOCK_EX)
                with transaction.atomic():
                    identity, _ = MediaPolicyIdentity.objects.get_or_create(pk=1)
                    identity = MediaPolicyIdentity.objects.select_for_update().get(pk=identity.pk)
                    expected = str(identity.identity)
                    if receipt.exists() or receipt.is_symlink():
                        if receipt.is_symlink() or receipt.stat().st_mode & 0o077:
                            raise CommandError('The identity receipt must be a private regular file.')
                        if receipt.read_text().strip() != expected:
                            raise CommandError('Restore identity mismatch; reviewed reconciliation is required. Nothing was overwritten.')
                    else:
                        if path.stat().st_size:
                            raise CommandError('Existing nonempty ledger has no receipt; reviewed reconciliation is required. Nothing was overwritten.')
                        receipt_fd = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os,'O_NOFOLLOW',0), 0o600)
                        with os.fdopen(receipt_fd, 'w') as out:
                            out.write(expected + '\n');out.flush();os.fsync(out.fileno())
                        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os,'O_DIRECTORY',0))
                        try:os.fsync(directory_fd)
                        finally:os.close(directory_fd)
        except CommandError:
            raise
        except Exception as exc:
            raise CommandError('Media identity initialization failed; preserve the existing ledger and receipt for review.') from exc
        self.stdout.write(self.style.SUCCESS('External media restore identity verified; no tombstones removed.'))
