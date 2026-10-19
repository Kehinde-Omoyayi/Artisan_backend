import logging

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import DatabaseError, IntegrityError
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

logger = logging.getLogger(__name__)


def _message(data, status_code):
    if isinstance(data, dict):
        for key in ("message", "detail", "error"):
            value = data.get(key)
            if isinstance(value, str) and value.strip(): return value.strip()
        return "Validation failed." if status_code == 400 else "The request could not be completed."
    if isinstance(data, list): return "Validation failed." if status_code == 400 else "The request could not be completed."
    return str(data or "The request could not be completed.")


def _infrastructure_error(exc):
    """Map cache/broker/database outages to an intentional 503 instead of an unhandled 500."""
    try:
        from redis.exceptions import RedisError
        from django_redis.exceptions import ConnectionInterrupted
        if isinstance(exc, (RedisError, ConnectionInterrupted)): return "cache"
    except ImportError:
        pass
    try:
        from kombu.exceptions import OperationalError
        if isinstance(exc, OperationalError): return "broker"
    except ImportError:
        pass
    from django.db import OperationalError as DbOperationalError
    if isinstance(exc, DbOperationalError): return "database"
    return None


def handler(exc, context):
    request = context.get("request")
    request_id = getattr(request, "request_id", "")
    if isinstance(exc, DjangoValidationError):
        # Django validators (e.g. password validation) raised outside a serializer must still be a 400.
        details = exc.message_dict if hasattr(exc, "error_dict") else {"non_field_errors": list(exc.messages)}
        message = next(iter(exc.messages), "Validation failed.")
        return Response({"code": "INVALID", "message": message, "detail": message, "details": details, "request_id": request_id}, status=status.HTTP_400_BAD_REQUEST)
    if isinstance(exc, IntegrityError):
        logger.warning("integrity_error_mapped_to_conflict request_id=%s", request_id, exc_info=True)
        message = "The request conflicts with the current state of the resource."
        return Response({"code": "CONFLICT", "message": message, "detail": message, "details": {}, "request_id": request_id}, status=status.HTTP_409_CONFLICT)
    response = drf_exception_handler(exc, context)
    if response is None:
        component = _infrastructure_error(exc)
        if component is None: return None
        logger.error("infrastructure_unavailable component=%s request_id=%s", component, request_id, exc_info=True)
        message = "The service is temporarily unavailable. Please retry shortly."
        response = Response({"code": "SERVICE_UNAVAILABLE", "message": message, "detail": message, "details": {}, "request_id": request_id}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        response["Retry-After"] = "5"
        return response
    original = response.data
    message = _message(original, response.status_code)
    code = getattr(exc, "default_code", None) or getattr(exc, "code", None) or f"HTTP_{response.status_code}"
    details = original if isinstance(original, (dict, list)) else {}
    if (
        str(code).upper() == "PROFILE_INCOMPLETE"
        and isinstance(original, dict)
        and isinstance(original.get("details"), dict)
    ):
        details = original["details"]
    response.data = {"code": str(code).upper(), "message": message, "detail": message, "details": details, "request_id": request_id}
    return response
