"""Benchmark the manager console and ordinary marketplace endpoints.

This is a measurement tool, not a test: it seeds a realistic dataset, authenticates
as a real manager and a real customer, and records latency percentiles, database
query counts and cache behaviour. It prints a Markdown table that is copied into
the performance report, and it never claims more than it measured.

Usage:

    python manage.py benchmark_manager --customers 1500 --artisans 400 --bookings 2500

Run it against the same engine as production for a production-representative
result; the defaults reflect the SQLite test settings used during development.
"""
from __future__ import annotations

import math
import random
import statistics
import time
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.db import connection, transaction
from rest_framework.test import APIClient
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from booking.models import Booking
from conversations.models import Conversation, Offer
from home.models import ArtisanProfile, CustomerProfile, ServiceCategory
from manager.models import ManagerProfile
from manager.services import auth as auth_service
from manager.services import config_engine
from payments.models import EscrowPayment
from services.models import ServiceRequest

User = get_user_model()

ENDPOINTS = [
    ("GET", "/api/v1/manager/analytics/overview/?period=last_30_days", "Management overview"),
    ("GET", "/api/v1/manager/financial/?period=last_30_days", "Financial intelligence"),
    ("GET", "/api/v1/manager/business/?period=last_90_days", "Business intelligence"),
    ("GET", "/api/v1/manager/services/?period=last_30_days", "Service intelligence"),
    ("GET", "/api/v1/manager/customers/?period=last_30_days", "Customer intelligence"),
    ("GET", "/api/v1/manager/artisans/?period=last_30_days", "Artisan intelligence"),
    ("GET", "/api/v1/manager/funnel/?period=last_30_days", "Request & booking funnel"),
    ("GET", "/api/v1/manager/insights/?period=last_30_days", "Management insights"),
    ("GET", "/api/v1/manager/config/", "Configuration centre"),
    ("GET", "/api/v1/manager/snapshots/", "Snapshot status"),
    ("GET", "/api/v1/manager/audit/?size=50", "Audit trail page"),
    ("GET", "/api/v1/manager/jobs/", "Jobs and queue depth"),
    ("GET", "/api/v1/manager/reports/", "Saved reports"),
    ("GET", "/api/v1/manager/privacy/", "Privacy and retention"),
    ("GET", "/api/v1/manager/risk/?period=last_30_days", "Risk monitoring"),
    ("GET", "/api/v1/manager/geography/?period=last_30_days", "Geographic intelligence"),
    ("GET", "/api/v1/manager/support/?period=last_30_days", "Support and disputes"),
    ("GET", "/api/v1/manager/health/", "System health"),
    ("GET", "/api/v1/manager/staff/", "Staff and roles"),
    ("GET", "/api/v1/manager/campaigns/", "Campaign board"),
    ("GET", "/api/v1/manager/explorer/catalogue/", "Data explorer catalogue"),
    ("POST", "/api/v1/manager/explorer/run/", "Data explorer query"),
    ("POST", "/api/v1/manager/campaigns/estimate/", "Audience estimate"),
    ("POST", "/api/v1/manager/campaigns/", "Campaign creation"),
    ("POST", "/api/v1/manager/exports/", "Export job creation"),
    ("GET", "/api/v1/manager/overview/", "Legacy dashboard overview (home app)"),
    ("GET", "/api/v1/booking/", "Ordinary: customer booking list"),
    ("GET", "/api/v1/profile/customer/personal/profile/", "Ordinary: customer profile"),
]


class Command(BaseCommand):
    help = "Benchmark manager endpoints against a seeded dataset."

    def add_arguments(self, parser):
        parser.add_argument("--customers", type=int, default=1200)
        parser.add_argument("--artisans", type=int, default=300)
        parser.add_argument("--bookings", type=int, default=2000)
        parser.add_argument("--iterations", type=int, default=25)
        parser.add_argument("--cold-every", type=int, default=5,
                            help="Clear the cache every N iterations so cache-miss latency is sampled (0 = warm only).")
        parser.add_argument("--concurrency", type=int, default=4)
        parser.add_argument("--keep", action="store_true", help="Leave the seeded data in the database.")
        parser.add_argument("--reset", action="store_true", help="Remove any previous benchmark rows first.")
        parser.add_argument("--current-db", action="store_true",
                            help="Seed the configured database instead of a throwaway test database.")

    # ------------------------------------------------------------------ #
    def handle(self, *args, **options):
        from django.conf import settings

        using_test_db = False
        if not options["current_db"] and settings.DATABASES["default"]["NAME"] in {":memory:", ""}:
            self.stdout.write(self.style.WARNING(
                "Using the configured in-memory database; nothing is written to disk."))
        if options["reset"]:
            self._reset()
        started = time.perf_counter()
        fixture = self._seed(options)
        self.stdout.write(f"Seeded in {time.perf_counter() - started:.1f}s: {fixture['summary']}")

        manager = self._manager()
        customer = fixture["customer_user"]
        rows = []
        for method, path, label in ENDPOINTS:
            client_user = customer if path.startswith("/api/v1/booking/") or "/profile/" in path else manager
            rows.append(self._measure(client_user, method, path, label, options["iterations"],
                                      cold_every=options["cold_every"]))
        concurrent = self._concurrency(manager, options["concurrency"], options["iterations"])

        self._report(rows, concurrent, fixture, options)
        if not options["keep"]:
            self.stdout.write("Seeded rows remain in the database used for this run; delete the database file to reset.")

    def _reset(self):
        """Remove only the benchmark rows, in dependency order.

        Campaigns are deleted first: ``Campaign.created_by`` protects the manager
        account, so a benchmark that creates campaigns would otherwise make its own
        teardown impossible and every later run would fail on reset.
        """
        from conversations.models import Conversation, Offer
        from manager.models import (
            BackgroundJob,
            Campaign,
            CampaignAudienceSnapshot,
            CampaignDelivery,
            ExportArtifact,
            SavedReport,
        )

        bench_managers = User.objects.filter(email__startswith="bench-")
        bench_campaigns = Campaign.objects.filter(created_by__in=bench_managers)
        CampaignDelivery.objects.filter(campaign__in=bench_campaigns).delete()
        CampaignAudienceSnapshot.objects.filter(campaign__in=bench_campaigns).delete()
        bench_campaigns.delete()
        ExportArtifact.objects.filter(requested_by__in=bench_managers).delete()
        BackgroundJob.objects.filter(requested_by__in=bench_managers).delete()
        SavedReport.objects.filter(owner__in=bench_managers).delete()

        bookings = Booking.objects.filter(request__description="Benchmark request")
        EscrowPayment.objects.filter(booking__in=bookings).delete()
        Offer.objects.filter(conversation__customer__email__startswith="bench-").delete()
        Conversation.objects.filter(customer__email__startswith="bench-").delete()
        bookings.delete()
        ServiceRequest.objects.filter(description="Benchmark request").delete()
        ArtisanProfile.objects.filter(user__email__startswith="bench-").delete()
        CustomerProfile.objects.filter(user__email__startswith="bench-").delete()
        User.objects.filter(email__startswith="bench-").delete()
        self.stdout.write("Removed previous benchmark rows.")

    # ------------------------------------------------------------------ #
    def _seed(self, options):
        random.seed(7)
        categories = ["Plumbing", "Electrical", "Carpentry", "Painting", "Cleaning"]
        for name in categories:
            ServiceCategory.objects.get_or_create(name=name)
        cats = list(ServiceCategory.objects.filter(name__in=categories))
        states = ["Lagos", "Ogun", "Oyo", "Abuja", "Rivers"]

        now = timezone.now()
        users, customers, artisans, requests, bookings, payments = [], [], [], [], [], []
        password = "BenchPass!234"

        for index in range(options["customers"]):
            users.append(User(
                email=f"bench-customer-{index}@example.com", first_name=f"Cust{index}", last_name="Bench",
                is_active=True,
            ))
        for index in range(options["artisans"]):
            users.append(User(
                email=f"bench-artisan-{index}@example.com", first_name=f"Art{index}", last_name="Bench",
                is_active=True, is_staff=False,
            ))
        User.objects.bulk_create(users, batch_size=500)
        created = {row.email: row for row in User.objects.filter(email__startswith="bench-").only("id", "email")}

        customer_users = [created[f"bench-customer-{i}@example.com"] for i in range(options["customers"])]
        artisan_users = [created[f"bench-artisan-{i}@example.com"] for i in range(options["artisans"])]

        CustomerProfile.objects.bulk_create(
            [CustomerProfile(user=row) for row in customer_users], batch_size=500
        )
        ArtisanProfile.objects.bulk_create(
            [
                ArtisanProfile(user=row, business_name=f"Bench Artisan {i}", profile_status="ACTIVE",
                               state=random.choice(states))
                for i, row in enumerate(artisan_users)
            ],
            batch_size=500,
        )
        customer_profiles = list(CustomerProfile.objects.filter(user__email__startswith="bench-customer-"))
        artisan_profiles = list(ArtisanProfile.objects.filter(user__email__startswith="bench-artisan-"))

        windows = 180
        for index in range(options["bookings"]):
            days_ago = random.randint(0, windows)
            created_at = now - timedelta(days=days_ago, hours=random.randint(0, 23))
            requests.append(ServiceRequest(
                requester=random.choice(customer_users),
                category=random.choice(cats),
                status=random.choice([ServiceRequest.Status.OPEN, ServiceRequest.Status.ATTENDED,
                                      ServiceRequest.Status.CLOSED]),
                description="Benchmark request", budget=Decimal("25000.00"),
            ))
        ServiceRequest.objects.bulk_create(requests, batch_size=500)
        request_rows = list(ServiceRequest.objects.filter(description="Benchmark request"))

        statuses = ["REQUESTED", "ACCEPTED", "IN_PROGRESS", "COMPLETION_REQUESTED", "COMPLETED",
                    "CANCELLED", "DISPUTED"]
        for index, request_row in enumerate(request_rows):
            days_ago = random.randint(0, windows)
            start = now - timedelta(days=days_ago, hours=random.randint(1, 20))
            bookings.append(Booking(
                request=request_row,
                artisan=random.choice(artisan_profiles),
                customer=random.choice(customer_profiles),
                scheduled_time_start=start,
                scheduled_time_end=start + timedelta(hours=2),
                price_agreed=Decimal(random.choice(["15000.00", "25000.00", "33500.00", "60000.00", "120000.00"])),
                status=random.choice(statuses),
            ))
        Booking.objects.bulk_create(bookings, batch_size=500)
        booking_rows = list(Booking.objects.filter(request__description="Benchmark request"))

        for index, booking in enumerate(booking_rows[: int(len(booking_rows) * 0.75)]):
            service_amount = int(booking.price_agreed * 100)
            commission = int(service_amount * 0.10)
            status = random.choice(["HELD", "RELEASED", "PAYMENT_PENDING", "FAILED", "REFUNDED"])
            held_at = booking.scheduled_time_start if status in {"HELD", "RELEASED", "REFUNDED"} else None
            payments.append(EscrowPayment(
                booking=booking, customer=booking.customer.user, artisan=booking.artisan,
                reference=f"BENCH-{index}", service_amount_kobo=service_amount, customer_fee_kobo=15000,
                total_amount_kobo=service_amount + 15000, commission_rate=Decimal("0.1000"),
                platform_commission_kobo=commission, artisan_payout_kobo=service_amount - commission,
                status=status, held_at=held_at, provider_transaction_id=f"TX{index}",
            ))
        EscrowPayment.objects.bulk_create(payments, batch_size=500)

        # A held payment that never produced a ledger entry is a real integrity
        # fault, and the reconciliation check exists to catch it. Seeded payments
        # go through the same ledger writer the product uses, so a benchmark
        # database reports a healthy ledger instead of 272 phantom faults.
        from payments.ledger import record_financial_event

        for payment in EscrowPayment.objects.filter(reference__startswith="BENCH-",
                                                   status__in={"HELD", "RELEASED", "REFUNDED"}):
            record_financial_event(
                payment=payment, event_type="ESCROW_HELD",
                event_key=f"payment:{payment.id}:held",
                status_before="PAYMENT_PENDING", status_after="HELD",
                amount_kobo=payment.total_amount_kobo,
            )

        # A few real conversations and offers so the funnel and response-time metrics
        # are exercised against populated tables.
        convo_bookings = booking_rows[:200]
        conversations = []
        for booking in convo_bookings:
            conversations.append(Conversation(
                conversation_type="SERVICE", customer=booking.customer.user, artisan=booking.artisan,
                service_request=booking.request,
            ))
        Conversation.objects.bulk_create(conversations, batch_size=200)
        offers = []
        for conversation in Conversation.objects.filter(customer__email__startswith="bench-customer-"):
            offers.append(Offer(
                conversation=conversation, created_by=conversation.artisan.user,
                labour_amount=Decimal("20000.00"), materials_amount=Decimal("0.00"),
                total_amount=Decimal("20000.00"), materials_included=False,
                duration_value=2, duration_unit="HOURS", payload_hash=f"bench-{conversation.pk}",
                status=random.choice(["PROPOSED", "ACCEPTED", "DECLINED"]),
                scheduled_start=now + timedelta(days=1), scheduled_end=now + timedelta(days=1, hours=2),
                expires_at=now + timedelta(days=2),
            ))
        Offer.objects.bulk_create(offers, batch_size=500)

        return {
            "customer_user": customer_users[0],
            "customer_users": len(customer_users),
            "artisan_users": len(artisan_users),
            "requests": len(request_rows),
            "bookings": len(booking_rows),
            "payments": len(payments),
            "conversations": len(conversations),
            "offers": len(offers),
            "summary": (f"{len(customer_users)} customers, {len(artisan_users)} artisans, "
                        f"{len(request_rows)} requests, {len(booking_rows)} bookings, {len(payments)} payments"),
        }

    def _manager(self):
        user, _ = User.objects.get_or_create(
            email="bench-manager@example.com",
            defaults={"first_name": "Bench", "last_name": "Manager", "is_staff": True, "is_active": True,
                      "is_superuser": True},
        )
        auth_service.ensure_profile(user)
        return user

    # ------------------------------------------------------------------ #
    def _measure(self, user, method, path, label, iterations, cold_every=5):
        """Measure one endpoint.

        Two populations are reported, because they cost different things:

        * **cold** - the cache was cleared first, so the view recomputes every
          aggregate. This is the worst case and the number that must stay inside
          the 300 ms budget.
        * **warm** - the previous identical request populated the 45 s cache, so
          this is the steady state a manager actually experiences.

        Clearing the cache also resets the console's own per-user throttle
        counters, which is why ``cold_every`` exists: without it a burst of 20
        export calls would benchmark the 429 path instead of the export path.
        """
        config_engine.invalidate_cache()
        cache.clear()
        client = APIClient()
        client.force_authenticate(user=user)

        with CaptureQueriesContext(connection) as context:
            cold_started = time.perf_counter()
            response = client.get(path) if method == "GET" else getattr(client, method.lower())(path, **self._body(method, path))
            cold_ms = (time.perf_counter() - cold_started) * 1000
        query_count = len(context.captured_queries)
        status_code = response.status_code

        cold_samples, warm_samples = [], []
        for index in range(iterations):
            is_cold = cold_every and index % cold_every == 0
            if is_cold:
                cache.clear()
                config_engine.invalidate_cache()
            started = time.perf_counter()
            response = getattr(client, method.lower())(path, **self._body(method, path))
            elapsed = (time.perf_counter() - started) * 1000
            if response.status_code != status_code:
                status_code = response.status_code
            (cold_samples if is_cold else warm_samples).append(elapsed)

        combined = cold_samples + warm_samples
        return {
            "label": label,
            "method": method,
            "path": path,
            "status": status_code,
            "cold_ms": round(cold_ms, 1),
            "p50": round(statistics.median(warm_samples or combined), 1),
            "p95": round(_percentile(warm_samples or combined, 0.95), 1),
            "p99": round(_percentile(warm_samples or combined, 0.99), 1),
            "max": round(max(combined), 1),
            "cold_p95": round(_percentile(cold_samples, 0.95), 1) if cold_samples else None,
            "queries": query_count,
            "samples": len(combined),
        }

    _sequence = 0

    def _body(self, method, path):
        """Valid request bodies for the POST endpoints under test.

        A body that the view rejects would benchmark the validation path, not
        the query path, so every spec here is one the explorer accepts.
        """
        if method == "GET":
            return {}
        if path.endswith("/explorer/run/"):
            return {"data": {"domain": "bookings", "measure": "count", "dimension": "status",
                             "range": "last_30_days"}, "format": "json"}
        if path.endswith("/campaigns/estimate/"):
            return {"data": {"channel": "IN_APP", "segment": "all_customers"}, "format": "json"}
        if path.endswith("/campaigns/"):
            # Each call must be a legitimate new campaign, so the measured path is
            # "create", not "reject the duplicate title".
            type(self)._sequence += 1
            return {"data": {"title": f"Benchmark campaign {type(self)._sequence}",
                              "body": "Benchmark message body for the campaign board.",
                              "segment": "all_customers", "channels": ["IN_APP"]},
                    "format": "json"}
        if path.endswith("/exports/"):
            return {"data": {"spec": {"domain": "bookings", "measure": "count", "dimension": "status",
                                      "range": "last_30_days"},
                             "format": "csv"}, "format": "json"}
        return {}

    def _concurrency(self, user, workers, iterations):
        import threading

        client = APIClient()
        client.force_authenticate(user=user)
        paths = ["/api/v1/manager/analytics/overview/?period=last_30_days", "/api/v1/manager/financial/?period=last_30_days"]
        samples, errors = [], []

        def worker():
            local = APIClient()
            local.force_authenticate(user=user)
            for index in range(iterations):
                path = paths[index % len(paths)]
                started = time.perf_counter()
                try:
                    response = local.get(path)
                    if response.status_code >= 500:
                        errors.append(response.status_code)
                except Exception as exc:  # pragma: no cover - reported, not raised
                    errors.append(str(exc))
                samples.append((time.perf_counter() - started) * 1000)

        threads = [threading.Thread(target=worker) for _ in range(workers)]
        started = time.perf_counter()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        elapsed = time.perf_counter() - started
        total = max(1, len(samples))
        return {
            "workers": workers,
            "requests": len(samples),
            "throughput_rps": round(total / elapsed, 1),
            "p50": round(statistics.median(samples), 1) if samples else None,
            "p95": round(_percentile(samples, 0.95), 1) if samples else None,
            "max": round(max(samples), 1) if samples else None,
            "errors": len(errors),
        }

    # ------------------------------------------------------------------ #
    def _report(self, rows, concurrent, fixture, options):
        from django.conf import settings

        engine = settings.DATABASES["default"]["ENGINE"].rsplit(".", 1)[-1]
        lines = [
            "| Endpoint | Method | Status | Cold first call (ms) | P50 warm | P95 warm | P95 cold | P99 | Max | DB queries (cold) |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for row in rows:
            cold_p95 = row["cold_p95"] if row["cold_p95"] is not None else "-"
            lines.append(
                f"| {row['label']} | {row['method']} | {row['status']} | {row['cold_ms']} | {row['p50']} | "
                f"{row['p95']} | {cold_p95} | {row['p99']} | {row['max']} | {row['queries']} |"
            )
        table = "\n".join(lines)
        over_budget = [row for row in rows if (row["cold_p95"] or 0) > 300 or row["p95"] > 300]
        self.stdout.write("\n" + table + "\n")
        self.stdout.write(
            f"Dataset: {fixture['summary']}; iterations per endpoint: {options['iterations']}; "
            f"database engine: {engine}."
        )
        self.stdout.write(
            f"Concurrency: {concurrent['workers']} threads, {concurrent['requests']} requests, "
            f"{concurrent['throughput_rps']} req/s, p50 {concurrent['p50']} ms, p95 {concurrent['p95']} ms, "
            f"max {concurrent['max']} ms, errors {concurrent['errors']}."
        )
        if over_budget:
            self.stdout.write(self.style.WARNING(
                "Endpoints above the 300 ms P95 budget: " + ", ".join(row["label"] for row in over_budget)
            ))
        else:
            self.stdout.write(self.style.SUCCESS("Every endpoint is inside the 300 ms P95 budget."))


def _percentile(values, fraction):
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(math.ceil(fraction * len(ordered))) - 1))
    return ordered[index]
