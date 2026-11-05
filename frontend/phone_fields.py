"""Phone controls for the standalone rendered frontend."""

import re

import phonenumbers
from django import forms
from phonenumbers import COUNTRY_CODE_TO_REGION_CODE, NumberParseException, PhoneNumberFormat

DEFAULT_CALLING_CODE = "+234"
DEFAULT_REGION = "NG"


def calling_code_choices():
    return [(f"+{code}", f"+{code}") for code in sorted(COUNTRY_CODE_TO_REGION_CODE)]


def is_valid_phone(value):
    if not isinstance(value, str) or not value.strip():
        return False
    candidate = value.strip()
    try:
        parsed = phonenumbers.parse(candidate, None if candidate.startswith("+") else DEFAULT_REGION)
    except (NumberParseException, TypeError, ValueError):
        return False
    return bool(phonenumbers.is_valid_number(parsed) and not parsed.extension)


def _split_existing(value):
    if not isinstance(value, str) or not value.strip():
        return DEFAULT_CALLING_CODE, ""
    candidate = value.strip()
    try:
        parsed = phonenumbers.parse(candidate, None if candidate.startswith("+") else DEFAULT_REGION)
    except (NumberParseException, TypeError, ValueError):
        return DEFAULT_CALLING_CODE, candidate
    if not phonenumbers.is_valid_number(parsed) or parsed.extension:
        return DEFAULT_CALLING_CODE, candidate
    return f"+{parsed.country_code}", phonenumbers.national_significant_number(parsed)


def initialize_phone_fields(form):
    """Show old values in split controls while retaining the exact stored value for no-op edits."""
    original = form.initial.get("phone_number", "")
    code, local = _split_existing(original)
    form.initial["phone_country_calling_code"] = code
    form.initial["phone_number"] = local
    form._original_phone_value = original
    form._original_phone_code = code
    form._original_phone_local = local


def _normalized_from_controls(local, calling_code):
    candidate = str(local or "").strip()
    if not candidate:
        return None
    if candidate.startswith("+"):
        raw = candidate
    else:
        digits = re.sub(r"\D", "", candidate)
        if not digits:
            raise forms.ValidationError("Enter a valid phone number.")
        if calling_code == "+234" and digits.startswith("0"):
            digits = digits[1:]
        raw = f"{calling_code}{digits}"
    try:
        parsed = phonenumbers.parse(raw, None)
    except (NumberParseException, TypeError, ValueError) as exc:
        raise forms.ValidationError("Enter a valid phone number with the selected country calling code.") from exc
    if parsed.extension:
        raise forms.ValidationError("Enter a phone number without an extension.")
    if not phonenumbers.is_valid_number(parsed):
        raise forms.ValidationError("Enter a valid phone number with the selected country calling code.")
    return phonenumbers.format_number(parsed, PhoneNumberFormat.E164)


def clean_phone_fields(form, cleaned):
    local = str(cleaned.get("phone_number") or "").strip()
    code = str(cleaned.get("phone_country_calling_code") or DEFAULT_CALLING_CODE)
    if code not in {value for value, _label in calling_code_choices()}:
        form.add_error("phone_country_calling_code", "Select a valid country calling code.")
        code = DEFAULT_CALLING_CODE

    original = getattr(form, "_original_phone_value", "")
    is_unchanged = (
        local == str(getattr(form, "_original_phone_local", ""))
        and code == str(getattr(form, "_original_phone_code", DEFAULT_CALLING_CODE))
    )
    if is_unchanged:
        if form.fields["phone_number"].required and original and not is_valid_phone(str(original)):
            form.add_error("phone_number", "Re-enter the phone number using a valid country calling code.")
        cleaned["phone_number"] = original or None
        cleaned["_phone_number_unchanged"] = True
    else:
        try:
            cleaned["phone_number"] = _normalized_from_controls(local, code)
            cleaned["_phone_number_unchanged"] = False
        except forms.ValidationError as exc:
            form.add_error("phone_number", exc)

    cleaned.pop("phone_country_calling_code", None)
    return cleaned
