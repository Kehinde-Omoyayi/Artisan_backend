"""Self-service Data Explorer.

Managers choose a domain, metric, dimension, filters and grouping from a fixed,
validated registry. There is no free-form SQL: every field name is looked up in
this module, so a crafted request cannot reach an arbitrary column, join or table.
"""
from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.db.models import Avg, Count, DecimalField, Max, Min, Q, Sum
from django.db.models.fields import DecimalField as DecimalModelField
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone

from booking.models import Booking
from conversations.models import Offer
from home.models import ArtisanProfile, CustomerProfile
from payments.models import EscrowPayment, EscrowRelease
from reviews.models import Review
from services.models import ServiceRequest
from support.models import SupportCase, SupportFinancialAction

MAX_ROWS = 500
MAX_EXPORT_ROWS = 50_000

MONEY = DecimalField(max_digits=20, decimal_places=2)


def _domain(label, model, date_field, dimensions, measures, description):
    return {
        "label": label,
        "model": model,
        "date_field": date_field,
        "description": description,
        "dimensions": dimensions,  # name -> (orm path, label, grouping hint)
        "measures": measures,  # name -> (orm path, label, aggregation, unit)
    }


DOMAINS = {
    "bookings": _domain(
        "Bookings",
        Booking,
        "created_at",
        {
            "status": ("status", "Status", "category"),
            "category": ("request__category__name", "Category", "category"),
            "service": ("request__service__name", "Service", "category"),
            "state": ("request__location__state", "State", "category"),
            "artisan": ("artisan__user__first_name", "Artisan", "entity"),
            "customer": ("customer__user__first_name", "Customer", "entity"),
            "day": ("__day__", "Day", "time"),
            "month": ("__month__", "Month", "time"),
        },
        {
            "count": ("pk", "Bookings", "count", "number"),
            "agreed_value": ("price_agreed", "Agreed value", "sum", "currency"),
            "average_agreed_value": ("price_agreed", "Average agreed value", "avg", "currency"),
            "completed": ("__completed__", "Completed bookings", "count_filter", "number"),
            "cancelled": ("__cancelled__", "Cancelled bookings", "count_filter", "number"),
        },
        "Every booking row, dated by creation time.",
    ),
    "payments": _domain(
        "Escrow payments",
        EscrowPayment,
        "created_at",
        {
            "status": ("status", "Status", "category"),
            "state": ("booking__request__location__state", "State", "category"),
            "category": ("booking__request__category__name", "Category", "category"),
            "day": ("__day__", "Day", "time"),
            "month": ("__month__", "Month", "time"),
        },
        {
            "count": ("pk", "Payments", "count", "number"),
            "captured_value": ("total_amount_kobo", "Captured value (kobo)", "sum", "kobo"),
            "commission": ("platform_commission_kobo", "Commission (kobo)", "sum", "kobo"),
            "service_fees": ("customer_fee_kobo", "Service fees (kobo)", "sum", "kobo"),
            "average_payment": ("total_amount_kobo", "Average payment (kobo)", "avg", "kobo"),
            "largest_payment": ("total_amount_kobo", "Largest payment (kobo)", "max", "kobo"),
        },
        "Escrow payment rows. Kobo values are integers; divide by 100 for naira.",
    ),
    "requests": _domain(
        "Service requests",
        ServiceRequest,
        "created_at",
        {
            "status": ("status", "Status", "category"),
            "category": ("category__name", "Category", "category"),
            "service": ("service__name", "Service", "category"),
            "state": ("location__state", "State", "category"),
            "urgency": ("urgency", "Urgency", "category"),
            "day": ("__day__", "Day", "time"),
            "month": ("__month__", "Month", "time"),
        },
        {
            "count": ("pk", "Requests", "count", "number"),
            "unique_customers": ("requester_id", "Unique customers", "distinct", "number"),
            "average_budget": ("budget", "Average budget", "avg", "currency"),
        },
        "Service requests, including ones that never became a booking.",
    ),
    "offers": _domain(
        "Offers",
        Offer,
        "created_at",
        {
            "status": ("status", "Status", "category"),
            "category": ("conversation__service_request__category__name", "Category", "category"),
            "day": ("__day__", "Day", "time"),
            "month": ("__month__", "Month", "time"),
        },
        {
            "count": ("pk", "Offers", "count", "number"),
            "total_value": ("total_amount", "Total value", "sum", "currency"),
            "average_value": ("total_amount", "Average value", "avg", "currency"),
        },
        "Itemised offers sent by artisans.",
    ),
    "customers": _domain(
        "Customers",
        CustomerProfile,
        "created_at",
        {
            "state": ("user__artisan_profile__state", "State", "category"),
            "day": ("__day__", "Day", "time"),
            "month": ("__month__", "Month", "time"),
        },
        {"count": ("pk", "Customers", "count", "number")},
        "Customer profiles. No contact details or identity data are exposed here.",
    ),
    "artisans": _domain(
        "Artisans",
        ArtisanProfile,
        "created_at",
        {
            "state": ("state", "State", "category"),
            "status": ("profile_status", "Profile status", "category"),
            "day": ("__day__", "Day", "time"),
            "month": ("__month__", "Month", "time"),
        },
        {
            "count": ("pk", "Artisans", "count", "number"),
            "average_rating": ("average_rating", "Average rating", "avg", "number"),
        },
        "Artisan profiles. Identity documents are never part of this domain.",
    ),
    "reviews": _domain(
        "Reviews",
        Review,
        "created_at",
        {
            "rating": ("rating", "Rating", "category"),
            "day": ("__day__", "Day", "time"),
            "month": ("__month__", "Month", "time"),
        },
        {
            "count": ("pk", "Reviews", "count", "number"),
            "average_rating": ("rating", "Average rating", "avg", "number"),
        },
        "Customer ratings left after a completed booking.",
    ),
    "support": _domain(
        "Support cases",
        SupportCase,
        "created_at",
        {
            "status": ("status", "Status", "category"),
            "case_type": ("case_type", "Case type", "category"),
            "priority": ("priority", "Priority", "category"),
            "day": ("__day__", "Day", "time"),
            "month": ("__month__", "Month", "time"),
        },
        {"count": ("pk", "Cases", "count", "number")},
        "Support and dispute cases.",
    ),
    "refunds": _domain(
        "Refunds",
        SupportFinancialAction,
        "requested_at",
        {
            "status": ("status", "Status", "category"),
            "action": ("action_type", "Action", "category"),
            "day": ("__day__", "Day", "time"),
            "month": ("__month__", "Month", "time"),
        },
        {
            "count": ("pk", "Refunds", "count", "number"),
            "amount": ("amount_kobo", "Amount (kobo)", "sum", "kobo"),
        },
        "Financial actions of type refund.",
    ),
    "payouts": _domain(
        "Payouts",
        EscrowRelease,
        "requested_at",
        {
            "status": ("status", "Status", "category"),
            "day": ("__day__", "Day", "time"),
            "month": ("__month__", "Month", "time"),
        },
        {
            "count": ("pk", "Payouts", "count", "number"),
            "amount": ("amount_kobo", "Amount (kobo)", "sum", "kobo"),
        },
        "Escrow releases to artisans.",
    ),
}

CHART_TYPES = [
    "kpi", "bar", "column", "line", "area", "stacked_bar", "pie", "donut", "funnel",
    "scatter", "histogram", "heatmap", "table",
]

SUGGESTED_CHARTS = {
    "time": ["line", "area", "column", "table"],
    "category": ["bar", "column", "pie", "donut", "stacked_bar", "table", "histogram"],
    "entity": ["table", "bar"],
}


class ExplorerError(ValueError):
    pass


def catalogue():
    return {
        "domains": [
            {
                "key": key,
                "label": domain["label"],
                "description": domain["description"],
                "dimensions": [
                    {"key": name, "label": label, "kind": kind}
                    for name, (_path, label, kind) in domain["dimensions"].items()
                ],
                "measures": [
                    {"key": name, "label": label, "unit": unit}
                    for name, (_path, label, _agg, unit) in domain["measures"].items()
                ],
            }
            for key, domain in DOMAINS.items()
        ],
        "aggregations": ["count", "sum", "avg", "min", "max", "distinct"],
        "charts": CHART_TYPES,
        "max_rows": MAX_ROWS,
        "note": "Only the fields listed here can be queried. There is no free-form SQL.",
    }


def _measure_expression(domain, measure_key):
    if measure_key not in domain["measures"]:
        raise ExplorerError(f"Unknown measure '{measure_key}' for this domain.")
    path, _label, agg, _unit = domain["measures"][measure_key]
    if agg == "count_filter":
        if path == "__completed__":
            return Count("pk", filter=Q(status="COMPLETED"))
        if path == "__cancelled__":
            return Count("pk", filter=Q(status__in=["CANCELLED", "REJECTED"]))
        raise ExplorerError("Unsupported filter measure.")
    if agg == "count":
        return Count("pk")
    if agg == "distinct":
        return Count(path, distinct=True)
    output = MONEY if path.endswith(("value", "amount", "budget", "kobo")) else None
    if agg == "sum":
        return Coalesce(Sum(path, output_field=output or DecimalField()), 0)
    if agg == "avg":
        return Avg(path)
    if agg == "min":
        return Min(path)
    if agg == "max":
        return Max(path)
    raise ExplorerError(f"Unsupported aggregation '{agg}'.")


def _dimension_expression(domain, dimension_key):
    if dimension_key not in domain["dimensions"]:
        raise ExplorerError(f"Unknown dimension '{dimension_key}' for this domain.")
    path, _label, kind = domain["dimensions"][dimension_key]
    if path == "__day__":
        return TruncDate("created_at" if "created_at" in [f.name for f in domain["model"]._meta.fields] else domain["date_field"],
                         tzinfo=timezone.get_current_timezone()), "day", "time"
    if path == "__month__":
        field = "created_at" if "created_at" in [f.name for f in domain["model"]._meta.fields] else domain["date_field"]
        return TruncDate(field, tzinfo=timezone.get_current_timezone()), "month", "time"
    return path, dimension_key, kind


def run(spec, *, limit=None):
    """Execute a validated explorer spec. Returns rows, a summary and chart hints."""
    domain_key = str(spec.get("domain") or "").strip()
    if domain_key not in DOMAINS:
        raise ExplorerError("Choose a data domain.")
    domain = DOMAINS[domain_key]
    measure_key = str(spec.get("measure") or "count").strip()
    dimension_key = str(spec.get("dimension") or "").strip()

    from manager.services.metrics import resolve_range

    rng = resolve_range(spec.get("filters") or spec)
    qs = domain["model"].objects.filter(**{
        f"{domain['date_field']}__gte": rng.start,
        f"{domain['date_field']}__lt": rng.end,
    })

    status_filter = str(spec.get("status") or "").strip()
    if status_filter and "status" in domain["dimensions"]:
        qs = qs.filter(status=status_filter[:30])

    rows = []
    dimension_label = "Total"
    chart = "table"
    if dimension_key:
        expression, dim_name, kind = _dimension_expression(domain, dimension_key)
        measure = _measure_expression(domain, measure_key)
        grouped = qs.values(dim_name).annotate(value=measure).order_by("-value")
        row_limit = min(limit or MAX_ROWS, MAX_ROWS)
        for row in grouped[:row_limit]:
            value = row["value"]
            rows.append({
                "group": str(row[dim_name]) if row[dim_name] is not None else "Unknown",
                "value": float(value) if isinstance(value, Decimal) else (value if value is not None else 0),
            })
        dimension_label = domain["dimensions"][dimension_key][1]
        chart = SUGGESTED_CHARTS.get(kind, ["table"])[0]
    else:
        measure = _measure_expression(domain, measure_key)
        value = qs.aggregate(value=measure)["value"]
        rows = [{"group": domain["label"], "value": float(value) if isinstance(value, Decimal) else (value or 0)}]
        chart = "kpi"

    unit = domain["measures"][measure_key][3]
    total = sum(row["value"] for row in rows)
    payload = {
        "domain": domain_key,
        "domain_label": domain["label"],
        "measure": measure_key,
        "measure_label": domain["measures"][measure_key][1],
        "dimension": dimension_key,
        "dimension_label": dimension_label,
        "unit": unit,
        "range": {"key": rng.key, "label": rng.label, "start": rng.start.isoformat(), "end": rng.end.isoformat()},
        "rows": rows,
        "row_count": len(rows),
        "total": total,
        "suggested_charts": SUGGESTED_CHARTS.get(
            domain["dimensions"].get(dimension_key, ("", "", "time"))[2], ["table"]),
        "chart": spec.get("chart") if spec.get("chart") in CHART_TYPES else chart,
        "generated_at": timezone.now().isoformat(),
        "spec_hash": hashlib.sha256(json.dumps(spec, sort_keys=True, default=str).encode()).hexdigest()[:16],
    }
    return payload


def cached_run(spec, ttl=60):
    key = cache.make_key("mgr:" + hashlib.sha256(f"explorer:{json.dumps(spec, sort_keys=True, default=str)}".encode()).hexdigest())
    hit = cache.get(key)
    if hit is not None:
        payload = dict(hit)
        payload["served_from_cache"] = True
        return payload
    payload = run(spec)
    cache.set(key, payload, ttl)
    payload = dict(payload)
    payload["served_from_cache"] = False
    return payload


def stream_rows(spec, *, limit=MAX_EXPORT_ROWS):
    """Row iterator used by the async export job (bounded, chunked)."""
    domain_key = str(spec.get("domain") or "")
    if domain_key not in DOMAINS:
        raise ExplorerError("Choose a data domain.")
    domain = DOMAINS[domain_key]
    from manager.services.metrics import resolve_range

    rng = resolve_range(spec.get("filters") or spec)
    columns = []
    dimension_key = str(spec.get("dimension") or "")
    if dimension_key:
        columns.append(("group", domain["dimensions"][dimension_key][1]))
    columns.append(("value", domain["measures"].get(spec.get("measure") or "count", ("", "Value", "", ""))[1]))
    payload = run(spec, limit=limit)
    for row in payload["rows"][:limit]:
        yield columns, row
