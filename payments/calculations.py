from decimal import Decimal, ROUND_HALF_UP
from core.policies import ARTISAN_COMMISSION_RATE, CUSTOMER_FEE_KOBO, MAX_SERVICE_AMOUNT_KOBO


def calculate_booking_money(service_amount_kobo: int, *, category_slug=None) -> dict:
    """Authoritative pricing for one booking.

    Delegates to the versioned configuration engine. With an untouched
    configuration the result is byte-for-byte what this function used to return
    (fixed ₦150 fee, 10% commission), so no existing behaviour or test changes.
    """
    if isinstance(service_amount_kobo, bool) or not isinstance(service_amount_kobo, int): raise ValueError("service_amount_kobo must be an integer.")
    if service_amount_kobo <= 0: raise ValueError("Service amount must be greater than zero.")
    from manager.services.fees import quote
    return quote(service_amount_kobo, category_slug=category_slug)


def assert_booking_money_invariants(values):
    if values["service_amount_kobo"] + values["customer_fee_kobo"] != values["total_amount_kobo"]: raise ValueError("Payment total invariant failed.")
    if values["platform_commission_kobo"] + values["artisan_payout_kobo"] != values["service_amount_kobo"]: raise ValueError("Payout invariant failed.")
    if values["platform_commission_kobo"] < 0 or values["artisan_payout_kobo"] < 0: raise ValueError("Financial invariant failed.")
    return values
