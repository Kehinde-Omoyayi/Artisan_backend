# Artisan Market — standalone frontend

A server-rendered Django frontend for the Artisan Market. This project owns the browser UI and Django sessions; it **does not include or replace the marketplace API**. Account identity, catalogue records, service requests, conversations, bookings, payments, notifications, support cases, permissions, and manager analytics remain API/database-owned.

The frontend communicates with the separate API over HTTP using `ARTISAN_API_BASE_URL`. Marketplace pages do not use a bundled/sample production database. The only local database is Django's session store; test-only API responses live in the isolated automated tests.

## Requirements

- Python 3.10 or newer (Python 3.12 recommended)
- The separate Artisan Market API, with its own configured database, running and reachable
- Docker/Compose only if using the optional container workflow

## Local setup

Run the commands from this directory (the one containing `manage.py`):

```bash
python -m venv .venv
# macOS/Linux
. .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` before starting. By default the API is expected at `http://127.0.0.1:8000` and the frontend will use its own port, `8001`:

```dotenv
ARTISAN_API_BASE_URL=http://127.0.0.1:8000/api/v1
ARTISAN_API_ROOT_URL=http://127.0.0.1:8000
FRONTEND_SESSION_DB=./frontend_sessions.sqlite3
```

Start the API separately first. Then initialize the frontend's **session-only** database and run checks:

```bash
python manage.py migrate
python manage.py check
python manage.py test
python manage.py runserver 0.0.0.0:8001
```

Open `http://127.0.0.1:8001`. For development, Django's static-file server serves the checked-in CSS, JavaScript, fonts, and local image assets. No `collectstatic` step is required for `DEBUG=true`.

The `.env.example` file uses a clearly marked development-only key. Replace it for any shared or deployed environment. Do not copy API, Google, Apple, email, payment-provider, or Django production secrets into source files or this ZIP.

## Configuration reference

| Variable | Purpose |
| --- | --- |
| `ENVIRONMENT` | `development` or `production`; production settings enforce secure defaults and validate key/API configuration. |
| `DEBUG` | Django debug mode. Must be `false` in production. |
| `SECRET_KEY` | Unique Django signing/session key. Use a random value of at least 50 characters in production. |
| `ALLOWED_HOSTS` | Comma-separated hostnames serving the frontend. |
| `CSRF_TRUSTED_ORIGINS` | Comma-separated full origins, including scheme, for cross-origin trusted frontend hosts. |
| `ARTISAN_API_BASE_URL` | API prefix, normally `https://api.example.com/api/v1`; include `/api/v1`. |
| `ARTISAN_API_ROOT_URL` | API origin for root-level operational endpoints such as `/health/`. |
| `ARTISAN_API_TIMEOUT` | Outbound API request timeout in seconds. |
| `FRONTEND_SESSION_DB` | SQLite file path for Django sessions. It stores browser session state and API bearer tokens, **not** marketplace records. Keep it private and persistent. |
| `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`, `SECURE_SSL_REDIRECT` | Secure transport settings. Production defaults are enabled; keep them on behind HTTPS. |
| `SECURE_HSTS_SECONDS` | Production must set an explicit positive HSTS duration only after HTTPS coverage and the domain scope are approved. The example value `0` deliberately leaves this decision pending, and production settings reject it until configured. |
| `TRUST_PROXY_SSL_HEADER` | Set `true` only behind a trusted reverse proxy that overwrites `X-Forwarded-Proto`. |
| `LOG_LEVEL` | Python root log level. |

`python-decouple` reads the local `.env` file and process environment. Process environment values take precedence. Only `.env.example` is included in the package.

## Frontend/API ownership and main flows

- **Customer requests:** category, service, and location choices come from the API catalogue; create/edit submissions use the API's `service_name` and nested existing-location reference format.
- **Responders and chats:** eligible artisans explicitly respond first. Customers can compare the API-returned responder profiles and start/resume a conversation with the chosen artisan. More than one conversation may stay open until the booking workflow finalizes selection.
- **Artisan request inbox:** the API returns only requests matched to active, verified, unrestricted services and the artisan's eligible area. The UI forwards category/service, location, urgency, preferred date, budget, and sort filters, and posts response/withdraw actions back to the API.
- **Support and disputes:** case messages and dispute submission are sent to customer support through the API. Staff operations remain separate from manager analytics.
- **Manager analytics:** the manager view is shown only after the support-capabilities API confirms `is_superuser`; the frontend does not trust a role in the Django session for that access decision. API permissions remain authoritative.
- **Artisan activation:** a restricted manager-only page submits activation changes to the API. It is not added to customer/artisan navigation; API permissions remain authoritative.
- **Notifications and email:** the frontend calls the existing API notification routes. Configure SMTP/provider credentials only in the API's environment; this package contains no mail secrets.
- **Google/Apple sign-in:** this app is a browser bridge to the API's social-auth endpoints. Configure provider credentials, allowed redirect URIs, and callback origins in the API and provider consoles. Keep provider secrets and token-verification configuration on the API server.

## Messages and customer request assistant

- The Messages inbox renders participant names, message previews, local-time timestamps, and the API-provided unread-message count. Unread state continues to follow the API's per-user read watermark.
- Customer service requests use a deterministic, rule-based helper in the conversation stream. It uses natural-language messages and the API's service/location catalogues; it does not call or claim to be a live AI service.
- Date/time and optional budget controls add a reviewed reply to the chat composer. The customer must send it for the existing API workflow to update the unsent request draft.
- The full request editor remains available in a collapsed review section. Saving a draft is separate from submitting a request, and submission is reported only after the API confirms the created request. Preferred dates/times are not booking confirmations.

## Automated tests

```bash
python manage.py test
```

The included frontend tests use a test-only fake API client to verify request catalog mapping, customer responder/chat selection, artisan filtering, and response/withdraw actions. They do not create fixtures in the production API or load sample marketplace data.

## Production deployment

1. Set environment variables through the deployment platform's secret/configuration manager. At minimum set:
   - `ENVIRONMENT=production`
   - `DEBUG=false`
   - a fresh 50+ character `SECRET_KEY`
   - the real frontend `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS`
   - an HTTPS `ARTISAN_API_BASE_URL` and `ARTISAN_API_ROOT_URL`
   - secure cookies and HTTPS redirect (`SESSION_COOKIE_SECURE=true`, `CSRF_COOKIE_SECURE=true`, `SECURE_SSL_REDIRECT=true`)
   - an explicitly approved positive `SECURE_HSTS_SECONDS` after HTTPS and domain coverage are confirmed; the example value `0` intentionally prevents production startup until that decision is made
2. Provide a durable, private location for `FRONTEND_SESSION_DB`. It contains session material and must not be committed, exposed through static/media hosting, or copied into an artifact. Back it up only under your data-retention/security policy. For multiple app replicas, replace the default SQLite session database with a shared production database/session backend.
3. Run migrations and collect static files during release/build:

   ```bash
   python manage.py migrate --noinput
   python manage.py collectstatic --noinput
   ```

4. Serve the WSGI application behind HTTPS with a production server, for example:

   ```bash
   gunicorn Frontend.wsgi:application --bind 0.0.0.0:8000 --workers 2 --timeout 60
   ```

The included `Dockerfile`, `docker-entrypoint.sh`, and `compose.yaml` are an optional single-container deployment path. The entrypoint runs Django migrations and `collectstatic` before Gunicorn. Copy `.env.example` to `.env`, configure a real production key and HTTPS API URL, and set `DOCKER_ARTISAN_API_BASE_URL` / `DOCKER_ARTISAN_API_ROOT_URL` to the API address as reachable **from the container**. Compose persists only the frontend session database in a named volume; it does not start or include the API.

When using a reverse proxy, trust forwarded scheme headers only if that proxy strips any client-supplied version and sets its own. Configure proxy host/HTTPS headers consistently to avoid redirect loops. Do not expose the SQLite session file, `.env`, or debug pages publicly.

## Package contents and exclusions

This project includes Django settings, WSGI/ASGI entry points, URL routing, templates, local static design assets, environment examples, run/deployment configuration, and tests. It intentionally excludes:

- the API/backend project and its database;
- live session/database files, accounts, test inspection credentials, and production marketplace data;
- secret values and provider/payment/email credentials;
- generated staticfiles, Python caches, virtual environments, and build output.
