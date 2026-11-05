"""Audit browsing, privacy workflows, retention and data quality actions."""
from __future__ import annotations

import secrets
from datetime import timedelta

from django.db.models import Count, Q, Sum
from django.utils import timezone

from core.models import AuditLog
from core.services import create_audit_log
from manager.models import (
    AnalyticsEvent,
    DataSubjectRequest,
    ExportArtifact,
    ManagerSession,
    OperationalSnapshot,
    RetentionPolicy,
)
from manager.permissions import PERMISSIONS


class GovernanceError(ValueError):
    pass


DEFAULT_RETENTION = {
    "analytics_events": (730, "Business event history used by funnels and cohorts."),
    "operational_snapshots": (365, "Point-in-time operational gauges."),
    "audit_log": (365, "Security and administrative audit trail."),
    "campaign_records": (730, "Campaign definitions and delivery outcomes."),
    "export_files": (3, "Generated CSV/XLSX/PDF downloads."),
    "media_recordings": (90, "Audio and video sent in conversations, after the booking closes."),
}


def ensure_retention_policies():
    created = []
    for data_class, (days, notes) in DEFAULT_RETENTION.items():
        policy, was_created = RetentionPolicy.objects.get_or_create(
            data_class=data_class, defaults={"days": days, "notes": notes}
        )
        if was_created:
            created.append(data_class)
    return created


def retention_payload():
    ensure_retention_policies()
    return [
        {"data_class": row.data_class, "days": row.days, "enabled": row.enabled, "notes": row.notes,
         "updated_at": row.updated_at.isoformat()}
        for row in RetentionPolicy.objects.order_by("data_class")
    ]


def set_retention(*, user, data_class, days, enabled=True):
    policy = RetentionPolicy.objects.filter(data_class=data_class).first()
    if not policy:
        raise GovernanceError("Unknown data class.")
    before = {"days": policy.days, "enabled": policy.enabled}
    policy.days = int(days)
    policy.enabled = bool(enabled)
    policy.updated_by = user
    policy.full_clean(exclude=["updated_by"])
    policy.save(update_fields=["days", "enabled", "updated_by", "updated_at"])
    create_audit_log(action="MANAGER_RETENTION_CHANGED", resource_type="RETENTION_POLICY",
                     resource_id=data_class, actor=user,
                     metadata={"before": before, "after": {"days": policy.days, "enabled": policy.enabled}})
    return policy


def purge_expired_retention():
    """Delete only data classes whose policy allows deletion; financial records are excluded."""
    ensure_retention_policies()
    now = timezone.now()
    deleted = {}
    for policy in RetentionPolicy.objects.filter(enabled=True):
        cutoff = now - timedelta(days=policy.days)
        if policy.data_class == "analytics_events":
            count, _ = AnalyticsEvent.objects.filter(recorded_at__lt=cutoff).delete()
            deleted["analytics_events"] = count
        elif policy.data_class == "operational_snapshots":
            count, _ = OperationalSnapshot.objects.filter(captured_for__lt=cutoff).delete()
            deleted["operational_snapshots"] = count
        elif policy.data_class == "audit_log":
            count, _ = AuditLog.objects.filter(created_at__lt=cutoff).delete()
            deleted["audit_log"] = count
        elif policy.data_class == "export_files":
            from manager.services import exports as export_service

            deleted["export_files"] = export_service.purge_expired()["removed"]
        elif policy.data_class == "campaign_records":
            from manager.models import CampaignDelivery

            count, _ = CampaignDelivery.objects.filter(created_at__lt=cutoff).delete()
            deleted["campaign_deliveries"] = count
    return deleted


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #
def audit_page(*, page=1, size=50, action="", actor_id="", resource_type=""):
    qs = AuditLog.objects.select_related("actor").order_by("-created_at")
    if action:
        qs = qs.filter(action__startswith=action[:60])
    if actor_id:
        qs = qs.filter(actor_id=actor_id)
    if resource_type:
        qs = qs.filter(resource_type=resource_type[:40])
    total = qs.count()
    rows = []
    for entry in qs[(page - 1) * size: page * size]:
        metadata = entry.metadata if isinstance(entry.metadata, dict) else {}
        rows.append({
            "id": str(entry.id),
            "action": entry.action,
            "resource_type": entry.resource_type,
            "resource_id": entry.resource_id,
            "actor": (entry.actor.get_full_name() or entry.actor.email) if entry.actor_id else "System",
            "created_at": entry.created_at.isoformat(),
            "metadata_keys": sorted(metadata.keys()),
            "ip_hash_prefix": (entry.ip_hash or "")[:10],
        })
    return {"total": total, "page": page, "size": size, "results": rows}


def audit_filters():
    rows = AuditLog.objects.values("action").annotate(n=Count("pk")).order_by("-n")[:40]
    return [{"action": row["action"], "count": row["n"]} for row in rows]


# --------------------------------------------------------------------------- #
# Data subject requests
# --------------------------------------------------------------------------- #
def create_subject_request(*, user, subject_user_id, request_type, details=""):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    if request_type not in DataSubjectRequest.Type.values:
        raise GovernanceError("Choose a request type.")
    subject = User.objects.filter(pk=subject_user_id).first()
    if not subject:
        raise GovernanceError("That account does not exist.")
    reference = f"DSR-{secrets.token_hex(4).upper()}"
    request = DataSubjectRequest.objects.create(
        reference=reference, subject_user=subject, request_type=request_type,
        details=str(details or "")[:2000], handled_by=user,
    )
    create_audit_log(action="MANAGER_DATA_SUBJECT_REQUEST", resource_type="DATA_SUBJECT_REQUEST",
                     resource_id=reference, actor=user,
                     metadata={"type": request_type, "subject_hash": _hash(subject.pk)})
    return request


def _hash(value):
    import hashlib

    return hashlib.sha256(str(value).encode()).hexdigest()[:16]


def advance_subject_request(*, user, request, status, notes=""):
    if status not in DataSubjectRequest.Status.values:
        raise GovernanceError("Unknown status.")
    request.status = status
    request.outcome_notes = str(notes or "")[:4000]
    request.handled_by = user
    if status == DataSubjectRequest.Status.COMPLETED:
        request.completed_at = timezone.now()
    request.save(update_fields=["status", "outcome_notes", "handled_by", "completed_at", "updated_at"])
    create_audit_log(action="MANAGER_DATA_SUBJECT_UPDATED", resource_type="DATA_SUBJECT_REQUEST",
                     resource_id=request.reference, actor=user, metadata={"status": status})
    return request


def subject_request_payload(row):
    return {
        "reference": row.reference,
        "type": row.request_type,
        "status": row.status,
        "created_at": row.created_at.isoformat(),
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "subject_hash": _hash(row.subject_user_id),
        "details": row.details,
        "outcome_notes": row.outcome_notes,
    }


def privacy_overview():
    ensure_retention_policies()
    open_requests = DataSubjectRequest.objects.exclude(status=DataSubjectRequest.Status.COMPLETED)
    return {
        "data_classes": [
            {"key": "public", "label": "Public business information", "holders": "Everyone"},
            {"key": "internal", "label": "Internal operational information", "holders": "Managers with analytics.view"},
            {"key": "customer", "label": "Confidential customer information", "holders": "Managers with customer.view"},
            {"key": "artisan", "label": "Confidential artisan information", "holders": "Managers with artisan.view"},
            {"key": "financial", "label": "Financial information", "holders": "Managers with analytics.financial or refund.view"},
            {"key": "identity", "label": "Identity and verification information", "holders": "Managers with artisan.sensitive or customer.sensitive"},
            {"key": "secrets", "label": "Authentication secrets", "holders": "Never exposed through the console"},
            {"key": "security", "label": "Security and audit logs", "holders": "Managers with audit.view"},
        ],
        "permission_matrix": [
            {"permission": code, "label": label, "category": category}
            for code, (label, category) in PERMISSIONS.items()
        ],
        "retention": retention_payload(),
        "open_requests": open_requests.count(),
        "requests": [subject_request_payload(row) for row in DataSubjectRequest.objects.order_by("-created_at")[:25]],
        "export_ttl_hours": 72,
        "notes": [
            "Analytics events never contain emails, phone numbers, tokens, provider payloads or message bodies.",
            "Exports are written to a private directory and served only through an authorised endpoint.",
            "Deleting a customer never deletes payments, refunds or payouts: those are retained for financial and "
            "legal reasons and are documented in the retention policy.",
        ],
    }


def session_payload(user):
    return [
        {
            "id": str(row.id),
            "created_at": row.created_at.isoformat(),
            "last_seen_at": row.last_seen_at.isoformat(),
            "mfa_verified_at": row.mfa_verified_at.isoformat(),
            "ip_hash_prefix": (row.ip_hash or "")[:10],
            "current": False,
        }
        for row in ManagerSession.objects.filter(user=user, revoked_at__isnull=True).order_by("-last_seen_at")[:25]
    ]
