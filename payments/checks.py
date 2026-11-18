"""Configuration mistakes that only show up later as a failed payment.

`runserver` runs the system checks on every start, so a redirect URL that still
carries the example's angle brackets — or one that points at localhost — is
reported before the first payer hits it, instead of surfacing as "Payment could
not be confirmed" in the chat with nothing to act on.
"""

from django.conf import settings
from django.core.checks import Warning, register

PLACEHOLDER = "payments.W001"
NOT_PUBLIC = "payments.W002"
NOT_SECURE = "payments.W003"


@register()
def checkout_urls(app_configs, **kwargs):
    problems = []
    if str(getattr(settings, "ENVIRONMENT", "") or "").lower() == "test":
        return problems

    for name in ("FLW_REDIRECT_URL", "SITE_URL"):
        value = str(getattr(settings, name, "") or "").strip()
        if not value:
            continue
        if "<" in value or ">" in value:
            problems.append(
                Warning(
                    f"{name} still contains the example's angle brackets: {value}",
                    hint=(
                        "Paste the real ngrok forwarding address into .env "
                        "(https://xxxx-xxxx.ngrok-free.app) and restart Django and "
                        "the Celery worker."
                    ),
                    id=PLACEHOLDER,
                )
            )
            continue
        if "localhost" in value or "127.0.0.1" in value:
            problems.append(
                Warning(
                    f"{name} points at this machine: {value}",
                    hint=(
                        "Flutterwave sends the payer's browser to this address, so it "
                        "must be the public https tunnel, not localhost."
                    ),
                    id=NOT_PUBLIC,
                )
            )
            continue
        if not value.startswith("https://"):
            problems.append(
                Warning(
                    f"{name} is not https: {value}",
                    hint="The provider will not redirect a browser back to an http address.",
                    id=NOT_SECURE,
                )
            )
    return problems
