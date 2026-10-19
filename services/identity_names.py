"""Name comparison helpers for identity verification and payout controls.

Provider identity names are kept in memory only unless encrypted by ``kyc_crypto``.
This module deliberately never logs or formats identity values in an error message.
"""
import re
import unicodedata


PROFILE_NAME_CORRECTION_MESSAGE = (
    "The name on your identity record does not match your artisan profile. "
    "Update the first and last name in your profile so they match your identity "
    "record, then complete identity verification again. Your profile has not "
    "been verified."
)

IDENTITY_NAME_UNAVAILABLE_MESSAGE = (
    "We could not confirm a complete name from the verification service, so "
    "your profile has not been verified. Please try again later."
)

ACCOUNT_NAME_MISMATCH_MESSAGE = (
    "The bank account name must match the name on your verified identity and "
    "artisan profile. Please check the account name and your profile details."
)


_HONORIFICS = {"mr", "mrs", "ms", "miss", "dr"}
_NAME_MATCH_FIELDS = {
    "match",
    "matched",
    "ismatched",
    "matchstatus",
    "namematch",
    "isnamematch",
    "namematched",
    "namematchstatus",
    "identitymatch",
    "isidentitymatch",
    "identitymatched",
    "identitymatchstatus",
}
_NAME_MISMATCH_FIELDS = {"mismatch", "mismatched", "namemismatch", "identitymismatch"}
_FALSE_VALUES = {"false", "no", "0", "failed", "mismatch", "unmatched", "notmatch", "notmatched"}
_TRUE_VALUES = {"true", "yes", "1", "failed", "mismatch", "unmatched", "notmatched"}


def name_tokens(value):
    """Return case- and punctuation-insensitive name tokens without honorifics."""
    if value is None:
        return ()
    text = unicodedata.normalize("NFKD", str(value)).casefold()
    tokens = []
    current = []
    for character in text:
        if unicodedata.combining(character):
            continue
        if character.isalnum():
            current.append(character)
        elif current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))
    return tuple(token for token in tokens if token not in _HONORIFICS)


def extract_provider_identity_name(result, method):
    """Extract only the name components used for comparison from a provider result."""
    data = result.get("data") if isinstance(result, dict) else None
    if not isinstance(data, dict):
        return None

    method = str(method or "").upper()
    if method == "NIN":
        first = data.get("firstname") or data.get("first_name") or data.get("given_name")
        middle = data.get("middlename") or data.get("middle_name") or ""
        last = data.get("surname") or data.get("last_name") or data.get("lastname")
    elif method == "BVN":
        first = data.get("first_name") or data.get("firstname") or data.get("given_name")
        middle = data.get("middle_name") or data.get("middlename") or ""
        last = data.get("last_name") or data.get("surname") or data.get("lastname")
    else:
        return None

    if not isinstance(first, str) or not isinstance(last, str):
        return None
    first = first.strip()
    middle = middle.strip() if isinstance(middle, str) else ""
    last = last.strip()
    if not name_tokens(first) or not name_tokens(last):
        return None
    return {"first_name": first, "middle_name": middle, "last_name": last}


def profile_name_matches_identity(identity_name, profile_first_name, profile_last_name):
    """Compare profile first/last names with provider given/middle/surname fields."""
    if not isinstance(identity_name, dict):
        return False
    profile_first = set(name_tokens(profile_first_name))
    profile_last = set(name_tokens(profile_last_name))
    identity_given = set(
        name_tokens(identity_name.get("first_name"))
        + name_tokens(identity_name.get("middle_name"))
    )
    identity_last = set(name_tokens(identity_name.get("last_name")))
    return bool(profile_first and profile_last and identity_given and identity_last) and (
        profile_first.issubset(identity_given)
        and profile_last == identity_last
    )


def account_name_matches_identity(account_name, identity_name, profile_first_name, profile_last_name):
    """Accept bank name order variations and optional middle names, but not a different name."""
    if not profile_name_matches_identity(identity_name, profile_first_name, profile_last_name):
        return False
    candidate = set(name_tokens(account_name))
    identity_first = set(name_tokens(identity_name.get("first_name")))
    identity_middle = set(name_tokens(identity_name.get("middle_name")))
    identity_last = set(name_tokens(identity_name.get("last_name")))
    profile_tokens = set(name_tokens(profile_first_name) + name_tokens(profile_last_name))
    required = identity_first | identity_last | profile_tokens
    allowed = identity_first | identity_middle | identity_last | profile_tokens
    return bool(candidate) and required.issubset(candidate) and candidate.issubset(allowed)


def provider_result_is_pending(result):
    """Return true for accepted/processing responses, even if the wrapper says successful."""
    if not isinstance(result, dict):
        return False
    try:
        if int(result.get("_http_status") or 0) == 202:
            return True
    except (TypeError, ValueError):
        pass
    pending_values = {"pending", "processing", "queued", "initiated", "in_progress", "in progress"}
    data = result.get("data")
    values = [result.get("status")]
    if isinstance(data, dict):
        values.extend(data.get(key) for key in ("status", "verification_status", "lookup_status"))
    return any(str(value or "").strip().casefold() in pending_values for value in values)


def provider_reports_name_mismatch(result):
    """Recognize an explicit provider name/identity mismatch without exposing its payload."""
    if not isinstance(result, dict):
        return False

    stack = [result]
    while stack:
        item = stack.pop()
        if not isinstance(item, dict):
            continue
        for key, value in item.items():
            normalized_key = re.sub(r"[^a-z]", "", str(key).casefold())
            normalized_value = re.sub(r"[^a-z0-9]", "", str(value).casefold()) if value is not None else ""
            if normalized_key in _NAME_MATCH_FIELDS and normalized_value in _FALSE_VALUES:
                return True
            if normalized_key in _NAME_MISMATCH_FIELDS and normalized_value in _TRUE_VALUES:
                return True
            if isinstance(value, dict):
                stack.append(value)
            elif isinstance(value, list):
                stack.extend(child for child in value if isinstance(child, dict))

    messages = [result.get("message")]
    data = result.get("data")
    if isinstance(data, dict):
        messages.append(data.get("message"))
    message = " ".join(value for value in messages if isinstance(value, str)).casefold()
    if any(phrase in message for phrase in ("no mismatch", "without a mismatch", "not mismatched")):
        return False
    return any(
        phrase in message
        for phrase in (
            "mismatch",
            "does not match",
            "doesn't match",
            "did not match",
            "do not match",
            "not matching",
            "not matched",
        )
    )
