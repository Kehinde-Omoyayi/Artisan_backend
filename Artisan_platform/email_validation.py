"""Import-time validation for the outbound email settings.

Kept free of Django app imports (stdlib + Django's exception only) so calling
it from ``settings.py`` can never boot the apps, and unit tests can exercise
it directly.

``production`` (and only production) refuses to boot on an invalid SMTP
configuration: a deployment that cannot mail its customers is not a working
deployment. In development the same problems are logged as warnings so a
developer sees them once, loudly, at boot.
"""

import logging

from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger("artisan.email")

IMPLICIT_SSL_PORT = 465
STARTTLS_PORT = 587


def validate_email_configuration(
    *,
    backend,
    host,
    port,
    use_ssl,
    use_tls,
    timeout,
    user,
    password,
    environment,
):
    """Validate the resolved email configuration. Returns the list of problems found.

    Raises ``ImproperlyConfigured`` in production; logs warnings elsewhere.
    """
    hard = []
    soft = []

    backend = str(backend or "")
    host = str(host or "").strip()
    user = str(user or "").strip()
    password = str(password or "").strip()
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = 0
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        timeout = 0
    is_production = str(environment).lower() == "production"
    is_smtp = backend.endswith("smtp.EmailBackend") or "backends.smtp" in backend
    console_like = any(token in backend for token in ("console.EmailBackend", "filebased.EmailBackend", "locmem.EmailBackend"))

    if use_ssl and use_tls:
        hard.append("EMAIL_USE_SSL and EMAIL_USE_TLS cannot both be enabled.")
    if is_smtp and (port <= 0 or port > 65535):
        hard.append("EMAIL_PORT must be an integer between 1 and 65535 (465 for implicit SSL, 587 for STARTTLS).")
    if is_smtp and not host:
        (hard if is_production else soft).append(
            "EMAIL_HOST is empty; no SMTP endpoint can be reached without one."
        )
    if timeout <= 0:
        hard.append("EMAIL_TIMEOUT must be greater than zero.")
    if is_production and console_like:
        hard.append(
            "Production email must not use the console/file/locmem backend; "
            "that backend silently drops or prints mail instead of delivering it."
        )
    if is_smtp and (not user or not password):
        (hard if is_production else soft).append(
            "Production SMTP email requires EMAIL_HOST_USER and EMAIL_HOST_PASSWORD. "
            "For Gmail use a 16-character App Password, not the account password."
        )
    if is_smtp:
        if use_ssl and port == STARTTLS_PORT:
            (hard if is_production else soft).append(
                "Port 587 requires STARTTLS, not implicit SSL. "
                "Set EMAIL_USE_SSL=False and EMAIL_USE_TLS=True."
            )
        elif use_tls and port == IMPLICIT_SSL_PORT:
            (hard if is_production else soft).append(
                "Port 465 requires implicit SSL, not STARTTLS. "
                "Set EMAIL_USE_SSL=True and EMAIL_USE_TLS=False."
            )
        elif use_ssl and port != IMPLICIT_SSL_PORT:
            soft.append(
                f"Implicit SSL is configured on non-standard port {port}; "
                "implicit SSL normally listens on port 465. Check the provider's documentation."
            )
        elif use_tls and port == 25:
            soft.append("STARTTLS is configured on port 25; port 587 is the submission port for STARTTLS.")
        elif not use_ssl and not use_tls:
            soft.append(
                "Neither SSL nor STARTTLS is configured; email traffic would travel as plaintext. "
                "Use port 465 with SSL or port 587 with STARTTLS."
            )

    problems = hard + soft
    if is_production and hard:
        raise ImproperlyConfigured("; ".join(hard))
    for warning in problems:
        logger.warning("email_configuration: %s", warning)
    return problems
