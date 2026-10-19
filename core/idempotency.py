"""Idempotency support for state-changing API operations.

Design
------
* The idempotency key, the business effects and the stored response commit **atomically** for database-only
  operations (``atomic=True``, the default). A crash or exception rolls everything back, so a retry with the
  same key safely re-executes; a success is replayed byte-for-byte.
* Only successful (2xx) responses are stored. A failed attempt leaves no record, so a client that fixes the
  problem can retry with the same key.
* The request fingerprint covers the HTTP method, the full path (so the target resource) and the body. Reusing
  one key for a different resource or a different body is rejected instead of silently replaying.
* Operations that call external providers (``atomic=False``) must not hold a database transaction open across
  network calls. They claim the key first, run, then store the response; a claim that never received a
  response is considered abandoned after ``IDEMPOTENCY_LEASE_SECONDS``.
"""
import logging
from functools import wraps

from django.db import transaction
from rest_framework import status
from rest_framework.exceptions import APIException
from rest_framework.response import Response

from .models import IdempotencyRecord
from .services import get_or_create_idempotency, json_safe, store_idempotency_response

logger = logging.getLogger(__name__)


class IdempotencyInProgress(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "idempotency_in_progress"
    default_detail = "The same operation is already being processed. Retry with the same Idempotency-Key later."


def _request_payload(request):
    data = getattr(request, "data", None)
    if hasattr(data, "lists"):
        return {key: values if len(values) > 1 else values[0] for key, values in data.lists()}
    if isinstance(data, dict):
        return data
    return {"data": str(data)} if data not in (None, "") else {}


def begin(actor, operation, key, payload, resource_type="", resource_id=""):
    """Claim ``key``. Returns ``(record, created, cached)`` where ``cached`` is ``(status, body)`` for a replay.

    Raises :class:`IdempotencyInProgress` when the key is claimed but no response has been stored yet.
    """
    record, created = get_or_create_idempotency(actor=actor, operation=operation, key=key, payload=payload, resource_type=resource_type, resource_id=resource_id)
    if created:
        return record, True, None
    if record.response_status is None:
        raise IdempotencyInProgress()
    return record, False, (record.response_status, record.response_body)


def finish(record, *, status_code, body, resource_type="", resource_id=""):
    store_idempotency_response(
        record,
        status_code=status_code,
        body=body if isinstance(body, (dict, list)) else {"detail": str(body)},
        resource_type=resource_type,
        resource_id=resource_id,
    )


def abort(record):
    IdempotencyRecord.objects.filter(pk=record.pk, response_status__isnull=True).delete()


def replay_response(cached):
    response = Response(cached[1], status=cached[0])
    response["Idempotent-Replayed"] = "true"
    return response


def _extract_key(request):
    header = str(request.headers.get("Idempotency-Key") or "").strip()
    if header:
        return header
    data = getattr(request, "data", None)
    if hasattr(data, "get"):
        return str(data.get("idempotency_key") or "").strip()
    return ""


def _audit(request, operation, resource_type, resource_id):
    try:
        from .audit import audit_request_action
        audit_request_action(request=request, action=operation.upper().replace(".", "_"), resource_type=resource_type, resource_id=resource_id)
    except Exception:  # audit must never break the business operation
        logger.exception("idempotency_audit_failed operation=%s resource=%s", operation, resource_id)


def idempotent_action(operation, *, resource_type="Resource", require_key=True, atomic=True):
    def decorator(view_func):
        @wraps(view_func)
        def wrapped(self, request, *args, **kwargs):
            key = _extract_key(request)
            if not key:
                if require_key:
                    return Response({"code": "IDEMPOTENCY_KEY_REQUIRED", "message": "Idempotency-Key is required for this operation.", "detail": "Idempotency-Key is required for this operation."}, status=status.HTTP_400_BAD_REQUEST)
                return view_func(self, request, *args, **kwargs)
            resource_id = str(kwargs.get("pk") or kwargs.get("booking_id") or kwargs.get("conversation_id") or kwargs.get("offer_id") or "")
            payload = {"method": request.method, "path": request.path, "body": _request_payload(request)}

            if atomic:
                with transaction.atomic():
                    record, created, cached = begin(actor=request.user, operation=operation, key=key, payload=payload, resource_type=resource_type, resource_id=resource_id)
                    if not created:
                        return replay_response(cached)
                    response = view_func(self, request, *args, **kwargs)
                    if 200 <= response.status_code < 300:
                        finish(record, status_code=response.status_code, body=response.data, resource_type=resource_type, resource_id=resource_id)
                        _audit(request, operation, resource_type, resource_id)
                    else:
                        # A rejected attempt must leave neither side effects nor a claimed key behind.
                        transaction.set_rollback(True)
                    return response

            record, created, cached = begin(actor=request.user, operation=operation, key=key, payload=payload, resource_type=resource_type, resource_id=resource_id)
            if not created:
                return replay_response(cached)
            try:
                response = view_func(self, request, *args, **kwargs)
            except BaseException:
                abort(record)
                raise
            if 200 <= response.status_code < 300:
                finish(record, status_code=response.status_code, body=json_safe(response.data), resource_type=resource_type, resource_id=resource_id)
                _audit(request, operation, resource_type, resource_id)
            else:
                abort(record)
            return response
        return wrapped
    return decorator
