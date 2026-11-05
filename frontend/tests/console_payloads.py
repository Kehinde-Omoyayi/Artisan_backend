"""Payload fixtures for the Manager Console page tests.

Every payload here mirrors the **shape the manager API actually returns**, so the
console templates are exercised against the same keys they see in production.
The figures are small hand-written values: these fixtures exist to prove the
rendering contract, not to be a second source of truth for analytics.

Metric dictionaries are built by :func:`metric` so a template that starts reading
a key the API never sends fails here first, instead of rendering a blank.
"""
from __future__ import annotations

RANGE = {
    "key": "last_30_days",
    "label": "Last 30 days",
    "start": "2026-10-01T00:00:00+00:00",
    "end": "2026-10-31T00:00:00+00:00",
    "previous_start": "2026-09-01T00:00:00+00:00",
    "previous_end": "2026-10-01T00:00:00+00:00",
    "days": 30,
}

FRESHNESS = {
    "events_last_recorded_at": "2026-10-30T09:15:00+00:00",
    "snapshot_last_captured_at": "2026-10-30T09:14:00+00:00",
    "computed_at": "2026-10-30T09:16:00+00:00",
    "cache_ttl_seconds": 45,
}


def metric(label, value, *, unit="number", definition="Definition supplied by the API.",
           previous=None, change_percent=None, numerator=None, denominator=None):
    payload = {"label": label, "value": value, "unit": unit, "definition": definition}
    if previous is not None:
        payload["previous"] = previous
        payload["change_percent"] = change_percent
    if numerator is not None:
        payload["numerator"] = numerator
        payload["denominator"] = denominator
    return payload


def ratio_metric(label, numerator, denominator, value, definition):
    return metric(label, value, unit="percent", definition=definition,
                  numerator=numerator, denominator=denominator)


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #
SESSION = {
    "is_manager": True,
    "user_id": "1",
    "name": "Console Manager",
    "email": "console.manager@example.com",
    "role": {"slug": "super-admin", "name": "Super Administrator"},
    "permissions": [
        "analytics.view", "analytics.financial", "analytics.customer", "analytics.artisan",
        "analytics.build", "config.view", "config.finance", "config.approve", "campaign.view",
        "campaign.create", "campaign.approve", "campaign.send", "staff.view", "staff.manage",
        "privacy.view", "privacy.manage", "risk.view", "risk.manage", "support.view",
        "system.health", "audit.view", "export.data", "export.sensitive",
    ],
    "totp_enrolled": True,
    "email_otp_enabled": True,
    "last_authenticated_at": "2026-10-30T08:00:00+00:00",
    "step_up_ttl_seconds": 600,
    "permissions_catalogue": [
        {"code": "analytics.view", "label": "View analytics", "category": "analytics"},
        {"code": "config.finance", "label": "Change financial rules", "category": "configuration"},
    ],
    "step_up_scopes": {
        "config:finance": "Change a money rule",
        "config:approve": "Approve a configuration change",
        "export:sensitive": "Export sensitive data",
    },
    "sessions": [],
    "server_time": "2026-10-30T09:16:00+00:00",
    "security": {"totp_enrolled": True, "email_otp_enabled": True,
                 "must_change_password": False, "last_authenticated_at": "2026-10-30T08:00:00+00:00"},
}

# --------------------------------------------------------------------------- #
# Analytics pages
# --------------------------------------------------------------------------- #
OVERVIEW = {
    "range": RANGE,
    "generated_at": "2026-10-30T09:16:00+00:00",
    "totals": {"customers": 1200, "artisans": 300, "verified_artisans": 240, "active_artisans": 260},
    "period": {
        "new_customers": metric("New customers", 120, previous=99, change_percent=21.2,
                                definition="Customer profiles created inside the range."),
        "bookings": metric("Bookings created", 340, previous=300, change_percent=13.3,
                           definition="Bookings created inside the range."),
        "paid_value_kobo": metric("Paid booking value", 697_125_000, unit="kobo",
                                  definition="Sum of captured customer payments inside the range."),
        "commission_kobo": metric("Commissions", 69_495_000, unit="kobo",
                                  definition="Platform commission retained from captured payments."),
    },
    "bookings_by_status": [
        {"status": "REQUESTED", "label": "Requested", "count": 278, "percent": 13.9},
        {"status": "COMPLETED", "label": "Completed", "count": 273, "percent": 13.7},
    ],
    "escrow": {"held_kobo": 1_416_980_000, "artisan_payable_kobo": 1_271_610_000,
               "pending_payouts_kobo": 0, "released_kobo": 336_500_000, "refund_liability_kobo": 0},
    "ratios": {
        "booking_conversion": ratio_metric("Booking conversion", 49, 200, 24.5,
                                           "Accepted offers divided by offers created."),
        "completion_rate": ratio_metric("Completion rate", 273, 2000, 13.7,
                                        "Completed bookings divided by bookings created."),
    },
    "attention": {
        "unpaid_bookings": {"label": "Bookings awaiting payment", "value": 12,
                            "definition": "Bookings created in range without a captured payment."},
        "overdue_services": {"label": "Services overdue", "value": 3,
                             "definition": "In-progress bookings past their scheduled end."},
    },
    "data_freshness": FRESHNESS,
    "served_from_cache": False,
}

FINANCIAL = {
    "range": RANGE,
    "generated_at": "2026-10-30T09:16:00+00:00",
    "kpis": {
        "successful_payment_kobo": metric("Captured payments", 697_125_000, unit="kobo",
                                          previous=500_000_000, change_percent=39.4,
                                          definition="Provider-confirmed escrow holds inside the range."),
        "escrow_liability_kobo": metric("Current escrow liability", 1_416_980_000, unit="kobo",
                                        definition="Customer money currently held."),
        "commission_revenue_kobo": metric("Commission revenue", 69_495_000, unit="kobo",
                                          definition="Platform commission from captured payments."),
        "net_revenue_kobo": metric("Net platform revenue", 71_670_000, unit="kobo",
                                   definition="Commission plus fees minus refunds and processing cost."),
    },
    "definitions": [
        ["captured_payments", "Provider-confirmed escrow holds. The only figure that counts as money collected."],
        ["escrow_liability", "Customer money the platform still holds; a liability, never revenue."],
    ],
    "payment_status_breakdown": [
        {"status": "HELD", "label": "Held", "count": 272, "percent": 18.1},
        {"status": "PAYMENT_PENDING", "label": "Payment Pending", "count": 304, "percent": 20.3},
    ],
    "reconciliation": {
        "checked_at": "2026-10-30T09:16:00+00:00",
        "range_start": RANGE["start"],
        "range_end": RANGE["end"],
        "issues": [
            {"key": "missing_provider_reference", "label": "Paid records without a provider reference",
             "count": 0, "severity": "high"},
            {"key": "escrow_without_ledger", "label": "Escrow holds with no ledger entry",
             "count": 4, "severity": "medium"},
        ],
    },
    "served_from_cache": False,
}

BUSINESS = {
    "range": RANGE,
    "generated_at": "2026-10-30T09:16:00+00:00",
    "growth": {
        "customers": metric("Customers", 1200, previous=1100, change_percent=9.1,
                            definition="Distinct customers active in range."),
        "revenue": metric("Revenue", 71_670_000, unit="kobo", previous=60_000_000, change_percent=19.4,
                          definition="Net platform revenue in range."),
    },
    "quality": {
        "repeat_booking_rate": ratio_metric("Repeat booking rate", 584, 981, 59.5,
                                            "Customers with more than one booking, divided by customers with any booking."),
        "average_booking_value_kobo": metric("Average booking value", 3_365_000, unit="kobo",
                                             definition="Agreed price divided by bookings."),
    },
    "supply_demand": {
        "categories": [
            {"id": "Plumbing", "name": "Plumbing", "requests": 800, "artisans": 120, "balance": "balanced"},
            {"id": "Electrical", "name": "Electrical", "requests": 600, "artisans": 40, "balance": "short"},
        ],
        "definition": "Requests created in range against artisans offering the category.",
    },
    "retention": [
        {"cohort": "2026-08", "new_customers": 200, "returned": 64, "retention_percent": 32.0},
    ],
    "served_from_cache": True,
}

SERVICES = {
    "range": RANGE,
    "generated_at": "2026-10-30T09:16:00+00:00",
    "categories": [
        {"id": "Plumbing", "name": "Plumbing", "requests": 800, "unique_customers": 610, "artisans": 120,
         "offers": 500, "bookings": 340, "paid_bookings": 300, "completed": 250, "cancelled": 40,
         "disputes": 5, "transaction_value": "3350000.00"},
    ],
    "count": 1,
    "definitions": {
        "requests": "Service requests created in range, by category.",
        "booking_conversion": "Bookings divided by requests for the category.",
    },
    "rankings": {"by_bookings": [], "method": "Rankings use real booking counts; no score is invented."},
    "served_from_cache": False,
}

CUSTOMERS = {
    "range": RANGE,
    "generated_at": "2026-10-30T09:16:00+00:00",
    "growth": metric("New customers", 120, previous=99, change_percent=21.2,
                     definition="Customer profiles created inside the range."),
    "activity": {
        "active_customers": metric("Active customers", 981, definition="Customers with at least one booking in range."),
        "repeat_rate": ratio_metric("Repeat rate", 584, 981, 59.5, "Repeat customers divided by active customers."),
    },
    "spend": {
        "average_spend_kobo": metric("Average spend", 5_015_200, unit="kobo",
                                     definition="Captured value divided by paying customers."),
        "top_spenders": [{"customer": "Ada Obi", "value_kobo": 33_500_000}],
    },
    "time_between_bookings_days": {"value": 18.4, "median": 12.0, "sample": 220,
                                   "definition": "Days between consecutive bookings for the same customer."},
    "segments": [{"key": "repeat", "label": "Repeat customers", "count": 584}],
    "cohorts": [{"cohort": "2026-09", "new_customers": 150, "returned": 48, "retention_percent": 32.0}],
    "served_from_cache": False,
}

ARTISANS = {
    "range": RANGE,
    "generated_at": "2026-10-30T09:16:00+00:00",
    "totals": {
        "artisans": 300, "verified": 240, "active": 260, "inactive": 40,
        "new_in_range": metric("New artisans", 30, previous=25, change_percent=20.0,
                               definition="Artisan profiles created inside the range."),
        "bookings": metric("Bookings", 340, previous=300, change_percent=13.3,
                           definition="Bookings involving an artisan, created in range."),
        "completed": metric("Completed", 250, definition="Bookings completed in range."),
    },
    "response": {"average_hours": 3.2, "median_hours": 1.8, "p90_hours": 8.4, "sample": 190,
                 "definition": "Hours between a request reaching an artisan and their first reply."},
    "leaderboard": [
        {"artisan_id": "a1", "name": "Musa Bello", "bookings": 22, "completed": 20, "cancelled": 1,
         "disputed": 0, "value": "660000.00", "unique_customers": 18, "cancellation_rate": 4.5,
         "dispute_rate": 0.0, "completion_rate": 90.9, "earnings_kobo": 59_400_000},
    ],
    "ranking_method": "Ranked by completed bookings in range, then by captured value.",
    "served_from_cache": False,
}

FUNNEL = {
    "range": RANGE,
    "generated_at": "2026-10-30T09:16:00+00:00",
    "stages": [
        {"key": "requests", "label": "Requests created", "count": 2000, "conversion_from_previous": 100.0,
         "conversion_from_start": 100.0, "drop_off": 0, "drop_off_percent": 0.0},
        {"key": "bookings", "label": "Bookings created", "count": 340, "conversion_from_previous": 17.0,
         "conversion_from_start": 17.0, "drop_off": 1660, "drop_off_percent": 83.0},
    ],
    "method": "Each stage counts records that reached it in range; drop-off is the arithmetic difference.",
    "operational_kpis": {
        "requests_without_offers": 40, "bookings_unpaid": 12, "payments_failed": 6,
        "services_in_progress": 280, "services_overdue": 3, "disputed_bookings": 5,
        "computed_at": "2026-10-30T09:16:00+00:00",
    },
    "timings": {"request_to_booking_hours": {"average": 5.4, "median": 2.1, "p90": 12.0, "sample": 340,
                                             "definition": "Hours between request creation and booking creation."}},
    "served_from_cache": False,
}

MARKETING = {
    "range": RANGE,
    "generated_at": "2026-10-30T09:16:00+00:00",
    "campaigns": {"total": 6, "scheduled": 1, "sent": 3, "drafts": 1, "pending_approval": 1},
    "delivery": {"queued": 20, "delivered": 1180, "failed": 4, "suppressed": 16, "opens": 410,
                 "clicks": 96, "open_rate": 34.7, "click_rate": 8.1, "failure_rate": 0.3,
                 "definition": "Delivered counts campaign deliveries the channel accepted, not messages composed."},
    "attribution": {"window_days": 7, "attributed_bookings": 12, "attributed_value_kobo": 40_200_000,
                    "method": "A booking counts as attributed when the recipient booked inside the window "
                              "after a tracked campaign link was opened. Association, not proof of cause."},
    "promotions": {"revenue_kobo": 15_000_000, "active_campaigns": 2, "payments_verified": 2,
                   "definition": "Verified promotion payments inside the range."},
    "served_from_cache": False,
}

GEOGRAPHY = {
    "range": RANGE,
    "generated_at": "2026-10-30T09:16:00+00:00",
    "states": [
        {"state": "Lagos", "artisans": 180, "requests": 900, "customers": 700, "bookings": 260,
         "value": "2600000.00", "conversion": 28.9},
    ],
    "definition": "States come from the request location and the artisan's registered state.",
    "served_from_cache": False,
}

INSIGHTS = {
    "range": RANGE,
    "generated_at": "2026-10-30T09:16:00+00:00",
    "insights": [
        {"key": "booking_conversion_drop", "title": "Booking conversion fell against the previous period",
         "observed": "24.5% vs 31.0%", "change_percent": -21.0, "segment": "All categories",
         "method": "Accepted offers divided by offers created, both ranges computed the same way.",
         "suggested_action": "Open the funnel page and check where offers stop being accepted.",
         "severity": "MEDIUM", "evidence_link": "/console/funnel/",
         "range_start": RANGE["start"], "range_end": RANGE["end"],
         "comparison_period": "2026-09-01 to 2026-10-01"},
    ],
    "method": "Rule based. A rule reports an association in the data; it does not claim that one figure caused the other.",
    "data_freshness": FRESHNESS,
    "served_from_cache": False,
}

DATA_QUALITY = {
    "generated_at": "2026-10-30T09:16:00+00:00",
    "checks": [{"key": "booking_without_offer", "label": "Bookings whose offer is missing",
                "count": 0, "severity": "high"}],
    "clean": True,
    "note": "Checks read the live tables; a zero means the condition returned no rows.",
    "served_from_cache": False,
}

SUPPORT = {
    "range": RANGE,
    "generated_at": "2026-10-30T09:16:00+00:00",
    "tickets": {"opened": 24, "open": 6, "under_review": 3, "resolved": 15, "escalated": 1,
                "by_category": [{"category": "PAYMENT", "label": "Payment", "count": 9}],
                "by_agent": [{"agent": "Ada", "label": "Ada", "count": 7}],
                "average_resolution_hours": 18.5, "sample": 15, "first_response_hours": 2.4},
    "disputes": {"opened": 5, "open": 2, "resolved": 3, "disputed_value_kobo": 67_000_000,
                 "refunded_kobo": 20_000_000,
                 "definition": "A dispute is a booking or payment placed in DISPUTED inside the range."},
    "definition": "Ticket counts come from support cases opened in range.",
    "served_from_cache": False,
}

RISK = {
    "range": RANGE,
    "generated_at": "2026-10-30T09:16:00+00:00",
    "signals": [{"key": "repeat_cancellations", "label": "Customers with 3+ cancellations",
                 "count": 2, "severity": "medium"}],
    "reviews": [],
    "policy": "A risk signal opens a human review. Nothing is suspended automatically and every decision "
              "is appealable.",
    "served_from_cache": False,
}

RISK_REVIEWS = {
    "results": [
        {"id": "5b1f0f9f-6f3f-4f1e-9d6a-0c1f5a1f0a11", "signal": "repeat_cancellations",
         "severity": "medium", "status": "OPEN", "summary": "Three cancellations in seven days.",
         "created_at": "2026-10-29T12:00:00+00:00"},
    ]
}

# --------------------------------------------------------------------------- #
# Reports / explorer / exports
# --------------------------------------------------------------------------- #
CATALOGUE = {
    "domains": [
        {"key": "bookings", "label": "Bookings", "description": "Every booking row, dated by creation time.",
         "dimensions": [{"key": "status", "label": "Status", "kind": "category"}],
         "measures": [{"key": "count", "label": "Bookings", "unit": "number"}]},
    ],
    "aggregations": ["count", "sum", "avg"],
    "charts": ["kpi", "bar", "line", "table"],
    "max_rows": 500,
    "note": "Only the fields listed here can be queried. There is no free-form SQL.",
}

EXPLORER_RESULT = {
    "domain": "bookings", "domain_label": "Bookings", "measure": "count", "measure_label": "Bookings",
    "dimension": "status", "dimension_label": "Status", "unit": "number", "range": RANGE,
    "rows": [{"group": "ACCEPTED", "value": 316}, {"group": "COMPLETED", "value": 273}],
    "chart": "bar", "row_count": 2, "spec_hash": "abc123", "served_from_cache": False,
}

SAVED_REPORTS = {
    "results": [
        {"id": "0e1b3f6e-8c2a-4b7d-9f2e-3a4b5c6d7e8f", "name": "Bookings by status",
         "description": "Explorer smoke test", "spec": {"domain": "bookings"}, "is_shared": True, "is_mine": True, "owner": "Console Manager",
         "updated_at": "2026-10-30T09:00:00+00:00"},
    ]
}

JOBS = {
    "results": [
        {"id": "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d", "kind": "EXPORT", "status": "SUCCEEDED",
         "queue": "exports", "progress": 100, "total": 7, "percent": 100,
         "result": {"filename": "bookings.csv", "rows": 7}, "error": "", "attempts": 1,
         "correlation_id": "c0ffee", "created_at": "2026-10-30T09:05:00+00:00",
         "started_at": "2026-10-30T09:05:02+00:00", "finished_at": "2026-10-30T09:05:03+00:00",
         "duration_seconds": 1.2},
    ],
    "queue": {"queued": 0, "running": 0, "failed_24h": 0, "succeeded_24h": 1,
              "oldest_queued_at": "", "oldest_queued_seconds": 0,
              "average_duration_seconds": 1.2, "p95_duration_seconds": 1.2, "sample": 1},
}

# --------------------------------------------------------------------------- #
# Campaigns, staff, configuration, privacy
# --------------------------------------------------------------------------- #
CAMPAIGNS = {
    "generated_at": "2026-10-30T04:00:00+00:00",
    "segments": [{"key": "all_customers", "label": "All customers"}],
    "channels": [{"key": "IN_APP", "label": "In-app"}],
    "results": [
        {"id": "c3c87157-74b3-4bc9-9adc-0426fb002108", "title": "Weekend plumbing demand",
         "status": "SCHEDULED", "audience": 1200, "suppressed": 16, "queued": 0, "sent": 1184,
         "failed": 0, "skipped": 16, "clicks": 96, "click_rate": 8.1,
         "failure_rate": 0.0, "attribution_window_days": 7},
    ],
}

STAFF = {
    "staff": [
        {"id": "11111111-1111-4111-8111-111111111111", "name": "Console Manager",
         "email": "console.manager@example.com", "role": "Super Administrator", "job_title": "Owner",
         "is_active": True, "is_suspended": False, "suspension_reason": "", "totp_enrolled": True,
         "last_authenticated_at": "2026-10-30T08:00:00+00:00", "active_sessions": 1},
    ],
    "roles": [
        {"slug": "super-admin", "name": "Super Administrator", "description": "Full control.",
         "is_system": True, "permissions": ["analytics.view"], "member_count": 1},
    ],
    "permission_catalogue": [
        {"category": "analytics", "permissions": [{"code": "analytics.view", "label": "View analytics"}]},
    ],
    "can_manage": True,
}

ROLES = {
    "roles": [
        {"slug": "finance-manager", "name": "Finance manager", "description": "Money and configuration.",
         "is_system": True, "permissions": ["analytics.financial"], "member_count": 0},
    ],
    "system_roles": [
        {"slug": "super-admin", "name": "Super Administrator", "description": "Full control.",
         "permissions": ["analytics.view"]},
    ],
}

CONFIG = {
    "settings": [
        {"key": "fees.customer_service_fee_kobo", "label": "Customer service fee",
         "description": "Flat fee added to every escrow payment.", "type": "int", "choices": [],
         "default": 15000, "category": "sensitive", "permission": "config.finance",
         "requires_approval": True, "value": 15000, "version": 3,
         "effective_from": "2026-10-01T00:00:00+00:00"},
    ],
    "pending_changes": [],
    "step_up_scope": "config:finance",
    "note": "Sensitive settings need step-up and a second approver. Accepted offers keep the terms they were priced with.",
}

SNAPSHOTS = {
    "interval_seconds": 300, "interval_label": "5 minutes",
    "supported_intervals": [{"seconds": 60, "label": "1 minute"}, {"seconds": 300, "label": "5 minutes"}],
    "last_captured_for": "2026-10-30T09:15:00+00:00", "last_success_at": "2026-10-30T09:15:04+00:00",
    "seconds_since_last": 60, "next_expected_at": "2026-10-30T09:16:00+00:00", "behind_by_intervals": 0,
    "slots_last_hour": 60, "expected_slots_last_hour": 60, "missing_slots_last_hour": 0,
    "latest_duration_ms": 412, "metrics_tracked": 18, "recent": [],
    "computed_at": "2026-10-30T09:16:00+00:00",
    "note": "Snapshots are written by a queued job; a page request never computes them.",
}

HEALTH = {
    "generated_at": "2026-10-30T09:16:00+00:00", "environment": "development", "debug": False,
    "database": {"status": "healthy", "latency_ms": 1.2, "error": None},
    "cache": {"status": "healthy", "latency_ms": 0.4, "backend": "locmem"},
    "storage": {"status": "healthy", "detail": "writable"},
    "outbox": {"status": "healthy", "pending": 0, "dead_lettered": 0, "oldest_pending_seconds": None},
    "payments": {"status": "healthy", "configured": True, "last_webhook_at": None,
                 "failed_payments_24h": 0, "detail": "provider reachable"},
    "notifications": {"status": "healthy", "last_delivery_at": None, "delivered_24h": 0},
    "jobs": {"queued": 0, "running": 0, "failed_24h": 0, "succeeded_24h": 1, "oldest_queued_at": "",
             "oldest_queued_seconds": 0, "average_duration_seconds": 1.2, "p95_duration_seconds": 1.2,
             "sample": 1},
    "workers": {"status": "healthy", "last_success_at": None, "queues": [{"queue": "default", "queued": 0}]},
    "snapshots": {"interval_seconds": 60, "last_captured_for": "2026-10-30T09:15:00+00:00"},
    "campaigns": {"status": "healthy", "dispatching": 0, "stale_dispatch": 0, "scheduled": 1},
    "analytics": {"status": "healthy", "last_event_at": "2026-10-30T09:15:00+00:00", "lag_seconds": 60,
                  "events_24h": 240},
    "overall": "healthy",
    "latency": {"overall": {"count": 120, "p50": 42.0, "p95": 180.0, "p99": 290.0, "max": 310.0,
                            "average": 55.0},
                "error_rate_percent": 0.0,
                "slowest_endpoints": [{"endpoint": "/api/v1/manager/financial/", "count": 10, "p50": 60.0,
                                       "p95": 120.0, "p99": 140.0, "max": 150.0, "average": 70.0}],
                "samples": 120},
}

AUDIT = {
    "total": 2, "page": 1, "size": 50,
    "results": [
        {"id": 2, "action": "MANAGER_CONFIG_CHANGE_REQUESTED", "resource_type": "PLATFORM_CONFIG",
         "resource_id": "fees.customer_service_fee_kobo", "actor": "console.manager@example.com",
         "created_at": "2026-10-30T09:00:00+00:00", "metadata_keys": ["requires_approval"],
         "ip_hash_prefix": "9f2a"},
    ],
    "filters": [{"action": "MANAGER_CONFIG_CHANGE_REQUESTED", "count": 1}],
}

PRIVACY = {
    "data_classes": [{"key": "analytics_events", "label": "Analytics events", "holders": ["manager"]}],
    "permission_matrix": [{"permission": "privacy.view", "label": "View privacy data", "category": "privacy"}],
    "retention": [{"data_class": "analytics_events", "days": 400, "enabled": True,
                   "notes": "Aggregated product events.", "updated_at": "2026-10-01T00:00:00+00:00"}],
    "open_requests": 1,
    "requests": [],
    "export_ttl_hours": 72,
    "notes": ["Exports redact contact details unless the exporter holds export.sensitive."],
}

PRIVACY_REQUESTS = {
    "results": [
        {"reference": "DSR-2026-0001", "request_type": "ACCESS", "status": "IN_PROGRESS",
         "created_at": "2026-10-28T09:00:00+00:00", "due_at": "2026-11-27T09:00:00+00:00"},
    ]
}

STEP_UP_GRANT = {
    "token": "grant-1",
    "scope": "config:finance",
    "expires_at": "2026-10-30T09:21:00+00:00",
    "expires_in": 300,
}

PAYLOADS = {
    # Navbar badges the shared page shell requests for any signed-in visitor.
    "/notifications/unread-count/": {"unread_count": 0},
    "/conversations/unread-count/": {"unread_count": 0},
    "/manager/session/": SESSION,
    "/manager/analytics/overview/": OVERVIEW,
    "/manager/financial/": FINANCIAL,
    "/manager/business/": BUSINESS,
    "/manager/services/": SERVICES,
    "/manager/customers/": CUSTOMERS,
    "/manager/artisans/": ARTISANS,
    "/manager/funnel/": FUNNEL,
    "/manager/marketing/": MARKETING,
    "/manager/geography/": GEOGRAPHY,
    "/manager/insights/": INSIGHTS,
    "/manager/data-quality/": DATA_QUALITY,
    "/manager/support/": SUPPORT,
    "/manager/risk/": RISK,
    "/manager/risk/reviews/": RISK_REVIEWS,
    "/manager/explorer/catalogue/": CATALOGUE,
    "/manager/explorer/run/": EXPLORER_RESULT,
    "/manager/reports/": SAVED_REPORTS,
    "/manager/jobs/": JOBS,
    "/manager/campaigns/": CAMPAIGNS,
    "/manager/staff/": STAFF,
    "/manager/staff/roles/": ROLES,
    "/manager/config/": CONFIG,
    "/manager/snapshots/": SNAPSHOTS,
    "/manager/health/": HEALTH,
    "/manager/audit/": AUDIT,
    "/manager/privacy/": PRIVACY,
    "/manager/privacy/requests/": PRIVACY_REQUESTS,
}

# Pages that must render, and a string that proves the payload reached the HTML.
PAGE_MARKERS = [
    ("console-overview", "/console/", "1200"),
    ("console-financial", "/console/financial/", "14,169,800.00"),
    ("console-business", "/console/business/", "Repeat booking rate"),
    ("console-services", "/console/services/", "Plumbing"),
    ("console-customers", "/console/customers/", "Ada Obi"),
    ("console-artisans", "/console/artisans/", "Musa Bello"),
    ("console-funnel", "/console/funnel/", "Requests created"),
    ("console-marketing", "/console/marketing/", "Attributed bookings"),
    ("console-geography", "/console/geography/", "Lagos"),
    ("console-insights", "/console/insights/", "Booking conversion fell"),
    ("console-data-quality", "/console/data-quality/", "Bookings whose offer is missing"),
    ("console-support", "/console/support/", "Value in dispute"),
    ("console-risk", "/console/risk/", "Nothing is suspended automatically"),
    ("console-reports", "/console/reports/", "There is no free-form SQL"),
    ("console-campaigns", "/console/campaigns/", "Weekend plumbing demand"),
    ("console-staff", "/console/staff/", "Super Administrator"),
    ("console-config", "/console/config/", "Customer service fee"),
    ("console-jobs", "/console/jobs/", "Capture interval"),
    ("console-system-health", "/console/system-health/", "healthy"),
    ("console-audit", "/console/audit/", "MANAGER_CONFIG_CHANGE_REQUESTED"),
    ("console-privacy", "/console/privacy/", "DSR-2026-0001"),
]


# Endpoints used by the write flows; kept out of the read fixtures above so a
# console page that tries to read them still fails loudly.
WRITE_PAYLOADS = {
    "/manager/auth/step-up/": STEP_UP_GRANT,
    "/manager/config/fees.customer_service_fee_kobo/change/": {"detail": "Change recorded."},
}
PAYLOADS.update(WRITE_PAYLOADS)
