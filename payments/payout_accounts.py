"""Flutterwave-backed bank directory and response normalization for artisan payouts."""
from django.core.cache import cache

from .flutterwave import FlutterwaveClient, FlutterwaveError


BANK_CACHE_KEY = "payments:flutterwave:ng:banks:v1"
BANK_CACHE_TTL_SECONDS = 12 * 60 * 60


def get_supported_nigerian_banks(*, client=None, force_refresh=False):
    """Return only bank records supplied by the configured Flutterwave account."""
    if not force_refresh:
        cached = cache.get(BANK_CACHE_KEY)
        if isinstance(cached, list):
            return cached

    provider = client or FlutterwaveClient()
    result = provider.list_banks(country="NG")
    raw_banks = result.get("data") if isinstance(result, dict) else None
    if not isinstance(raw_banks, list):
        raise FlutterwaveError("Flutterwave returned an invalid bank list.")

    banks = []
    seen_codes = set()
    for item in raw_banks:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()
        name = str(item.get("name") or "").strip()
        provider_type = str(item.get("provider_type") or item.get("type") or "").strip().casefold()
        if not code or not code.isascii() or not code.isdigit() or not name:
            continue
        if any(term in provider_type for term in ("mobile", "wallet", "momo", "telco")):
            continue
        if code in seen_codes:
            continue
        banks.append({"code": code, "name": name})
        seen_codes.add(code)

    banks.sort(key=lambda bank: bank["name"].casefold())
    if not banks:
        raise FlutterwaveError("Flutterwave returned no supported Nigerian banks.")
    cache.set(BANK_CACHE_KEY, banks, BANK_CACHE_TTL_SECONDS)
    return banks


def get_supported_bank(bank_code, banks):
    code = str(bank_code or "").strip()
    return next((bank for bank in banks if bank.get("code") == code), None)


def resolved_account_details(result, expected_account_number):
    """Extract a provider-resolved account name only when the returned number matches."""
    data = result.get("data") if isinstance(result, dict) else None
    if not isinstance(data, dict):
        return None
    resolved_number = str(data.get("account_number") or "").strip()
    resolved_name = data.get("account_name")
    if (
        not resolved_number.isascii()
        or not resolved_number.isdigit()
        or resolved_number != str(expected_account_number)
        or not isinstance(resolved_name, str)
        or not resolved_name.strip()
    ):
        return None
    return {"account_number": resolved_number, "account_name": resolved_name.strip()}
