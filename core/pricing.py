"""Central minimum individual price validation.

One rule, one place: every actual individual charge an artisan enters or
submits anywhere on Artisan Market must be at least ₦100.00.

This module is the single reusable mechanism behind the rule. It is used by
the offer/quotation serializers, the add-on order serializers, and the
service layer that re-derives and persists every pricing payload. Do not
re-implement the check elsewhere — import it.

Scope notes (deliberate):

* The rule is a minimum *individual charge* rule, never a minimum booking
  total, payout, escrow or commission rule. Nothing in payments/ tasks,
  EscrowRelease or payout calculations reads this module.
* A charge that is legitimately not applied (for example transportation when
  no transport is charged, or an unused extra charge) may remain zero, null
  or omitted. ``allow_zero=True`` models exactly that "not applied" state.
* Money is handled with ``Decimal`` quantized to two places, consistent with
  the rest of the codebase. No floats, no kobo/naira confusion: the constant
  below is Decimal naira (₦100.00 == 10,000 kobo).
"""

from decimal import Decimal, InvalidOperation

from rest_framework.exceptions import ValidationError

MIN_INDIVIDUAL_PRICE = Decimal("100.00")
MIN_INDIVIDUAL_PRICE_KOBO = 10_000  # ₦100.00 — informational only; compare in naira.
MIN_INDIVIDUAL_PRICE_MESSAGE = (
    "The minimum permitted price for an item or service is ₦100.00."
)


def coerce_money(value, field):
    """Parse ``value`` as a finite, two-decimal Decimal or raise a field error."""
    if value is None:
        raise ValidationError({field: "Enter a monetary amount."})
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError, AttributeError) as exc:
        raise ValidationError({field: "Enter a valid monetary amount."}) from exc
    if not parsed.is_finite():
        raise ValidationError({field: "Enter a finite monetary amount."})
    return parsed.quantize(Decimal("0.01"))


def validate_individual_price(value, field, *, allow_zero=False):
    """Return ``value`` as a Decimal if it satisfies the individual minimum.

    Raises ``rest_framework.exceptions.ValidationError`` keyed on ``field``
    with the platform-standard message when the amount is below ₦100.00.
    When ``allow_zero`` is true, exactly zero is accepted as "not charged";
    anything else must still be at least ₦100.00.
    """
    amount = coerce_money(value, field)
    if allow_zero and amount == Decimal("0.00"):
        return amount
    if amount < MIN_INDIVIDUAL_PRICE:
        raise ValidationError({field: MIN_INDIVIDUAL_PRICE_MESSAGE})
    return amount


def minimum_price_validator(field_label=None, *, allow_zero=False, message=None):
    """Build a DRF field validator enforcing the individual minimum.

    ``field_label`` is the key used in the error dict; defaults to the
    validated field name by DRF when used with validators=[] on a serializer.
    """

    def validate(value):
        key = field_label or "amount"
        amount = coerce_money(value, key)
        if allow_zero and amount == Decimal("0.00"):
            return value
        if amount < MIN_INDIVIDUAL_PRICE:
            raise ValidationError({key: message or MIN_INDIVIDUAL_PRICE_MESSAGE})
        return value

    validate.__name__ = "minimum_price_validator"
    return validate
