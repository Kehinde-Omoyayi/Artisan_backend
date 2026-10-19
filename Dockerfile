FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DJANGO_SETTINGS_MODULE=Artisan_platform.api_settings

WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN python -m pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /app/staticfiles /app/media \
    && chown -R app:app /app
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live/', timeout=4)" || exit 1
CMD ["gunicorn", "Artisan_platform.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "60"]
