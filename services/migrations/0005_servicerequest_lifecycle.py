from django.db import migrations, models


def backfill_attended(apps, schema_editor):
    """Requests that already have a booking were attended to before this field existed."""
    ServiceRequest = apps.get_model("services", "ServiceRequest")
    Booking = apps.get_model("booking", "Booking")

    bookings = Booking.objects.filter(status__in=["ACCEPTED", "IN_PROGRESS", "COMPLETION_REQUESTED", "COMPLETED", "DISPUTED"]).order_by("created_at")
    for booking in bookings.iterator():
        ServiceRequest.objects.filter(pk=booking.request_id, status="OPEN").update(
            status="ATTENDED", attended_at=booking.created_at
        )


class Migration(migrations.Migration):

    dependencies = [
        ("booking", "0001_initial"),
        ("services", "0004_servicerequest_preferred_time_and_details"),
    ]

    operations = [
        migrations.AddField(
            model_name="servicerequest",
            name="status",
            field=models.CharField(
                choices=[
                    ("OPEN", "Open"),
                    ("ATTENDED", "Attended"),
                    ("ARCHIVED", "Archived"),
                ],
                default="OPEN",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="servicerequest",
            name="attended_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="servicerequest",
            name="archived_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddIndex(
            model_name="servicerequest",
            index=models.Index(
                fields=["requester", "status"], name="svc_req_requester_status_idx"
            ),
        ),
        migrations.RunPython(backfill_attended, migrations.RunPython.noop),
    ]