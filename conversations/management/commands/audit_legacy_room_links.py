"""Read-only audit against applied historical models, before new columns exist."""
import importlib
from django.core.management.base import BaseCommand,CommandError
from django.db import connections
from django.db.migrations.loader import MigrationLoader


class Command(BaseCommand):
    help='Report historical ownership ambiguity before applying request-link migrations. Does not modify data.'

    def add_arguments(self,parser):
        parser.add_argument('--database',default='default')

    def handle(self,*args,**options):
        alias=options['database'];connection=connections[alias]
        loader=MigrationLoader(connection)
        applied=[node for node in loader.applied_migrations if node in loader.graph.nodes]
        state=loader.project_state(nodes=applied)
        try:Room=state.apps.get_model('conversations','Conversation')
        except LookupError:
            self.stdout.write('No applied conversation model to audit. No data changed.');return
        required={'conversation_type','customer','artisan','service_request','draft','participants'}
        if not required.issubset({f.name for f in Room._meta.get_fields()}):
            raise CommandError('This schema predates the supported participant migration. Review/apply earlier cumulative migrations first.')
        classify=importlib.import_module('conversations.migrations.0008_mediarelocation_and_more').legitimate_roles
        issues=business=peers=0
        for room in Room.objects.using(alias).all().iterator():
            try:
                if classify(room,state.apps,alias) is None:peers+=1
                else:business+=1
            except RuntimeError as exc:
                issues+=1;self.stdout.write(str(exc))
        self.stdout.write(f'Business rooms: {business}; unrelated/confirmed peer rooms: {peers}; review required: {issues}. No data changed.')
        if issues:raise CommandError('Resolve the reported ownership ambiguity before migrating. Do not invent ownership or erase history.')
