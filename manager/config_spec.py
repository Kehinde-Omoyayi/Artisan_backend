"""Typed catalogue of configurable platform rules.

Every key has a type, a default, a validator and a sensitivity flag. The default
value of the financial keys equals the constants the marketplace shipped with, so
turning on the configuration engine does not reprice anything.
"""
from decimal import Decimal

from core.policies import ARTISAN_COMMISSION_RATE, CUSTOMER_FEE_KOBO, MAX_SERVICE_AMOUNT_KOBO

SENSITIVE = "sensitive"
OPERATIONAL = "operational"


class ConfigError(ValueError):
    pass


def _int(value, spec):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ConfigError("Enter a whole number.")
    if "min" in spec and number < spec["min"]:
        raise ConfigError(f"Must be at least {spec['min']}.")
    if "max" in spec and number > spec["max"]:
        raise ConfigError(f"Must be at most {spec['max']}.")
    return number


def _decimal(value, spec):
    try:
        number = Decimal(str(value))
    except Exception:
        raise ConfigError("Enter a decimal number.")
    if "min" in spec and number < Decimal(str(spec["min"])):
        raise ConfigError(f"Must be at least {spec['min']}.")
    if "max" in spec and number > Decimal(str(spec["max"])):
        raise ConfigError(f"Must be at most {spec['max']}.")
    if "places" in spec and -number.as_tuple().exponent > spec["places"]:
        raise ConfigError(f"Use at most {spec['places']} decimal places.")
    return str(number.quantize(Decimal(1).scaleb(-spec.get("places", 4))))


def _choice(value, spec):
    if value not in spec["choices"]:
        raise ConfigError(f"Choose one of: {', '.join(spec['choices'])}.")
    return value


def _bool(value, spec):
    if isinstance(value, bool):
        return value
    if str(value).lower() in {"true", "1", "yes"}:
        return True
    if str(value).lower() in {"false", "0", "no"}:
        return False
    raise ConfigError("Enter true or false.")


def _str(value, spec):
    text = str(value).strip()
    if not text and not spec.get("allow_blank"):
        raise ConfigError("This setting cannot be empty.")
    if "max_length" in spec and len(text) > spec["max_length"]:
        raise ConfigError(f"Use at most {spec['max_length']} characters.")
    return text


def _json(value, spec):
    if not isinstance(value, (dict, list)):
        raise ConfigError("Enter a structured value.")
    return value


_TYPES = {"int": _int, "decimal": _decimal, "choice": _choice, "bool": _bool, "string": _str, "json": _json}

CONFIG_KEYS = {
    # ---------------- Financial ----------------
    "fees.customer_fee_mode": {
        "label": "Customer service fee mode",
        "description": "How the fee charged to the customer on top of the agreed price is calculated.",
        "type": "choice", "choices": ["FIXED", "PERCENTAGE", "COMBINED"], "default": "FIXED",
        "category": SENSITIVE, "permission": "config.finance",
    },
    "fees.customer_fee_kobo": {
        "label": "Fixed customer fee (kobo)",
        "description": "Flat fee added to every booking when mode is FIXED or COMBINED.",
        "type": "int", "default": CUSTOMER_FEE_KOBO, "min": 0, "max": 5_000_000,
        "category": SENSITIVE, "permission": "config.finance",
    },
    "fees.customer_fee_percent": {
        "label": "Percentage customer fee",
        "description": "Share of the agreed price added as a customer fee when mode is PERCENTAGE or COMBINED.",
        "type": "decimal", "default": "0", "min": 0, "max": "50", "places": 4,
        "category": SENSITIVE, "permission": "config.finance",
    },
    "fees.min_fee_kobo": {
        "label": "Minimum customer fee (kobo)",
        "description": "Lower bound applied after calculation.", "type": "int", "default": 0,
        "min": 0, "max": 5_000_000, "category": SENSITIVE, "permission": "config.finance",
    },
    "fees.max_fee_kobo": {
        "label": "Maximum customer fee (kobo)",
        "description": "Upper bound applied after calculation. 0 disables the cap.",
        "type": "int", "default": 0, "min": 0, "max": 50_000_000,
        "category": SENSITIVE, "permission": "config.finance",
    },
    "fees.commission_rate": {
        "label": "Platform commission rate",
        "description": "Share of the agreed price retained by the platform before artisan payout.",
        "type": "decimal", "default": str(ARTISAN_COMMISSION_RATE), "min": 0, "max": "0.5", "places": 4,
        "category": SENSITIVE, "permission": "config.finance",
    },
    "fees.category_commission_overrides": {
        "label": "Category commission overrides",
        "description": "Map of service category slug to commission rate; unlisted categories use the platform rate.",
        "type": "json", "default": {}, "category": SENSITIVE, "permission": "config.finance",
    },
    "fees.transaction_limit_kobo": {
        "label": "Maximum transaction value (kobo)",
        "description": "Hard ceiling on a single booking's agreed price.",
        "type": "int", "default": MAX_SERVICE_AMOUNT_KOBO, "min": 100_000, "max": 1_000_000_000_000,
        "category": SENSITIVE, "permission": "config.finance",
    },
    # ---------------- Settlement ----------------
    "payouts.schedule": {
        "label": "Payout schedule",
        "description": "When held escrow is released to artisans after completion.",
        "type": "choice", "choices": ["ON_DEMAND", "DAILY", "WEEKLY"], "default": "ON_DEMAND",
        "category": SENSITIVE, "permission": "config.finance",
    },
    "payouts.release_delay_hours": {
        "label": "Release delay (hours)",
        "description": "Cooling-off period after customer confirmation before money can be released.",
        "type": "int", "default": 0, "min": 0, "max": 720,
        "category": SENSITIVE, "permission": "config.finance",
    },
    "payouts.withdrawal_min_kobo": {
        "label": "Minimum withdrawal (kobo)",
        "description": "Smallest payout an artisan can request.", "type": "int", "default": 100_00,
        "min": 0, "max": 50_000_000, "category": SENSITIVE, "permission": "config.finance",
    },
    "refunds.window_days": {
        "label": "Refund window (days)",
        "description": "How long after completion a refund can still be requested.",
        "type": "int", "default": 14, "min": 0, "max": 365,
        "category": SENSITIVE, "permission": "config.finance",
    },
    "refunds.cancellation_fee_percent": {
        "label": "Late cancellation fee",
        "description": "Share of the agreed price retained when a customer cancels inside the cancellation window.",
        "type": "decimal", "default": "0", "min": 0, "max": "100", "places": 2,
        "category": SENSITIVE, "permission": "config.finance",
    },
    # ---------------- Booking rules ----------------
    "bookings.offer_expiry_hours": {
        "label": "Offer validity (hours)",
        "description": "How long an artisan's quote stays open for the customer.",
        "type": "int", "default": 72, "min": 1, "max": 720, "category": OPERATIONAL,
        "permission": "config.operational",
    },
    "bookings.max_offer_revisions": {
        "label": "Maximum offer revisions",
        "description": "How many times one agreement can be revised before support is needed.",
        "type": "int", "default": 10, "min": 1, "max": 50, "category": OPERATIONAL,
        "permission": "config.operational",
    },
    "bookings.cancellation_window_hours": {
        "label": "Cancellation window (hours)",
        "description": "Free-cancellation window measured from the scheduled start.",
        "type": "int", "default": 24, "min": 0, "max": 168, "category": OPERATIONAL,
        "permission": "config.operational",
    },
    "bookings.attachment_max_bytes": {
        "label": "Maximum attachment size (bytes)",
        "description": "Largest audio, video or image a participant may send in a conversation.",
        "type": "int", "default": 30 * 1024 * 1024, "min": 1024, "max": 100 * 1024 * 1024,
        "category": OPERATIONAL, "permission": "config.operational",
    },
    # ---------------- Notifications ----------------
    "notifications.email_enabled": {
        "label": "Email notifications", "description": "Send transactional and campaign email.",
        "type": "bool", "default": True, "category": OPERATIONAL, "permission": "config.operational",
    },
    "notifications.push_enabled": {
        "label": "Push notifications", "description": "Deliver push notifications to installed apps.",
        "type": "bool", "default": True, "category": OPERATIONAL, "permission": "config.operational",
    },
    "notifications.sms_enabled": {
        "label": "SMS notifications", "description": "Allow SMS where a provider is configured.",
        "type": "bool", "default": False, "category": OPERATIONAL, "permission": "config.operational",
    },
    # ---------------- Platform ----------------
    "platform.maintenance_mode": {
        "label": "Maintenance mode", "description": "Show the maintenance page instead of the marketplace.",
        "type": "bool", "default": False, "category": SENSITIVE, "permission": "config.operational",
    },
    "platform.registration_open": {
        "label": "Registration open", "description": "Allow new customers and artisans to sign up.",
        "type": "bool", "default": True, "category": OPERATIONAL, "permission": "config.operational",
    },
    "platform.default_currency": {
        "label": "Default currency", "description": "Currency used for display and settlement.",
        "type": "choice", "choices": ["NGN"], "default": "NGN", "category": SENSITIVE,
        "permission": "config.finance",
    },
    "platform.payment_methods": {
        "label": "Payment methods", "description": "Methods offered at checkout: card or bank transfer only.",
        "type": "json", "default": ["CARD", "BANK_TRANSFER"], "category": SENSITIVE,
        "permission": "config.finance",
    },
    # ---------------- Analytics ----------------
    "analytics.snapshot_interval_seconds": {
        "label": "Operational snapshot interval (seconds)",
        "description": "Cadence of the point-in-time operational snapshot engine.",
        "type": "int", "default": 300, "min": 60, "max": 86400,
        "category": OPERATIONAL, "permission": "config.snapshot_interval",
    },
    "analytics.max_dashboard_range_days": {
        "label": "Maximum dashboard range (days)",
        "description": "Bounded query range for analytical endpoints.",
        "type": "int", "default": 366, "min": 30, "max": 1100,
        "category": OPERATIONAL, "permission": "config.snapshot_interval",
    },
}

SNAPSHOT_INTERVALS = (60, 120, 300, 600, 900, 1800, 3600, 21600, 43200, 86400)


def spec_for(key):
    if key not in CONFIG_KEYS:
        raise ConfigError(f"Unknown configuration key: {key}")
    return CONFIG_KEYS[key]


def normalise(key, value):
    spec = spec_for(key)
    coerced = _TYPES[spec["type"]](value, spec)
    if key == "fees.category_commission_overrides":
        if not isinstance(coerced, dict):
            raise ConfigError("Enter a mapping of category to rate.")
        from decimal import InvalidOperation

        cleaned = {}
        for slug, rate in coerced.items():
            try:
                numeric = Decimal(str(rate))
            except (InvalidOperation, TypeError, ValueError):
                raise ConfigError(f"Rate for '{slug}' is not a number.")
            if numeric < 0 or numeric > Decimal("0.5"):
                raise ConfigError(f"Rate for '{slug}' must be between 0 and 0.5.")
            cleaned[str(slug)[:80]] = str(numeric.quantize(Decimal("0.0001")))
        return cleaned
    if key == "platform.payment_methods":
        allowed = {"CARD", "BANK_TRANSFER"}
        chosen = [str(item) for item in coerced if str(item) in allowed]
        if not chosen:
            raise ConfigError("Keep at least one payment method.")
        return sorted(set(chosen))
    return coerced
