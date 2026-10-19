from decimal import Decimal, ROUND_HALF_UP
from core.policies import ARTISAN_COMMISSION_RATE, CUSTOMER_FEE_KOBO, MAX_SERVICE_AMOUNT_KOBO


def calculate_booking_money(service_amount_kobo: int) -> dict:
    if isinstance(service_amount_kobo, bool) or not isinstance(service_amount_kobo, int): raise ValueError("service_amount_kobo must be an integer.")
    if service_amount_kobo <= 0: raise ValueError("Service amount must be greater than zero.")
    if service_amount_kobo > MAX_SERVICE_AMOUNT_KOBO: raise ValueError("Service amount exceeds the platform transaction limit.")
    commission_kobo = int((Decimal(service_amount_kobo) * ARTISAN_COMMISSION_RATE).quantize(Decimal("1"), rounding=ROUND_HALF_UP)); artisan_payout_kobo = service_amount_kobo - commission_kobo; total_amount_kobo = service_amount_kobo + CUSTOMER_FEE_KOBO
    return {"service_amount_kobo": service_amount_kobo, "customer_fee_kobo": CUSTOMER_FEE_KOBO, "total_amount_kobo": total_amount_kobo, "commission_rate": ARTISAN_COMMISSION_RATE, "platform_commission_kobo": commission_kobo, "artisan_payout_kobo": artisan_payout_kobo}


def assert_booking_money_invariants(values):
    if values["service_amount_kobo"] + values["customer_fee_kobo"] != values["total_amount_kobo"]: raise ValueError("Payment total invariant failed.")
    if values["platform_commission_kobo"] + values["artisan_payout_kobo"] != values["service_amount_kobo"]: raise ValueError("Payout invariant failed.")
    if values["platform_commission_kobo"] < 0 or values["artisan_payout_kobo"] < 0: raise ValueError("Financial invariant failed.")
    return values
