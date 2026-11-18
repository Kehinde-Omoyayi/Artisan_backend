"""Shared international phone-number normalization for user profile data."""

from phonenumbers import (
    COUNTRY_CODE_TO_REGION_CODE,
    NumberParseException,
    PhoneNumberFormat,
    format_number,
    is_valid_number,
    national_significant_number,
    parse,
)
from rest_framework.exceptions import ValidationError


DEFAULT_PHONE_REGION = "NG"


def phone_is_valid(value, *, default_region=DEFAULT_PHONE_REGION):
    """Validate a stored number without modifying it (legacy rows are read-only here)."""
    if not isinstance(value, str) or not value.strip():
        return False
    candidate = value.strip()
    try:
        number = parse(candidate, None if candidate.startswith("+") else default_region)
    except (NumberParseException, TypeError, ValueError):
        return False
    return bool(is_valid_number(number) and not number.extension)


def normalize_phone(value, *, default_region=DEFAULT_PHONE_REGION):
    """Return a validated E.164 number, accepting legacy national input on profile edits.

    Existing stored values are not changed unless the user submits the phone field. For
    unprefixed input, Nigeria is the historical/default region; international values must
    include their plus-prefixed calling code.
    """
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValidationError("Enter a valid telephone number.")

    candidate = value.strip()
    if not candidate:
        return None
    if len(candidate) > 80:
        raise ValidationError("Enter a valid telephone number.")

    try:
        number = parse(candidate, None if candidate.startswith("+") else default_region)
    except (NumberParseException, TypeError, ValueError) as exc:
        raise ValidationError(
            "Enter a valid phone number with its country calling code."
        ) from exc

    if number.extension:
        raise ValidationError("Enter a phone number without an extension.")
    if not is_valid_number(number):
        raise ValidationError(
            "Enter a valid phone number with the selected country calling code."
        )
    return format_number(number, PhoneNumberFormat.E164)


def calling_code_for_phone(value, *, default_region=DEFAULT_PHONE_REGION):
    """Return the numeric E.164 calling code for a valid phone, or an empty string."""
    if not phone_is_valid(value, default_region=default_region):
        return ""
    try:
        number = parse(value.strip(), None if value.strip().startswith("+") else default_region)
    except (NumberParseException, TypeError, ValueError):
        return ""
    return f"+{number.country_code}"


def national_input_for_phone(value, *, default_region=DEFAULT_PHONE_REGION):
    """Split a valid stored number for a calling-code + national-number form control."""
    if not phone_is_valid(value, default_region=default_region):
        return None
    try:
        number = parse(value.strip(), None if value.strip().startswith("+") else default_region)
    except (NumberParseException, TypeError, ValueError):
        return None
    return f"+{number.country_code}", national_significant_number(number)


def calling_code_choices():
    """Unique calling codes for server-side ChoiceField validation (JS supplies names)."""
    return [(f"+{code}", f"+{code}") for code in sorted(COUNTRY_CODE_TO_REGION_CODE)]
