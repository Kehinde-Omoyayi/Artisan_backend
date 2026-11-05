"""Safe, reusable formatting for API failures shown in rendered frontend pages."""

import re


_METADATA_KEYS = {
    "code",
    "error_code",
    "request_id",
    "requestid",
    "correlation_id",
    "trace_id",
    "trace",
    "traceback",
    "stack",
    "stacktrace",
    "debug",
    "exception",
    "status",
    "status_code",
    "headers",
    "profile_role",
}
_MESSAGE_KEYS = {"message", "detail", "error", "non_field_errors", "non_field_error"}
_TRACE_LINE = re.compile(
    r"(?im)^\s*(?:traceback \(most recent call last\)|file \"|"
    r"(?:at )?\w+(?:error|exception):|stack trace\b)"
)
_HTML_PREFIX = re.compile(r"(?is)^\s*(?:<!doctype html|<html|<body)")


def _safe_text(value):
    if not isinstance(value, str):
        return ""
    text = " ".join(value.strip().split())
    if not text or _TRACE_LINE.search(text) or _HTML_PREFIX.search(text):
        return ""
    if re.fullmatch(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+", text):
        return ""
    if text.casefold() in {"internal server error", "server error", "bad gateway"}:
        return ""
    return text


def _is_metadata(key, value):
    """True when a key is transport metadata rather than a field the user can fix.

    ``code`` is metadata when its value is a token ("PERMISSION_DENIED") but a real
    field when its value is a sentence ("That authenticator code is not valid.").
    """
    normalized = str(key).strip().casefold().replace("-", "_")
    if normalized not in _METADATA_KEYS:
        return False
    if isinstance(value, str) and " " in value.strip() and _safe_text(value):
        return False
    return True


def _field_label(key):
    key = str(key).replace("_", " ").replace(".", " ").strip()
    if not key:
        return ""
    return key if key.isupper() else key[:1].upper() + key[1:].lower()


def _collect(value, *, label=""):
    parts = []
    if isinstance(value, str):
        text = _safe_text(value)
        if text:
            parts.append(f"{label}: {text}" if label else text)
    elif isinstance(value, (list, tuple)):
        for item in value:
            parts.extend(_collect(item, label=label))
    elif isinstance(value, dict):
        # Support serializer detail entries shaped as {"field": "phone", "message": "..."}.
        explicit_field = value.get("field") if isinstance(value.get("field"), str) else ""
        explicit_message = value.get("message")
        if explicit_field and explicit_message is not None:
            parts.extend(_collect(explicit_message, label=_field_label(explicit_field)))
            return parts
        for key, child in value.items():
            normalized = str(key).strip().casefold().replace("-", "_")
            if normalized.startswith("trace"):
                continue
            if _is_metadata(key, child):
                continue
            if normalized in _MESSAGE_KEYS:
                parts.extend(_collect(child, label=label))
                continue
            if normalized == "missing_fields":
                values = child if isinstance(child, (list, tuple)) else [child]
                field_names = [_field_label(item) for item in values if isinstance(item, str) and item.strip()]
                if field_names:
                    field_label = _field_label(key)
                    if label:
                        field_label = f"{label} · {field_label}"
                    parts.append(f"{field_label}: {', '.join(field_names)}" if field_label else ", ".join(field_names))
                continue
            child_label = _field_label(key)
            if label:
                child_label = f"{label} · {child_label}" if child_label else label
            parts.extend(_collect(child, label=child_label))
    return parts


def _deduplicate(parts):
    result = []
    seen = set()
    for part in parts:
        normalized = re.sub(r"[^a-z0-9]+", " ", part.casefold()).strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(part)
    return result


def _fallback(status_code=None, *, network_error=False):
    if network_error:
        return "We couldn’t connect to the marketplace. Check your connection and try again."
    if status_code == 401:
        return "Your session may have expired. Please sign in again."
    if status_code == 403:
        return "You don’t have permission to perform this action."
    if status_code == 404:
        return "The requested item could not be found."
    if status_code == 429:
        return "Too many requests. Please wait a moment and try again."
    if status_code == 400:
        return "Please check the highlighted fields and try again."
    if status_code is not None and status_code >= 500:
        return "The service is temporarily unavailable. Please try again shortly."
    return "The request could not be completed. Please try again."


def normalize_api_error(payload=None, status_code=None, *, network_error=False):
    """Combine a safe main message and useful validation details without duplicates."""
    main_parts = []
    detail_parts = []

    if isinstance(payload, dict):
        for key in ("message", "detail", "error"):
            value = payload.get(key)
            if value is not None:
                main_parts.extend(_collect(value))

        if "details" in payload:
            detail_parts.extend(_collect(payload.get("details")))
        else:
            for key, value in payload.items():
                normalized = str(key).strip().casefold().replace("-", "_")
                if normalized in _MESSAGE_KEYS or normalized == "details":
                    continue
                if _is_metadata(key, value):
                    continue
                detail_parts.extend(_collect({key: value}))
    elif isinstance(payload, (list, tuple)):
        detail_parts.extend(_collect(payload))
    elif isinstance(payload, str):
        main_parts.extend(_collect(payload))

    main_parts = _deduplicate(main_parts)
    detail_parts = _deduplicate(detail_parts)
    if not main_parts and detail_parts:
        return " · ".join(detail_parts)

    message = " · ".join(main_parts)
    main_text = {
        re.sub(r"^[^:]+:\s*", "", item).casefold()
        for item in main_parts
    }
    remaining = [
        part for part in detail_parts
        if re.sub(r"^[^:]+:\s*", "", part).casefold() not in main_text
    ]
    if remaining:
        message = f"{message} · {' · '.join(remaining)}" if message else " · ".join(remaining)
    return message or _fallback(status_code, network_error=network_error)


# Kept as a compatibility alias while every view migrates to the generic formatter.
flatten_errors = normalize_api_error
