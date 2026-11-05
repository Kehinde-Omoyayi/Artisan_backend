"""Staff accounts, roles and permissions.

Privilege escalation rules enforced here:

* nobody can grant a permission they do not hold themselves;
* nobody can edit their own role or permissions;
* only a Super Administrator can create or promote to Super Administrator, and
  that needs a second approver;
* the last active Super Administrator cannot be removed or demoted.
"""
from __future__ import annotations

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from core.services import create_audit_log
from manager.models import ManagerProfile, ManagerRole
from manager.permissions import PERMISSIONS, SUPER_ADMIN, SYSTEM_ROLES
from manager.services.auth import ensure_system_roles, has_perm


class StaffError(ValueError):
    pass


def role_payload(role):
    return {
        "slug": role.slug,
        "name": role.name,
        "description": role.description,
        "is_system": role.is_system,
        "permissions": sorted(role.permissions or []),
        "member_count": role.members.count(),
    }


def list_roles():
    ensure_system_roles()
    return [role_payload(role) for role in ManagerRole.objects.order_by("-is_system", "name")]


def permission_catalogue():
    categories = {}
    for code, (label, category) in PERMISSIONS.items():
        categories.setdefault(category, []).append({"code": code, "label": label})
    return [{"category": name, "permissions": items} for name, items in categories.items()]


def create_role(*, user, slug, name, description="", permissions=None):
    if not has_perm(user, "staff.manage"):
        raise StaffError("You cannot manage roles.")
    slug = str(slug or "").strip().lower()
    if not slug.replace("-", "").isalnum() or len(slug) > 60:
        raise StaffError("Use a short slug of letters, digits and dashes.")
    chosen = {code for code in (permissions or []) if code in PERMISSIONS}
    _assert_no_escalation(user, chosen)
    if ManagerRole.objects.filter(slug=slug).exists():
        raise StaffError("A role with that slug already exists.")
    role = ManagerRole.objects.create(
        slug=slug, name=str(name or slug)[:80], description=str(description or "")[:240],
        is_system=False, permissions=sorted(chosen), created_by=user,
    )
    create_audit_log(action="MANAGER_ROLE_CREATED", resource_type="MANAGER_ROLE", resource_id=str(role.id),
                     actor=user, metadata={"slug": slug, "permissions": sorted(chosen)})
    return role


def _assert_no_escalation(user, permissions):
    from manager.services.auth import effective_permissions

    mine = effective_permissions(user)
    extra = set(permissions) - mine
    if extra:
        raise StaffError(f"You cannot grant permissions you do not hold: {', '.join(sorted(extra))}.")


def _active_super_admins(exclude_profile=None):
    qs = ManagerProfile.objects.filter(role__slug=SUPER_ADMIN, is_suspended=False, user__is_active=True)
    if exclude_profile:
        qs = qs.exclude(pk=exclude_profile.pk)
    return qs.count()


def assign_role(*, user, profile, role_slug):
    role = ManagerRole.objects.filter(slug=role_slug).first()
    if not role:
        raise StaffError("Unknown role.")
    if profile.user_id == user.pk:
        raise StaffError("You cannot change your own role.")
    if not has_perm(user, "staff.manage"):
        raise StaffError("You cannot change roles.")
    _assert_no_escalation(user, role.permissions or [])
    if role.slug == SUPER_ADMIN and not has_perm(user, "system.manage"):
        raise StaffError("Only a system administrator can promote another Super Administrator.")
    if profile.role.slug == SUPER_ADMIN and role.slug != SUPER_ADMIN and _active_super_admins(profile) == 0:
        raise StaffError("The last active Super Administrator cannot be demoted.")
    with transaction.atomic():
        previous = profile.role.slug
        profile.role = role
        profile.save(update_fields=["role", "updated_at"])
        create_audit_log(action="MANAGER_ROLE_ASSIGNED", resource_type="MANAGER_PROFILE",
                         resource_id=str(profile.id), actor=user,
                         metadata={"from": previous, "to": role.slug})
    return profile


def set_permissions(*, user, role, permissions):
    if not has_perm(user, "staff.manage"):
        raise StaffError("You cannot change permissions.")
    if role.is_system:
        raise StaffError("System roles are fixed. Create a custom role instead.")
    chosen = {code for code in permissions if code in PERMISSIONS}
    _assert_no_escalation(user, chosen)
    before = sorted(role.permissions or [])
    role.permissions = sorted(chosen)
    role.save(update_fields=["permissions", "updated_at"])
    create_audit_log(action="MANAGER_PERMISSIONS_CHANGED", resource_type="MANAGER_ROLE",
                     resource_id=str(role.id), actor=user,
                     metadata={"before": before, "after": sorted(chosen)})
    return role


def create_staff(*, user, email, first_name, last_name, role_slug, job_title=""):
    if not has_perm(user, "staff.manage"):
        raise StaffError("You cannot create staff accounts.")
    from django.contrib.auth import get_user_model
    from django.utils.crypto import get_random_string

    User = get_user_model()
    email = str(email or "").strip().casefold()
    if "@" not in email:
        raise StaffError("Enter a valid email address.")
    if User.objects.filter(email=email).exists():
        raise StaffError("An account with that email already exists.")
    role = ManagerRole.objects.filter(slug=role_slug).first()
    if not role:
        raise StaffError("Unknown role.")
    _assert_no_escalation(user, role.permissions or [])
    if role.slug == SUPER_ADMIN and not has_perm(user, "system.manage"):
        raise StaffError("Only a system administrator can create a Super Administrator.")
    temporary_password = get_random_string(20)
    with transaction.atomic():
        account = User.objects.create_user(
            email=email, password=temporary_password,
            first_name=str(first_name or "")[:40], last_name=str(last_name or "")[:40],
            is_staff=True, is_active=True,
        )
        profile = ManagerProfile.objects.create(
            user=account, role=role, job_title=str(job_title or "")[:120],
            created_by=user, must_change_password=True,
        )
        create_audit_log(action="MANAGER_STAFF_CREATED", resource_type="MANAGER_PROFILE",
                         resource_id=str(profile.id), actor=user,
                         metadata={"role": role.slug, "email_hash": _hash(email)})
    return account, profile, temporary_password


def _hash(value):
    import hashlib

    return hashlib.sha256(str(value).encode()).hexdigest()[:16]


def suspend(*, user, profile, reason):
    if profile.user_id == user.pk:
        raise StaffError("You cannot suspend your own account.")
    if profile.role.slug == SUPER_ADMIN and _active_super_admins(profile) == 0:
        raise StaffError("The last active Super Administrator cannot be suspended.")
    profile.is_suspended = True
    profile.suspension_reason = str(reason or "")[:240]
    profile.save(update_fields=["is_suspended", "suspension_reason", "updated_at"])
    from manager.models import ManagerSession

    ManagerSession.objects.filter(user=profile.user, revoked_at__isnull=True).update(
        revoked_at=timezone.now(), revoked_reason="Account suspended")
    create_audit_log(action="MANAGER_STAFF_SUSPENDED", resource_type="MANAGER_PROFILE",
                     resource_id=str(profile.id), actor=user, metadata={"reason": str(reason)[:200]})
    return profile


def restore(*, user, profile):
    profile.is_suspended = False
    profile.suspension_reason = ""
    profile.save(update_fields=["is_suspended", "suspension_reason", "updated_at"])
    create_audit_log(action="MANAGER_STAFF_RESTORED", resource_type="MANAGER_PROFILE",
                     resource_id=str(profile.id), actor=user, metadata={})
    return profile


def list_staff():
    rows = (
        ManagerProfile.objects.select_related("user", "role")
        .annotate(active_sessions=Count("user__manager_sessions",
                                        filter=Q(user__manager_sessions__revoked_at__isnull=True)))
        .order_by("role__name", "user__first_name")
    )
    return [
        {
            "id": str(row.id),
            "name": row.user.get_full_name() or row.user.email,
            "email": row.user.email,
            "role": {"slug": row.role.slug, "name": row.role.name},
            "job_title": row.job_title,
            "is_active": row.user.is_active,
            "is_suspended": row.is_suspended,
            "suspension_reason": row.suspension_reason,
            "totp_enrolled": row.totp_enrolled,
            "email_otp_enabled": row.email_otp_enabled,
            "must_change_password": row.must_change_password,
            "last_authenticated_at": row.last_authenticated_at.isoformat() if row.last_authenticated_at else None,
            "active_sessions": row.active_sessions,
        }
        for row in rows
    ]


def system_role_summary():
    return [
        {"slug": slug, "name": spec["name"], "description": spec["description"],
         "permissions": spec["permissions"]}
        for slug, spec in SYSTEM_ROLES.items()
    ]
