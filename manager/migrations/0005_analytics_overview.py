"""Seed the new ``analytics.overview`` permission to the two intended roles.

``analytics.overview`` (platform management overview) is granted to Super
Administrators and General Managers only. Finance Manager, Support Agent and
every other seeded role are deliberately left untouched, and no custom role is
modified by this migration.
"""
from django.db import migrations


NEW_CODE = "analytics.overview"
GRANTEE_SLUGS = ("super-admin", "general-manager")


def grant_analytics_overview(apps, schema_editor):
    ManagerRole = apps.get_model("manager", "ManagerRole")
    for role in ManagerRole.objects.filter(slug__in=GRANTEE_SLUGS):
        permissions = list(dict.fromkeys([*(role.permissions or []), NEW_CODE]))
        if permissions != list(role.permissions or []):
            role.permissions = permissions
            role.save(update_fields=["permissions", "updated_at"])


def revoke_analytics_overview(apps, schema_editor):
    ManagerRole = apps.get_model("manager", "ManagerRole")
    for role in ManagerRole.objects.filter(slug__in=GRANTEE_SLUGS):
        permissions = [code for code in (role.permissions or []) if code != NEW_CODE]
        if permissions != list(role.permissions or []):
            role.permissions = permissions
            role.save(update_fields=["permissions", "updated_at"])


class Migration(migrations.Migration):

    dependencies = [
        ("manager", "0004_alter_operationalsnapshot_interval_seconds_and_more"),
    ]

    operations = [
        migrations.RunPython(grant_analytics_overview, revoke_analytics_overview),
    ]
