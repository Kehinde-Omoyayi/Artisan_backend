# Manager Console — completion report

Every line below is either **Implemented** (code exists and was exercised) or
**Partial** (something specific is missing) or **Not implemented** (nothing
shipped). Where a claim rests on a test or a measurement, the evidence is named.
Nothing here is described as compliant, certified or complete without evidence
attached.

Test/measurement commands used, and their results on the final code:

| Command | Result |
|---|---|
| `manage.py test --settings=Artisan_platform.test_settings manager` | **82 tests, OK** |
| `manage.py test` (frontend) | **80 tests, OK** |
| `manage.py test --settings=… booking conversations payments notifications` | **174 tests, 2 failures + 3 errors**, all pre-existing (see §23) |
| `manage.py benchmark_manager … --reset` | all 28 endpoints within the 300 ms P95/P99 budget (see `docs/MANAGER_CONSOLE_PERFORMANCE.md`) |
| `console_flow_check.py` (live, HTTP, both servers) | **21/21 flows** |
| `shoot_console.py` (Playwright, 22 pages) | **0 problems** across 22 pages |

---

## 1. Manager authentication — password + OTP + active account + permissions

**Implemented.** `manager/services/auth.py` + `ManagerAPIView`.

* Password checked against the Django hasher; failures feed
  `ManagerLoginThrottle` (per-account and per-IP rows in the database, not the
  cache, so a cache flush cannot reset an attacker's allowance).
* Second factor: TOTP (RFC 6238, 6 digits, 30 s step, ±1 window) with
  **single-use enforcement** — `totp_last_used_step` is stored and any step at or
  below it is refused. Email fallback via `ManagerOTPChallenge` (only an HMAC
  digest is stored, never the code).
* Active account: `_is_active_manager()` requires a `ManagerProfile` with
  `is_suspended=False` and `user.is_active`.
* Permissions: every view declares `required_permission` and the base class
  refuses without it; superusers get the synthesised `super-admin` role.

**Evidence:** `manager.tests.test_access` (18 tests) — including
`test_a_totp_code_cannot_be_reused`,
`test_a_wrong_email_code_is_rejected_and_the_right_one_works_once`,
`test_an_expired_email_code_is_rejected`,
`test_the_step_up_code_reaches_the_manager`,
`test_a_suspended_manager_loses_access_immediately`.

## 2. OTP expiry, single use, replay protection, rate limits, no plaintext OTPs

**Implemented.** 5-minute TTL, `consumed_at`/`invalidated_at`, attempt counter
that invalidates after `OTP_MAX_ATTEMPTS`, per-scope throttle, keyed HMAC digest
for storage, and the code is passed to the email worker **encrypted**
(`home.security_mail`), never logged.

A defect was found and fixed here during this work: the delivery task was called
with a challenge id that did not exist, so a manager without an authenticator
never received a code. Fixed: `manager/tasks.send_manager_step_up_code` re-reads
the real challenge and refuses to send for a consumed/expired one (test:
`test_the_step_up_code_reaches_the_manager`).

## 3. Step-up verification for sensitive actions

**Implemented.** `StepUpGrant` rows store only a hashed token, scoped, TTL-bounded
and consumed on first use. Scopes: `config:finance`, `config:approve`,
`refund:approve`, `payout:approve`, `staff:manage`, `privacy:manage`,
`export:sensitive`, `system:manage`.

The console **parks the interrupted request in the session and replays it once
verification succeeds** (`frontend/console_views._park_pending_action` /
`resume_pending_action`), so a manager does not retype a fee change after
verifying. A second verification never re-fires a stale action
(`test_a_second_step_up_does_not_replay_a_stale_action`).

An unreadable (e.g. rotated-key) authenticator secret now returns a 403 with an
actionable message instead of a 500
(`test_an_unreadable_authenticator_secret_is_a_clear_refusal_not_a_crash`).

**Verified live:** sensitive config change, retention change and snapshot capture
each redirected to step-up, then completed after the emailed code was entered
(21/21 in `console_flow_check.py`).

## 4. Session controls, revocation, CSRF

**Implemented.** `ManagerSession` rows are listed on the overview with a revoke
action; `POST /manager/sessions/<id>/revoke/`. The console is server-rendered
Django forms behind `CsrfViewMiddleware`; every POST template carries
`{% csrf_token %}`. Session cookies are Django's, not the API JWT.

**Partial:** there is no idle-timeout auto-logout beyond Django's
`SESSION_COOKIE_AGE`, and no "sign out everywhere" button (revoking is per
session).

## 5. Granular RBAC — 9 recommended roles + custom, deny by default, endpoint + object level, dual approval, no self-escalation

**Implemented.**
* 9 system roles (`manager/services/staff.py SYSTEM_ROLES`): Super Administrator,
  General Manager, Finance Manager, Operations Manager, Customer Support Agent,
  Risk & Compliance Officer, Marketing Manager, Data & Analytics Officer, System
  Administrator. Custom roles are supported (`is_system=False`).
* 43 permissions in 8 categories, each with a human label.
* Deny by default: a view without `required_permission` still requires an active
  manager; a missing permission is a 403.
* Object level: exports are readable only by the manager who requested them
  unless the role holds `export.sensitive`; staff rows cannot be edited by the
  subject themselves; the last super-admin cannot be removed.
* Dual approval: a config change or campaign must be approved by a **different**
  manager (`test_a_financial_change_needs_a_second_approver`,
  and the live run shows `creator cannot approve their own campaign`).
* No self-escalation: a manager cannot change their own role or grant themselves
  a permission.

**Verified live:** the non-superuser `campaign-approver` account reached
`/console/campaigns/`, saw the Approve control, approved another manager's
campaign and could not approve their own.

## 6. No-code configuration centre — versioning, effective dates, financial preview, historical integrity, rollback as a new version

**Implemented.** 26 typed keys in `manager/config_spec.py` with category,
permission, type and validation. `PlatformConfigVersion` is append-only;
`ConfigChangeRequest` carries `effective_from`; `preview()` shows the financial
effect before saving; `approve` writes a new version rather than mutating the old
one; `rollback` creates a *new* version pointing at an old value; accepted offers
keep the terms they were priced with (`test_new_bookings_use_the_new_rule_and_old_ones_keep_theirs`,
`test_future_effective_date_does_not_apply_yet`).

A mistyped key now returns 400 with the key name instead of a 500
(`test_a_mistyped_configuration_key_is_a_client_error`).

## 7. Analytics pages — the 14 required areas

**Implemented (14 sections, all reaching real API data):** overview, financial,
business, service, customer, artisan, funnel, marketing, geography, insights,
data quality, support & disputes, risk, system health. Plus reports/data
explorer, campaigns, staff, configuration, privacy, audit and jobs.

Each section is a distinct page with its own endpoint and its own metric set; the
rendering is data-driven with no hardcoded figures
(`test_the_dashboard_never_renders_a_figure_the_api_did_not_send`).

## 8. Metric taxonomy and filters

**Implemented.** Counts, distincts, ratios with numerator/denominator,
percentiles (P50/P95/P99 on health), quartiles and medians (financial), cohorts
and retention (customer retention = customers with >1 booking ÷ customers with any
booking), conversion (funnel), growth (period-over-period, `None` when there is no
baseline), moving averages (daily series), rankings (service and artisan tables).

Filters: all named presets plus a custom start/end range, category, location,
booking status and payment status, all server-side, with a hard 366-day cap
(`test_over_range_is_rejected_instead_of_scanning_everything`).

**Partial:** the filter bar exposes date, category, location and status but not
campaign or segment on the analytics pages — campaign filters exist inside the
Campaign board's audience builder only.

## 9. Configurable snapshot engine, 1-minute default

**Implemented.** `SnapshotIntervalSetting` + `capture()` writes immutable rows
keyed `(metric_key, captured_for, interval_seconds)` — one row per metric per
bucket, never overwritten. Supported intervals: 1/2/5/10/15/30 min, 1/6/12/24 h.
Each row carries `dimensions`, `captured_at`, `calc_version="v1"` and
`duration_ms`. `capture_if_due()` makes the beat tick cheap, so the cadence is
enforced by the setting rather than by the scheduler.

**Verified live:** beat + worker produced 17 metric rows per bucket, and
`/console/jobs/` reports 0 missing slots once the scheduler ran.

**Default cadence is 5 minutes (300 s), changed on 2026-10-31 on your
instruction.** It is set in three places that must agree — `config_spec` default,
`SnapshotIntervalSetting.interval_seconds`, and the `or 300` fallback in
`snapshots.interval_seconds()` — with migration `manager/0004`. One minute is
still selectable on `/console/jobs/`; only the shipped default moved.

**Partial:** "custom" interval is a supported list plus a validator, not an
arbitrary free integer.

## 10. Data Explorer — domains, dimensions, chart types, saved/shared/scheduled reports, exports

**Implemented.** 10 domains (bookings, payments, payouts, refunds, requests,
offers, customers, artisans, reviews, support) each exposing whitelisted measures
and dimensions — **no free-form SQL anywhere**. Output includes a chart
hint, `MAX_ROWS=500` for the screen and `MAX_EXPORT_ROWS=50_000` for a file.
Reports can be saved, named, shared with other managers, re-run and deleted.
Exports are queued jobs, not request-time work.

**Partial:** scheduled report delivery (a report that emails itself on a cron)
is not implemented — `run_report_job` exists for on-demand report generation, not
for recurring delivery. Of the requested chart types, the console renders KPI
cards, bars, tables, funnels and line/area series; pie, donut, scatter,
histogram, box, heatmap, map, cohort matrix and pivot are **not** drawn.

## 11. Campaign Board — no-code editor, audience builder, consent, creation ≠ dispatch, bounded batches, retries, dead letters, analytics

**Implemented.** Title/subject/body/CTA/channels/segment/schedule form. Audience
estimate returns estimated, eligible, excluded, opted-out, invalid-contact and
recently-contacted counts computed on the server; the recipient list never leaves
the backend. Consent is enforced through `ContactPreference` (opt-out) and a
48-hour frequency cap. Create → `PENDING_APPROVAL` → a *different* manager
approves → dispatch is a separate, logged action. Delivery runs in bounded
batches on the `campaigns` queue with per-delivery status, error text and
retry/watchdog tasks. Analytics: queued, sent, failed, skipped, opens, clicks,
click rate — where an "open" is a recipient reading the in-app notification
(`notifications.in_app` calls `campaigns.mark_open`), never a guess.

**Verified live:** dispatch returned in 82 ms; the worker created and sent **1 200**
deliveries in 2.4 s; a fully suppressed campaign now explains itself on the board
("1200 recipients were suppressed…"), which was a real gap found during review.

**Partial:** attributed requests / bookings / revenue per campaign and ROI are
**not** implemented — attribution is not wired to the booking records, so the ROI
column the spec asks for does not exist. Push is accepted as a channel but has no
provider wired (like SMS, which requires `notifications.sms_enabled`).

## 12. Support centre and dispute management

**Implemented (read + case actions).** Support metrics: tickets opened, open,
under review, resolved, escalated, average resolution hours, first response hours,
by category and by agent; disputes with value in dispute and refunded value.
Case assignment/response/resolution run through the existing `support` app, and
refunds go through the existing refund flow with `refund.approve` + step-up.

**Partial:** there is no dedicated dispute *timeline* UI inside the console; the
manager is deep-linked to the existing support case pages rather than managing
evidence in the console.

## 13. Risk monitoring — no automatic permanent suspension

**Implemented.** `RiskReview` rows with a decision (`risk.view` / `risk.manage`),
`POST /manager/risk/reviews/<id>/`. Suspension requires a human decision plus a
reason; nothing in the codebase suspends an account automatically from a risk
signal (grep for suspension writes shows only the staff/customer/artisan manage
endpoints, all human-triggered).

**Partial:** the appeal workflow is a recorded decision and a note on the review,
not a customer-facing appeal form with its own SLA.

## 14. Privacy — classification, least privilege, no secrets/PII in logs or exports, encryption, retention, DSR, incident response, NDPR

**Implemented.** Data classes with retention policies (analytics events,
operational snapshots, audit log, export files) configurable in the console and
purged by a daily task. DSR workflow: `DataSubjectRequest` with reference,
type, status and an audit trail. Exports exclude identity fields unless the role
holds `export.sensitive`, which additionally demands step-up. Audit entries
record actor, action, resource and metadata — no OTPs, passwords or tokens are
written to any log or event (event properties are `_clean()`-ed before storage).
Secrets come from configuration, never from source.

**Partial / stated honestly:** this implements *specific* controls; it is **not** a
certification of NDPR compliance. There is no DPIA document, no documented
breach-notification runbook with named owners and timelines, and no
"encryption at rest" claim beyond what the database/filesystem provides. Media
retention uses the existing conversation retention tasks plus the export TTL
(72 h) — there is no per-media-type lifecycle table.

## 15. Security — OWASP-style controls, validation, upload security, rate limits, secrets, test matrix

**Implemented.** Parameterised queries only (the ORM); no free-form SQL surface;
server-side whitelisting of explorer domains/measures/dimensions; ranged inputs;
CSRF on every console form; login and write throttles; staff boundary that blocks
shopper endpoints for staff accounts; step-up on sensitive writes; audit of every
manager write; no secrets/OTPs/PII in logs (the OTP path encrypts and never logs).

**Partial:** upload security in the console is inherited from the existing
conversation media pipeline (magic-byte checks, size limits) — the console itself
accepts no file uploads. There is no independent penetration-test report and no
CSP beyond the existing `SECURE_CSP` setting being off by default.

## 16. Database and queue engineering — indexes, no N+1, bounded ranges, transactions, idempotency, cache scoping, queue isolation, worker monitoring

**Implemented.** Explicit indexes for every hot access path (event name/time,
entity, snapshot key/time, throttle scope/key, delivery campaign/status,
profile role/suspended). Bounded ranges (≤366 days) and bounded page sizes.
`transaction.atomic()` around money and campaign writes. Idempotency via
`IdempotencyRecord` for payments and `dedupe_key` for jobs and analytics events.
Cache keys include a permission fingerprint *and* the filter set, so one role can
never read another's cached payload
(`test_cache_is_keyed_by_permissions_and_filters`). Queue isolation: `exports`
and `campaigns` are separate queues with their own worker pool, so a campaign
fan-out cannot delay an export or a payment. `/console/jobs/` shows queue depth,
oldest queued age, average and P95 job duration and recent jobs.

**Known and documented:** the overview is **56 queries cold** — that is aggregate
queries across eight independent blocks, not a per-row N+1 (the count does not
grow with the row count), but it is not "no N+1" in the absolute sense and this
report does not claim it is.

A lost-broker-message case was found and fixed: a queued job whose publish failed
used to sit forever while every new request deduplicated onto it. `enqueue()` now
re-publishes a deduplicated job that is still unpublished after 10 s
(`test_a_lost_broker_message_is_republished_instead_of_stuck`).

## 17. Management Insights Centre — rule-based, transparent, no causal claims

**Implemented.** Rules emit metric, comparison period, observed values, method,
freshness and a suggested action; the page prints the method for each insight and
acknowledgement is recorded. No insight asserts causation, and none fires without
the two figures behind it.

**Partial:** the forecasting-ready architecture is "async only, not built" — no
forecast job or model is shipped.

## 18. System Health page with a separate permission

**Implemented.** Latency percentiles (P50/P95/P99/max) with sample counts, error
rate, per-endpoint breakdown, database/cache/storage/outbox/payments/notifications/
workers/campaigns/analytics status, queue depth and job durations. It is gated by
`system.health`, which a support or marketing role does not hold — so health is
separable from financial and customer data.

## 19. Mandatory automated tests

**Implemented.** 82 backend tests and 80 frontend tests, all passing. They cover
access control, step-up, config engine arithmetic and history, analytics
correctness, campaign lifecycle, exports, staff, privacy, health, the browser
pages (render, no leaked template syntax, no empty CSS values, no outbound HTTP
during a render, no "empty" lie while rows exist) and the write flows.

## 20. Sixteen implementation phases, each leaving the app working

**Partial.** The work was delivered as one continuous build with the suite green
at the end, not as 16 separately tagged, individually deployable phases. Honest
version: the app works at the end of this work and at each point where a test run
was taken, but there is no per-phase tag or changelog giving 16 restart points.

## 21. Performance target measured and documented

**Implemented.** `docs/MANAGER_CONSOLE_PERFORMANCE.md` reports P50/P95/P99/max,
throughput, error rate and DB query counts for all 28 endpoints under declared
conditions, plus the page-render timings. All endpoints are inside the 300 ms
budget; the concurrency run is 113 req/s with 0 errors. The document states the
SQLite caveat rather than hiding it.

## 22. Nothing expensive blocks a normal request

**Implemented.** Exports, campaign dispatch, snapshot capture, reconciliation,
aggregation and data-quality runs all return a job id (`202`) and run on a worker.
Publishing is bounded to ~1 s so a dead broker costs the request a second at
most, and the job stays `QUEUED` for the watchdog.

## 23. Documented vs delivered — the honest gaps

* **Pre-existing failures left alone:** `conversations.test_recording_policy` (3
  errors, requires the `ffmpeg` binary) and two payout-provider tests. They were
  failing before this work for environmental reasons and were not "fixed" by
  weakening them.
* **Not built at all:** campaign attribution/ROI, scheduled report delivery,
  forecast jobs, the appeal workflow, and the nine chart types listed in §10.
* **Campaign opens only covers in-app.** `opened_at` is written when the
  recipient actually reads the campaign notification in their inbox — the only
  real read signal the platform has. Email opens (tracking pixel) and SMS
  delivery receipts are not collected, so an email-only campaign will show
  0 opens and that is correct rather than missing.
* **No compliance certification.** NDPR is addressed control-by-control; no
  external audit or DPIA exists, so no compliance claim is made.
* **Phase-by-phase delivery** is not evidenced (§20).
* **Real-world caveat:** everything is measured on SQLite in this environment.
  Re-run `benchmark_manager` against PostgreSQL before quoting latency for
  production.
* **Two runtime defects found while running the finished system, and fixed:**
  1. Nothing consumed Celery's default queue. Manager step-up codes were fine
     (they use the `manager` queue) but *staff sign-in* codes went to the default
     queue, so a fresh browser could never complete sign-in. The deployment now
     runs two workers: `-Q manager,exports,campaigns` for bulk work and
     `-Q celery` for transactional mail and payment callbacks, which also keeps a
     campaign fan-out from delaying a sign-in code.
  2. A campaign could be created with an audience segment that does not exist;
     the KeyError surfaced later in the dispatch worker. `create()` now refuses an
     unknown segment up front, where the manager can see it.
  3. An export job redelivered by Celery crashed on the one-to-one artefact
     constraint. `build_export()` now returns the existing artefact and marks the
     job succeeded, so at-least-once delivery is safe.
  4. The jobs table printed a job's progress *count* with a percent sign, so a
     finished 1 200-delivery dispatch read "1 200%". The API now sends a bounded
     percentage and the page prints that. The export list had the same defect.
  5. Three console pages printed a value the API does not send, or a template
     default that hides the truth: the staff page read `results` while the API
     sends `staff` (so it claimed there were no managers), the reports owner
     column was a hardcoded dash, and an insight with no baseline printed
     "Change: None%". All three now show what the backend actually returned.
  6. `REDIS_URL` resolved to `localhost`, which prefers IPv6 on this host; every
     publish spent ~2 s retrying `::1` before falling back. Pointing it at
     `127.0.0.1` took campaign dispatch from 2 090 ms to 110 ms. Recorded here
     because it is exactly the kind of "works in dev" delay the 300 ms budget
     exists to catch.

---

### Where the evidence lives

| Evidence | Location |
|---|---|
| Backend behaviour | `manager/tests/` (5 modules, 82 tests) |
| Console pages | `frontend/tests/test_manager_console.py`, `frontend/tests/console_payloads.py` |
| Performance | `docs/MANAGER_CONSOLE_PERFORMANCE.md`, `manager/management/commands/benchmark_manager.py` |
| Live HTTP flows | `/home/user/console_flow_check.py` (21 checks) |
| Page screenshots / leak scan | `/home/user/shoot_console.py`, `/home/user/console_shots/` |
| Seed accounts for a walkthrough | `/home/user/seed_console_demo.py`, `/home/user/make_console_sessions.py` |
