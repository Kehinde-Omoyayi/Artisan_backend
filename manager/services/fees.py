"""Fee and commission resolution used by the payment pipeline.

The engine reproduces the marketplace's original arithmetic exactly when the
configuration is untouched (fixed ₦150 customer fee, 10% commission), so enabling
it changes no existing price. ``payments.calculations`` calls :func:`quote`.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from core.policies import ARTISAN_COMMISSION_RATE, CUSTOMER_FEE_KOBO


@dataclass(frozen=True)
class FeePolicy:
    mode: str
    fixed_kobo: int
    percent: Decimal
    min_kobo: int
    max_kobo: int
    commission_rate: Decimal
    overrides: dict


def _value(key, override, now):
    if override and key in override:
        return override[key]
    from manager.services.config_engine import current

    return current(key, now=now)


def policy(*, now=None, override=None) -> FeePolicy:
    overrides = _value("fees.category_commission_overrides", override, now) or {}
    return FeePolicy(
        mode=_value("fees.customer_fee_mode", override, now),
        fixed_kobo=int(_value("fees.customer_fee_kobo", override, now)),
        percent=Decimal(str(_value("fees.customer_fee_percent", override, now))),
        min_kobo=int(_value("fees.min_fee_kobo", override, now)),
        max_kobo=int(_value("fees.max_fee_kobo", override, now)),
        commission_rate=Decimal(str(_value("fees.commission_rate", override, now))),
        overrides={str(k): Decimal(str(v)) for k, v in dict(overrides).items()},
    )


def customer_fee_kobo(service_amount_kobo: int, *, fee_policy: FeePolicy) -> int:
    if fee_policy.mode == "PERCENTAGE":
        fee = Decimal(service_amount_kobo) * fee_policy.percent / Decimal(100)
    elif fee_policy.mode == "COMBINED":
        fee = fee_policy.fixed_kobo + Decimal(service_amount_kobo) * fee_policy.percent / Decimal(100)
    else:
        fee = Decimal(fee_policy.fixed_kobo)
    fee = int(fee.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if fee < fee_policy.min_kobo:
        fee = fee_policy.min_kobo
    if fee_policy.max_kobo and fee > fee_policy.max_kobo:
        fee = fee_policy.max_kobo
    return max(0, fee)


def commission_rate_for(fee_policy: FeePolicy, category_slug=None) -> Decimal:
    if category_slug:
        rate = fee_policy.overrides.get(str(category_slug))
        if rate is not None:
            return rate
    return fee_policy.commission_rate


def quote(service_amount_kobo: int, *, category_slug=None, override=None, now=None) -> dict:
    """Return the same money contract ``calculate_booking_money`` returns."""
    if isinstance(service_amount_kobo, bool) or not isinstance(service_amount_kobo, int):
        raise ValueError("service_amount_kobo must be an integer.")
    if service_amount_kobo <= 0:
        raise ValueError("Service amount must be greater than zero.")
    limit = int(_value("fees.transaction_limit_kobo", override, now))
    if service_amount_kobo > limit:
        raise ValueError("Service amount exceeds the platform transaction limit.")
    fee_policy = policy(now=now, override=override)
    rate = commission_rate_for(fee_policy, category_slug)
    try:
        commission_kobo = int((Decimal(service_amount_kobo) * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except InvalidOperation:  # pragma: no cover - guarded by configuration validation
        raise ValueError("Commission configuration is invalid.")
    fee = customer_fee_kobo(service_amount_kobo, fee_policy=fee_policy)
    return {
        "service_amount_kobo": service_amount_kobo,
        "customer_fee_kobo": fee,
        "total_amount_kobo": service_amount_kobo + fee,
        "commission_rate": rate,
        "platform_commission_kobo": commission_kobo,
        "artisan_payout_kobo": service_amount_kobo - commission_kobo,
    }


DEFAULTS = {"fee": CUSTOMER_FEE_KOBO, "rate": ARTISAN_COMMISSION_RATE}
