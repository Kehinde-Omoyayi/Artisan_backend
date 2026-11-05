FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DJANGO_SETTINGS_MODULE=frontend.settings

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system artisan \
    && useradd --system --gid artisan --home-dir /app artisan

COPY requirements.txt ./requirements.txt
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt

COPY --chown=artisan:artisan . .
RUN mkdir -p /app/staticfiles /app/media /var/lib/artisan-market \
    && chown -R artisan:artisan /app /var/lib/artisan-market \
    && chmod +x /app/docker-entrypoint.sh

USER artisan
EXPOSE 8000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["gunicorn", "frontend.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "60"]
