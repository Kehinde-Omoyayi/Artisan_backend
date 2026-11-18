"""Asynchronous exports.

The request only validates the spec, authorises it and enqueues a job. The worker
streams rows in chunks into a file under ``MEDIA_ROOT/manager_exports`` (private,
never publicly served), records the row count and checksum, and the console
downloads through an authorised view.
"""
from __future__ import annotations

import csv
import hashlib
import io
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

from core.services import create_audit_log
from manager.models import ExportArtifact
from manager.services import jobs
from manager.services.data_explorer import MAX_EXPORT_ROWS, run

EXPORT_TTL_HOURS = 72
SENSITIVE_FIELDS = {"email", "phone_number"}


def storage_dir():
    from pathlib import Path

    root = Path(getattr(settings, "MEDIA_ROOT", "/tmp")) / "manager_exports"
    root.mkdir(parents=True, exist_ok=True)
    return root


def request_export(*, user, spec, fmt="csv", sensitive=False):
    if fmt not in {"csv", "xlsx", "pdf"}:
        raise jobs.JobError("Choose a supported export format.")
    # Validate before queueing so an invalid spec fails fast and cheaply.
    preview = run(spec, limit=5)
    if preview["row_count"] == 0 and preview["chart"] != "kpi":
        payload = {"empty": True}
    else:
        payload = {"empty": False}
    job, created = jobs.enqueue(
        kind="EXPORT",
        payload={"spec": spec, "format": fmt, "sensitive": bool(sensitive)},
        user=user,
        dedupe_key=f"export:{preview['spec_hash']}:{fmt}",
        queue="exports",
    )
    create_audit_log(action="MANAGER_EXPORT_REQUESTED", resource_type="BACKGROUND_JOB",
                     resource_id=str(job.id), actor=user,
                     metadata={"domain": preview["domain"], "format": fmt, "sensitive": bool(sensitive)})
    return job


def build_export(job):
    from manager.services import jobs as job_service

    if job.requested_by_id is None:
        # Never leave a half-written artefact behind: an export belongs to somebody.
        job_service.mark_failed(job.pk, "This export has no requesting manager. Request it again.")
        return None

    spec = (job.payload or {}).get("spec") or {}
    fmt = (job.payload or {}).get("format") or "csv"
    data = run(spec, limit=MAX_EXPORT_ROWS)
    rows = data["rows"]
    columns = ["group", "value"]
    headers = [data.get("dimension_label") or "Group", data.get("measure_label") or "Value"]
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(headers)
    for row in rows[:MAX_EXPORT_ROWS]:
        writer.writerow([row["group"], _number(row["value"])])
    content = buffer.getvalue()
    content_type = "text/csv"
    extension = "csv"
    if fmt == "xlsx":
        content, content_type, extension = _to_xlsx(headers, rows), (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"), "xlsx"
    elif fmt == "pdf":
        content, content_type, extension = _to_pdf(data, rows), "application/pdf", "pdf"

    encoded = content.encode() if isinstance(content, str) else content
    checksum = hashlib.sha256(encoded).hexdigest()
    filename = f"{data['domain']}-{timezone.now():%Y%m%d-%H%M%S}.{extension}"
    path = storage_dir() / filename
    path.write_bytes(encoded)
    # Celery delivers at least once. A redelivered job must reuse the artifact it
    # already produced instead of dying on the one-to-one constraint.
    artifact = ExportArtifact.objects.filter(job=job).first()
    if artifact:
        job_service.mark_succeeded(job.id, {"filename": artifact.filename, "rows": artifact.row_count,
                                            "checksum": artifact.checksum[:16]})
        return artifact
    artifact = ExportArtifact.objects.create(
        job=job,
        filename=filename,
        content_type=content_type,
        row_count=len(rows),
        byte_size=len(encoded),
        storage_key=str(path),
        checksum=checksum,
        requested_by=job.requested_by,
        expires_at=timezone.now() + timedelta(hours=EXPORT_TTL_HOURS),
    )
    job_service.mark_succeeded(job.id, {"filename": filename, "rows": len(rows), "checksum": checksum[:16]})
    return artifact


def _number(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return value


def _to_xlsx(headers, rows):
    from openpyxl import Workbook

    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet("Export")
    sheet.append(headers)
    for row in rows:
        sheet.append([row["group"], _number(row["value"])])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _to_pdf(data, rows):
    """A small, dependency-free PDF writer: one page per 40 rows."""
    lines = [f"{data['domain_label']} - {data['measure_label']}", f"Range: {data['range']['label']}", ""]
    for row in rows[:400]:
        lines.append(f"{row['group'][:60]:<60} {_number(row['value'])}")
    pages = [lines[i:i + 44] for i in range(0, len(lines), 44)] or [["No data"]]
    objects = []
    for page_lines in pages:
        text = "\\n".join(
            line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") for line in page_lines
        )
        content = f"BT /F1 10 Tf 40 780 Td 12 TL ({text}) Tj ET"
        objects.append(content)
    stream = "".join(objects)
    pdf = (
        "%PDF-1.4\n"
        f"% {data['generated_at']}\n"
        "1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        f"2 0 obj<</Type/Pages/Count {len(objects)}/Kids [{' '.join(f'{3 + i * 2} 0 R' for i in range(len(objects)))}]>>endobj\n"
    )
    for index, content in enumerate(objects):
        page_obj = 3 + index * 2
        content_obj = page_obj + 1
        pdf += f"{page_obj} 0 obj<</Type/Page/Parent 2 0 R/MediaBox [0 0 595 842]/Contents {content_obj} 0 R/Resources<</Font<</F1 999 0 R>>>>>>endobj\n"
        pdf += f"{content_obj} 0 obj<</Length {len(content)}>>stream\n{content}\nendstream endobj\n"
    pdf += "999 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
    pdf += "trailer<</Root 1 0 R>>\n%%EOF\n"
    return pdf


def purge_expired():
    removed = 0
    for artifact in ExportArtifact.objects.filter(expires_at__lt=timezone.now()):
        try:
            from pathlib import Path

            Path(artifact.storage_key).unlink(missing_ok=True)
        except OSError:
            pass
        artifact.delete()
        removed += 1
    return {"removed": removed}


def download_payload(artifact):
    from pathlib import Path

    path = Path(artifact.storage_key)
    if not path.exists():
        raise FileNotFoundError("The export file has expired or was removed.")
    return path, artifact.content_type, artifact.filename
