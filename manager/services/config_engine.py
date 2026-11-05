"""Versioned platform configuration.

Rules:
* a value is never edited in place - every change is a new ``PlatformConfigVersion``;
* financial (sensitive) keys need a second approver who is not the requester;
* every applied version records who, when, why and the resulting audit entry;
* callers always resolve through :func:`current`, so a change takes effect from its
  ``effective_from`` and never rewrites history.
"""
from __future__ import annotations

from decimal import Decimal

from django.core.cache import cache
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from core.services import create_audit_log
from manager.config_spec import CONFIG_KEYS, SENSITIVE, ConfigError, normalise, spec_for
from manager.models import ConfigChangeRequest, PlatformConfigVersion

CACHE_KEY = "manager:config:v1"
CACHE_TTL = 60


class ConfigPermissionError(PermissionError):
    pass


def _serialise_version(version):
    return {
        "id": str(version.id),
        "key": version.key,
        "version": version.version,
        "value": version.value,
        "effective_from": version.effective_from.isoformat(),
        "expires_at": version.expires_at.isoformat() if version.expires_at else None,
        "note": version.note,
        "created_by": str(version.created_by_id) if version.created_by_id else None,
        "created_at": version.created_at.isoformat(),
    }


def _resolved_map(now=None):
    """One query for the whole catalogue: latest effective version per key."""
    now = now or timezone.now()
    cached = cache.get(CACHE_KEY)
    if cached is not None:
        return cached
    rows = (
        PlatformConfigVersion.objects.filter(effective_from__lte=now)
        .exclude(expires_at__lte=now)
        .order_by("key", "-effective_from", "-version")
        .values("key", "value", "version", "effective_from")
    )
    resolved = {}
    for row in rows:
        if row["key"] in resolved:
            continue
        resolved[row["key"]] = {
            "value": row["value"],
            "version": row["version"],
            "effective_from": row["effective_from"].isoformat(),
        }
    cache.set(CACHE_KEY, resolved, CACHE_TTL)
    return resolved


def invalidate_cache():
    cache.delete(CACHE_KEY)


def current(key, *, now=None):
    """Resolve one setting. Falls back to the shipped default."""
    spec = spec_for(key)
    row = _resolved_map(now).get(key)
    return row["value"] if row else spec["default"]


def current_with_meta(key, *, now=None):
    spec = spec_for(key)
    row = _resolved_map(now).get(key)
    if row:
        return {"key": key, "value": row["value"], "version": row["version"],
                "effective_from": row["effective_from"], "source": "database"}
    return {"key": key, "value": spec["default"], "version": 0,
            "effective_from": None, "source": "default"}


def current_version_number(key):
    return PlatformConfigVersion.objects.filter(key=key).aggregate(v=Max("version"))["v"] or 0


def all_settings():
    """Full catalogue with current value, default and metadata, for the console."""
    resolved = _resolved_map()
    rows = []
    for key, spec in CONFIG_KEYS.items():
        row = resolved.get(key)
        rows.append(
            {
                "key": key,
                "label": spec["label"],
                "description": spec["description"],
                "type": spec["type"],
                "choices": spec.get("choices", []),
                "default": spec["default"],
                "category": spec["category"],
                "permission": spec["permission"],
                "requires_approval": spec["category"] == SENSITIVE,
                "value": row["value"] if row else spec["default"],
                "version": row["version"] if row else 0,
                "effective_from": row["effective_from"] if row else None,
                "is_default": row is None,
            }
        )
    return rows


def history(key):
    return [_serialise_version(v) for v in PlatformConfigVersion.objects.filter(key=key).select_related("created_by").order_by("-version")[:50]]


def _money(kobo):
    return f"{Decimal(int(kobo)) / 100:,.2f}"


def preview(key, value, *, sample_amounts_kobo=(5_000_00, 33_500_00, 150_000_00)):
    """Compare the live rule with the proposal on real worked examples."""
    spec = spec_for(key)
    incoming = normalise(key, value)
    existing = current(key)
    payload = {
        "key": key,
        "label": spec["label"],
        "current": existing,
        "proposed": incoming,
        "changed": existing != incoming,
        "requires_approval": spec["category"] == SENSITIVE,
        "examples": [],
    }
    if key in {"fees.customer_fee_mode", "fees.customer_fee_kobo", "fees.customer_fee_percent",
               "fees.min_fee_kobo", "fees.max_fee_kobo", "fees.commission_rate",
               "fees.category_commission_overrides"}:
        from manager.services.fees import quote

        for amount in sample_amounts_kobo:
            before = quote(amount, override={key: existing})
            after = quote(amount, override={key: incoming})
            payload["examples"].append(
                {
                    "service_amount_kobo": amount,
                    "service_amount": _money(amount),
                    "before": {
                        "customer_total": _money(before["total_amount_kobo"]),
                        "customer_fee": _money(before["customer_fee_kobo"]),
                        "platform_revenue": _money(before["platform_commission_kobo"] + before["customer_fee_kobo"]),
                        "artisan_payout": _money(before["artisan_payout_kobo"]),
                    },
                    "after": {
                        "customer_total": _money(after["total_amount_kobo"]),
                        "customer_fee": _money(after["customer_fee_kobo"]),
                        "platform_revenue": _money(after["platform_commission_kobo"] + after["customer_fee_kobo"]),
                        "artisan_payout": _money(after["artisan_payout_kobo"]),
                    },
                    "customer_difference": _money(after["total_amount_kobo"] - before["total_amount_kobo"]),
                    "platform_difference": _money(
                        (after["platform_commission_kobo"] + after["customer_fee_kobo"])
                        - (before["platform_commission_kobo"] + before["customer_fee_kobo"])
                    ),
                }
            )
        payload["note"] = (
            "Existing agreements keep the price and fee they were accepted with. "
            "Only new bookings created after the effective date use the new rule."
        )
        payload["affected_transactions"] = "New bookings only"
    return payload


def _assert_can_change(user, key):
    from manager.services.auth import has_perm

    spec = spec_for(key)
    if not has_perm(user, spec["permission"]):
        raise ConfigPermissionError("You do not have permission to change this setting.")


def request_change(*, user, key, value, reason, effective_from=None):
    """Create a change request. Non-sensitive keys apply immediately."""
    _assert_can_change(user, key)
    if not str(reason or "").strip():
        raise ConfigError("A reason is required for every configuration change.")
    spec = spec_for(key)
    proposed = normalise(key, value)
    existing = current(key)
    requires_approval = spec["category"] == SENSITIVE
    with transaction.atomic():
        change = ConfigChangeRequest.objects.create(
            key=key,
            proposed_value=proposed,
            current_value={"value": existing},
            preview=preview(key, proposed),
            reason=str(reason).strip()[:500],
            requires_approval=requires_approval,
            requested_by=user,
            effective_from=effective_from,
            status=ConfigChangeRequest.Status.PENDING,
        )
        if not requires_approval:
            _apply(user=user, change=change, effective_from=effective_from)
        create_audit_log(
            action="MANAGER_CONFIG_CHANGE_REQUESTED",
            resource_type="PLATFORM_CONFIG",
            resource_id=key,
            actor=user,
            metadata={"key": key, "from": existing, "to": proposed, "requires_approval": requires_approval},
        )
    return change


def approve_change(*, user, change, effective_from=None):
    _assert_can_change(user, change.key)
    from manager.services.auth import has_perm

    if not has_perm(user, "config.approve") and change.requires_approval:
        raise ConfigPermissionError("You do not have permission to approve configuration changes.")
    if change.requested_by_id == user.id:
        raise ConfigPermissionError("A change must be approved by a different manager.")
    if change.status not in {ConfigChangeRequest.Status.PENDING, ConfigChangeRequest.Status.APPROVED}:
        raise ConfigError("This change is no longer awaiting approval.")
    with transaction.atomic():
        _apply(user=user, change=change, effective_from=effective_from)
        create_audit_log(
            action="MANAGER_CONFIG_CHANGE_APPROVED",
            resource_type="PLATFORM_CONFIG",
            resource_id=change.key,
            actor=user,
            metadata={"key": change.key, "requested_by": str(change.requested_by_id),
                      "value": change.proposed_value},
        )
    return change


def reject_change(*, user, change, reason=""):
    _assert_can_change(user, change.key)
    if change.status != ConfigChangeRequest.Status.PENDING:
        raise ConfigError("This change is no longer awaiting approval.")
    change.status = ConfigChangeRequest.Status.REJECTED
    change.approved_by = user
    change.decided_at = timezone.now()
    change.save(update_fields=["status", "approved_by", "decided_at", "updated_at"])
    create_audit_log(
        action="MANAGER_CONFIG_CHANGE_REJECTED",
        resource_type="PLATFORM_CONFIG",
        resource_id=change.key,
        actor=user,
        metadata={"key": change.key, "reason": reason[:500]},
    )
    return change


def _apply(*, user, change, effective_from=None):
    version = current_version_number(change.key) + 1
    effective_from = effective_from or change.effective_from or timezone.now()
    PlatformConfigVersion.objects.create(
        key=change.key,
        version=version,
        value=change.proposed_value,
        value_type=spec_for(change.key)["type"],
        effective_from=effective_from or timezone.now(),
        note=change.reason[:240],
        created_by=user,
        change_request=change,
    )
    change.status = ConfigChangeRequest.Status.APPLIED
    change.approved_by = user
    change.decided_at = timezone.now()
    change.applied_version = version
    change.save(update_fields=["status", "approved_by", "decided_at", "applied_version", "updated_at"])
    invalidate_cache()
    return version


def rollback(*, user, key, to_version, reason):
    """Restore an earlier value by writing a new version - history is kept."""
    _assert_can_change(user, key)
    target = PlatformConfigVersion.objects.get(key=key, version=to_version)
    spec = spec_for(key)
    with transaction.atomic():
        change = ConfigChangeRequest.objects.create(
            key=key,
            proposed_value=target.value,
            current_value={"value": current(key)},
            preview={"rollback_to": to_version},
            reason=f"Rollback to v{to_version}: {reason}"[:500],
            requires_approval=spec["category"] == SENSITIVE,
            requested_by=user,
            status=ConfigChangeRequest.Status.PENDING,
        )
        if spec["category"] != SENSITIVE:
            _apply(user=user, change=change)
        create_audit_log(
            action="MANAGER_CONFIG_ROLLBACK_REQUESTED",
            resource_type="PLATFORM_CONFIG",
            resource_id=key,
            actor=user,
            metadata={"key": key, "to_version": to_version, "reason": reason[:240]},
        )
    return change
