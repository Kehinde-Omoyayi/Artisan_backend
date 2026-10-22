import os
from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "Artisan_platform.api_settings")

app = Celery("Artisan_platform")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

app.conf.beat_schedule = {
    "dispatch-outbox-every-minute": {
        "task": "core.tasks.dispatch_outbox_events",
        "schedule": crontab(),
    },
    "purge-core-retention-every-day": {
        "task": "core.tasks.purge_core_retention",
        "schedule": crontab(hour=3, minute=0),
    },
    "reconcile-stale-payments-every-five-minutes": {
        "task": "payments.tasks.reconcile_stale_payments",
        "schedule": 300,
    },
    "reconcile-promotion-campaigns-every-minute": {
        "task": "promotions.tasks.reconcile_promotion_campaigns",
        "schedule": crontab(),
    },
    "purge-promotion-performance-dedupe-every-day": {
        "task": "promotions.maintenance_tasks.purge_performance_dedupe_rows",
        "schedule": crontab(hour=3, minute=30),
    },
    "reconcile-financial-ledger-every-day": {
        "task": "payments.tasks.reconcile_financial_ledger",
        "schedule": crontab(hour=5, minute=0),
    },
    "delete-unverified-accounts-every-day": {
        "task": "home.tasks.delete_unverified_accounts",
        "schedule": crontab(hour=2, minute=0),
    },
    "purge-expired-staff-security-challenges-every-day": {
        "task": "home.tasks.purge_expired_staff_security_challenges",
        "schedule": crontab(hour=3, minute=15),
    },
    "dispatch-manager-analytics-snapshots-every-minute": {
        "task": "home.tasks.dispatch_manager_snapshot_if_due",
        "schedule": crontab(),
    },
    "expire-conversation-offers-every-minute": {
        "task": "conversations.tasks.expire_conversation_offers",
        "schedule": crontab(),
    },
    "reconcile-recording-retention-every-minute": {
        "task": "conversations.tasks.reconcile_recording_retention",
        "schedule": crontab(),
    },
    "conversation-reminders-every-30-minutes": {
        "task": "conversations.tasks.send_conversation_reminders",
        "schedule": 1800,
    },
}
