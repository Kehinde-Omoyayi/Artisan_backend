"""Error classification for outbound email delivery.

One question decides whether retrying helps: will trying again behave
differently, or fail the exact same way? Retrying a Gmail authentication
rejection four times delivers nothing and delays the visible failure; retrying
a five-second network blip delivers the code.

Transient failures get a bounded ladder -- 5s, 15s, 30s, 60s, 60s -- that fits
inside the five-minute validity of an OTP. Permanent failures (authentication,
sender/recipient rejection, 5xx, invalid TLS, missing configuration) are never
retried. Unknown errors are deliberately treated as permanent: an error we do
not understand must not be retried blindly forever.
"""

import errno
import smtplib
import socket
import ssl

from django.core.exceptions import ImproperlyConfigured

TRANSIENT = "transient"
PERMANENT = "permanent"

# Bounded retry ladder, in seconds, one rung per retry attempt. The whole
# ladder (110s) sits comfortably inside the 5-minute lifetime of an OTP, so
# expired codes are never retried into oblivion.
EMAIL_RETRY_COUNTDOWN = (5, 15, 30, 60, 60)
EMAIL_MAX_RETRIES = len(EMAIL_RETRY_COUNTDOWN)


def retry_countdown(retry_number):
    """Countdown in seconds before retry number ``retry_number`` (1-based)."""
    try:
        index = int(retry_number) - 1
    except (TypeError, ValueError):
        index = 0
    index = min(max(index, 0), len(EMAIL_RETRY_COUNTDOWN) - 1)
    return EMAIL_RETRY_COUNTDOWN[index]


def classify_email_exception(exc):
    """Return ``TRANSIENT`` or ``PERMANENT`` for an exception raised during send."""
    # Misconfiguration fails identically on every attempt.
    if isinstance(exc, ImproperlyConfigured):
        return PERMANENT
    # Authentication and credential problems.
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return PERMANENT
    # SMTPFeature may arrive as SMTPNotSupportedError (e.g. STARTTLS on a server
    # that does not offer it): a configuration problem, not a blip.
    if isinstance(exc, smtplib.SMTPNotSupportedError):
        return PERMANENT
    # Recipient/sender rejections.
    if isinstance(exc, (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused)):
        return PERMANENT
    # The server dropped the connection without answering: temporary.
    if isinstance(exc, (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError)):
        return TRANSIENT
    # SMTP replies carry a status code: 4xx is a temporary failure, 5xx is not.
    smtp_code = getattr(exc, "smtp_code", None)
    if smtp_code is not None:
        try:
            code = int(smtp_code)
        except (TypeError, ValueError):
            return PERMANENT
        return TRANSIENT if 400 <= code < 500 else PERMANENT
    # TLS/certificate failures are configuration problems (wrong port, wrong
    # mode, expired or mismatched certificate), not blips.
    if isinstance(exc, ssl.SSLError):
        return PERMANENT
    # Network-level failures: timeouts, DNS, refused, unreachable, reset.
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return TRANSIENT
    if isinstance(exc, (socket.gaierror, ConnectionRefusedError, ConnectionResetError, ConnectionAbortedError)):
        return TRANSIENT
    if isinstance(exc, OSError):
        err = getattr(exc, "errno", None)
        if err in (
            errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN,
            errno.ECONNABORTED, errno.ECONNRESET, errno.ETIMEDOUT,
        ):
            return TRANSIENT
        # An unread network-family error still fails plausibly transiently; the
        # bounded ladder keeps that safe.
        return TRANSIENT
    # Unknown errors are not retried blindly.
    return PERMANENT


def describe_email_error(exc):
    """One safe line for logs and state: class name and SMTP code only.

    Never the exception message: SMTP responses can echo the recipient or
    server chatter, and exception chains can carry anything.
    """
    smtp_code = getattr(exc, "smtp_code", None)
    base = type(exc).__name__
    if smtp_code is not None:
        try:
            return f"{base}({int(smtp_code)})"
        except (TypeError, ValueError):
            return base
    return base
