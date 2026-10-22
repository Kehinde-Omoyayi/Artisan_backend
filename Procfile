web: gunicorn Artisan_platform.wsgi:application --bind 0.0.0.0:${PORT:-8001}
worker: celery -A Artisan_platform worker -l info -Q celery,verification,maintenance,notifications,media,payments
beat: celery -A Artisan_platform beat -l info
