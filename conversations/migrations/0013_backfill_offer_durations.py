"""Backfill explicit duration fields for historical offers.

Offers predating the duration fields derived their "duration" implicitly from
the scheduled interval; carry that into duration_value/duration_unit so legacy
rows keep a coherent duration for display and booking snapshots. Idempotent:
rows already set (duration written by the new offer flow) are untouched.
"""
import math

from django.db import migrations


def backfill_offer_durations(apps, schema_editor):
    Offer = apps.get_model("conversations", "Offer")
    for offer in Offer.objects.all().iterator():
        delta = offer.scheduled_end - offer.scheduled_start
        days = max(1, math.ceil(delta.total_seconds() / 86400))
        offer.duration_value = days
        offer.duration_unit = "DAYS"
        offer.save(update_fields=["duration_value", "duration_unit"])


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0012_conversation_description_confirmed_at_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill_offer_durations, migrations.RunPython.noop),
    ]
