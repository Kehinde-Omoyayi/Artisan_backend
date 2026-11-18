web: gunicorn Artisan_platform.wsgi:application --bind 0.0.0.0:${PORT:-8001}
# Every queue routed in CELERY_TASK_ROUTES has its own consumer, so a slow or
# failing workload can never occupy another workload's worker. The notifications
# worker owns all outbound email; a wedged SMTP socket must only ever block
# email, never payments, bookings or bank transfers. Shared knobs (acks_late,
# prefetch=1, reject-on-worker-lost, time limits) are preserved in settings.
worker-payments: celery -A Artisan_platform worker -l info -Q payments --concurrency=2 -Ofair --max-tasks-per-child=200 --hostname=payments@%h
worker-verification: celery -A Artisan_platform worker -l info -Q verification --concurrency=4 -Ofair --max-tasks-per-child=200 --hostname=verification@%h
worker-notifications: celery -A Artisan_platform worker -l info -Q notifications --concurrency=4 -Ofair --max-tasks-per-child=200 --hostname=notifications@%h
worker-media: celery -A Artisan_platform worker -l info -Q media --concurrency=2 -Ofair --max-tasks-per-child=200 --hostname=media@%h
worker-maintenance: celery -A Artisan_platform worker -l info -Q maintenance --concurrency=2 -Ofair --max-tasks-per-child=200 --hostname=maintenance@%h
worker-manager: celery -A Artisan_platform worker -l info -Q manager --concurrency=2 -Ofair --max-tasks-per-child=200 --hostname=manager@%h
worker-exports: celery -A Artisan_platform worker -l info -Q exports --concurrency=1 -Ofair --max-tasks-per-child=200 --hostname=exports@%h
worker-campaigns: celery -A Artisan_platform worker -l info -Q campaigns --concurrency=2 -Ofair --max-tasks-per-child=200 --hostname=campaigns@%h
worker-default: celery -A Artisan_platform worker -l info -Q celery --concurrency=2 -Ofair --max-tasks-per-child=200 --hostname=default@%h
beat: celery -A Artisan_platform beat -l info
