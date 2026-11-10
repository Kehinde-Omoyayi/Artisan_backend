# Artisan Market API — standalone backend

This package runs the frontend-free Django API service. It includes the API apps, migrations, Django admin/security pages, health endpoints, and API documentation. It intentionally does **not** contain either browser frontend, their templates, or their static assets. The standalone Django UI is delivered separately and calls this API over HTTP.

## Local setup

Use Python 3.12 or newer. Copy the example file and replace local-only values as appropriate; it contains placeholders only:

```bash
python -m venv .venv
# macOS/Linux: . .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env
```

For development, configure a local Redis-compatible service for cache/pub-sub and a local PostgreSQL or SQLite database. The example selects SQLite and console email; Redis is still required for full live behavior. Provider values in the example are non-live placeholders. Do not use them for real payments or identity verification. Generate a local KYC encryption key before exercising encrypted identity flows:

```bash
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Set the resulting key only in your local, ignored `.env`. Keep production keys, provider credentials, and production `.env` files out of source archives.

The API package's `manage.py`, WSGI, ASGI, Celery, and Docker entry points default to `Artisan_platform.api_settings`, which mounts the API routes and protected Django admin—not the legacy integrated UI URLconf.

```bash
python manage.py check
python manage.py runserver 0.0.0.0:8000
```

The standalone frontend expects `ARTISAN_API_BASE_URL=http://127.0.0.1:8000/api/v1` by default. Configure exact allowed hosts, HTTPS origins, proxy trust, HSTS, CORS, email, database, Redis, object storage, and provider settings for your own deployment. Production hostname/origin-dependent security choices are intentionally not guessed here.

## Safe tests

The API package includes test settings that override provider values with test-only dummies, generate an ephemeral Fernet key, and use in-memory SQLite, LocMem cache, and LocMem email. They do not call live providers or use a persistent database:

```bash
python -m pip install -r requirements-dev.txt
python manage.py check --settings=Artisan_platform.test_settings
python manage.py test --settings=Artisan_platform.test_settings
python manage.py test Artisan_platform.test_api_only conversations.tests risk.tests --settings=Artisan_platform.test_settings
```

## Database and uploads

### Institution register

The institution list every picker, filter and promotion reads lives in the `home.Institution` table. The published register ships as JSON under `catalogue/data/institutions/`; the migration `home.0017` loads the default country (Nigeria, 342 rows) into the table, so a migrated database is never empty. Load more countries, or refresh the rows, with:

```bash
python manage.py seed_institutions                    # Nigeria, the default
python manage.py seed_institutions --country all      # the whole register, ~30,000 rows
python manage.py seed_institutions --dry-run          # count only, write nothing
```

It is safe to re-run: rows are matched on `source_id` and anything already present is left alone (`--update` refreshes name, campus and aliases).

### Password hashing cost

`PASSWORD_HASHERS` uses Argon2id at OWASP's minimum recommended cost (19 MiB, two passes, one lane: about 25 ms a hash here, against ~165 ms for Django's stock 100 MiB setting, which is most of a slow sign-in on a laptop). Raise it on a production server without touching code:

```dotenv
ARGON2_MEMORY_COST=65536
ARGON2_TIME_COST=3
ARGON2_PARALLELISM=2
```

Existing hashes keep working: Django re-hashes each password with the current settings at that user's next successful sign-in.

No database, customer records, uploads, or live secrets are included in this archive. Migrations are included as code. Run `python manage.py migrate` only after selecting and authorizing the target database. Configure `MEDIA_ROOT`/object storage separately; do not copy existing uploads into this package.

`Dockerfile` builds only the API service and does not auto-run migrations. The deployment operator must configure services and explicitly authorize any migration or deployment step.
