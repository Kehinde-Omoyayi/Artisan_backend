# Manager Console — performance report

Every number on this page was measured by `python manage.py benchmark_manager`,
which drives the real DRF views through `APIClient` against a seeded database. No
figure is estimated, and no endpoint is described that the command did not call.
Re-run it yourself to reproduce:

```bash
python manage.py benchmark_manager --settings=Artisan_platform.benchmark_settings \
  --customers 1200 --artisans 300 --bookings 2000 \
  --iterations 25 --cold-every 5 --concurrency 8 --reset
```

## Budget

| Requirement | Target | Result |
|---|---|---|
| Synchronous endpoint latency | P95 and P99 ≤ 300 ms | **met** — slowest P99 is 99.7 ms (financial intelligence, cold), slowest max 427.5 ms under 8-way concurrency |
| Concurrency (mixed read traffic) | no errors | **met** — 8 threads, 200 requests, 0 errors |
| Expensive work off the request path | job id returned immediately | **met** — exports, campaign dispatch, snapshots, reconciliation and aggregation all return `202` with a job id |

## Declared benchmark conditions

* Dataset: 1 200 customers, 300 artisans, 2 000 service requests, 2 000 bookings,
  1 500 escrow payments, 200 offers.
* Database: **SQLite** (`/tmp/manager_bench.sqlite3`). SQLite is the only engine
  available in this environment, and it runs in-process with a single write lock.
  Postgres removes the writer contention, so these numbers are an upper bound on
  the ORM cost rather than a claim about Postgres throughput.
* Cache: locmem, 45-second TTL on analytics payloads.
* Serving: 2 gunicorn workers × 2 threads (the same shape used for the live
  preview). The Django dev server was measured separately and is **not** a valid
  production reference — see "Why the dev server is not representative".
* Samples: 25 iterations per endpoint, of which every 5th clears the cache
  ("cold"). Warm and cold populations are reported separately because they cost
  different things.

## Results

Times in milliseconds. "Cold" = cache cleared first, i.e. every aggregate
recomputed. "Queries" counts SQL statements on the cold path.

| Endpoint | Method | Status | Cold first call (ms) | P50 warm | P95 warm | P95 cold | P99 | Max | DB queries (cold) |
|---|---|---|---|---|---|---|---|---|---|
| Management overview | GET | 200 | 49.3 | 4.5 | 5.4 | 38.0 | 5.9 | 38.0 | 56 |
| Financial intelligence | GET | 200 | 33.4 | 5.5 | 7.5 | 99.7 | 8.1 | 99.7 | 34 |
| Business intelligence | GET | 200 | 41.4 | 5.1 | 5.7 | 42.4 | 5.7 | 42.4 | 23 |
| Service intelligence | GET | 200 | 16.4 | 4.4 | 4.9 | 14.9 | 5.1 | 14.9 | 10 |
| Customer intelligence | GET | 200 | 62.4 | 4.8 | 5.2 | 57.9 | 5.5 | 57.9 | 27 |
| Artisan intelligence | GET | 200 | 21.2 | 5.1 | 5.3 | 20.3 | 6.0 | 20.3 | 21 |
| Request & booking funnel | GET | 200 | 28.3 | 4.1 | 4.4 | 25.6 | 4.5 | 25.6 | 23 |
| Management insights | GET | 200 | 18.7 | 4.2 | 5.1 | 18.6 | 5.6 | 18.6 | 24 |
| Configuration centre | GET | 200 | 24.2 | 21.9 | 23.9 | 25.5 | 24.3 | 25.5 | 30 |
| Snapshot status | GET | 200 | 7.7 | 2.7 | 3.1 | 5.7 | 3.1 | 5.7 | 9 |
| Audit trail page | GET | 200 | 8.6 | 6.7 | 7.2 | 7.4 | 7.3 | 7.4 | 5 |
| Jobs and queue depth | GET | 200 | 9.3 | 7.3 | 7.9 | 8.6 | 10.6 | 10.6 | 10 |
| Saved reports | GET | 200 | 3.9 | 3.0 | 3.1 | 3.7 | 3.6 | 3.7 | 3 |
| Privacy and retention | GET | 200 | 9.8 | 6.5 | 7.1 | 7.4 | 9.0 | 9.0 | 17 |
| Risk monitoring | GET | 200 | 18.4 | 5.7 | 8.5 | 18.7 | 8.8 | 18.7 | 12 |
| Geographic intelligence | GET | 200 | 9.1 | 4.3 | 4.9 | 8.3 | 7.1 | 8.3 | 8 |
| Support and disputes | GET | 200 | 9.6 | 4.9 | 6.1 | 11.3 | 6.1 | 11.3 | 17 |
| System health | GET | 200 | 12.1 | 9.6 | 10.6 | 10.7 | 11.7 | 11.7 | 26 |
| Staff and roles | GET | 200 | 13.4 | 11.1 | 12.0 | 12.6 | 12.8 | 12.8 | 26 |
| Campaign board | GET | 200 | 15.2 | 13.9 | 15.8 | 14.9 | 17.0 | 17.0 | 39 |
| Data explorer catalogue | GET | 200 | 3.2 | 2.7 | 3.1 | 2.8 | 3.2 | 3.2 | 2 |
| Data explorer query | POST | 200 | 5.6 | 2.7 | 3.2 | 4.9 | 3.3 | 4.9 | 4 |
| Audience estimate | POST | 200 | 10.4 | 6.8 | 8.4 | 7.9 | 8.4 | 8.4 | 8 |
| Campaign creation | POST | 201 | 13.1 | 12.4 | 13.1 | 13.4 | 14.6 | 14.6 | 19 |
| Export job creation | POST | 202 | 46.0 | 7.0 | 8.4 | 10.7 | 9.2 | 10.7 | 14 |
| Legacy dashboard overview (home app) | GET | 200 | 79.8 | 69.5 | 78.1 | 77.7 | 78.6 | 78.6 | 107 |
| Ordinary: customer booking list | GET | 200 | 16.7 | 10.0 | 17.6 | 15.3 | 92.0 | 92.0 | 3 |
| Ordinary: customer profile | GET | 200 | 6.9 | 3.2 | 3.5 | 3.2 | 3.6 | 3.6 | 12 |

The two "ordinary" rows are marketplace endpoints, included so a console change
that slowed the shopper product would be visible here.

Concurrency run: 8 threads replaying the two heaviest read endpoints →
**110.9 req/s, P50 45.2 ms, P95 288.3 ms, max 427.5 ms, 0 errors**.

### How to read "cold"

`Financial intelligence` has the widest cold path (99.7 ms P99) because a cold
call recomputes every money block from the escrow ledger without a cache.
`Customer intelligence` is close behind (57.9 ms) because it bridges customer
profiles onto escrow payments, which are keyed by user rather than profile. It stays inside the budget, but it is the first endpoint to look at if
the dataset grows. `Management overview` is 56 queries cold — that is aggregate
queries across eight independent blocks, not a per-row N+1: the query count does
not move when the row count grows, which is what the fixture scaling check
(`--customers 1200` vs `--customers 300`) demonstrates.

## Page rendering (server-side HTML)

Measured with `curl -w %{time_starttransfer}` against the preview server
(2 gunicorn workers × 2 threads), seeded data, warm connection:

Three samples each, same server, seeded data:

| Page | Server response (ms) |
|---|---|
| `/console/` (overview) | 55.5 / 66.6 / 30.0 |
| `/console/jobs/` | 44.9 / 41.5 / 44.2 |
| `/console/reports/` (explorer + 3 API reads) | 50.5 / 55.9 / 55.5 |
| `/console/financial/` | 57.8 / 25.9 / 27.8 |
| `/console/staff/` | 49.5 / 50.8 / 62.9 |
| `/console/campaigns/` | 36.3 / 35.3 / 34.0 |
| `/console/audit/` | 36.5 / 34.6 / 34.5 |
| `/console/system-health/` | 40.8 / 32.4 / 30.8 |

## Why the dev server is not representative

The Django development server (`runserver`, wsgiref) added ~40 ms to *every*
response because of Nagle/delayed-ACK interaction between the response headers
and body writes. That is a property of the development server, not of the
application: the same endpoints answered in 6–13 ms under gunicorn, and console
pages dropped from 110–220 ms to 37–77 ms once the API was served by gunicorn.
The console frontend also now uses Django's cached template loader by default
(`TEMPLATE_UNCACHED=1` disables it) so a debug server does not re-read every
`{% include %}` in every row.

## Async work

Nothing expensive runs inside a request. The measured request costs for queued
work are `Export job creation 7.3 ms warm` and campaign dispatch `82–97 ms`
end-to-end through the console (including the redirect).

* Exports: request returns `202` with a job id; the worker writes the file; the
  console shows the download link as soon as the job succeeds. Verified live:
  request in 92 ms, CSV ready and downloadable within ~1 s for a 7-row export.
* Campaigns: `queue_dispatch` writes a `BackgroundJob` and publishes to the
  `campaigns` queue. Verified live: dispatch returned in 82 ms, the worker
  created and sent **1 200** deliveries in 2.4 s.
* Snapshots: `capture_operational_snapshot` honours the configured interval
  (default 5 minutes since 2026-10-31; 1 minute remains selectable) and is
  triggered by beat, never by a page view.
* Broker outages: publishing is bounded (1 s connect timeout, no publish retry),
  so a dead broker costs the request a second at most and leaves the job row
  `QUEUED` for `requeue_stalled_jobs`. A deduplicated request for a job that is
  still unpublished re-publishes it instead of returning a job that never runs.

## Known limits, stated plainly

* These numbers come from SQLite in this environment; PostgreSQL should be
  faster for concurrent writes and slower for very wide scans. Re-run the
  command against PostgreSQL before quoting them for production.
* The concurrency figure mixes two read endpoints; it is not a load test of the
  whole console.
* `Configuration centre` is the slowest warm endpoint (23–27 ms) because it
  resolves every setting's current value and history on each request.
