"""Diagnose email delivery from this machine, stage by stage.

A verification code that never arrives is one of the most confusing failures in
the product: the account is created, the code is stored, and the person waits
for a message that is stuck behind a firewall, a blocked port or a rejected
password. Nothing in the app says why.

Run this and it walks the whole route -- configuration, DNS, TCP, SMTP
handshake, authentication, delivery -- saying exactly where it breaks:

    python manage.py check_email
    python manage.py check_email --to someone@example.com

Run it from the same runtime that sends the mail (the API host and the Celery
worker, or the worker's shell/logs): a pass from a laptop proves nothing about
the machine the worker runs on.

Secrets are never printed: the password and any token stay out of the output.
"""

import smtplib
import socket
import ssl

from django.conf import settings
from django.core.management.base import BaseCommand

SMTP_STEP_TIMEOUT = 10


def _redact_broker(url):
    """Show scheme://host:port/path for the broker, never userinfo."""
    url = str(url or "").strip()
    if not url:
        return "(not configured)"
    try:
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
        path = parts.path or ""
        if not host:
            return parts.scheme + "://" + path
        return f"{parts.scheme}://{host}{port}{path}"
    except Exception:
        return "(configured; could not be displayed safely)"


class Command(BaseCommand):
    help = "Check that this machine can reach and authenticate against the configured mail server."

    def add_arguments(self, parser):
        parser.add_argument(
            "--to",
            dest="to",
            default="",
            help="Send one real test message to this address after the connection check.",
        )

    def _line(self, label, value):
        self.stdout.write(f"  {label:<12}{value}")

    def handle(self, *args, **options):
        import os

        backend = str(getattr(settings, "EMAIL_BACKEND", "") or "")
        host = str(getattr(settings, "EMAIL_HOST", "") or "").strip()
        try:
            port = int(getattr(settings, "EMAIL_PORT", 0) or 0)
        except (TypeError, ValueError):
            port = 0
        user = str(getattr(settings, "EMAIL_HOST_USER", "") or "").strip()
        password = str(getattr(settings, "EMAIL_HOST_PASSWORD", "") or "")
        use_ssl = bool(getattr(settings, "EMAIL_USE_SSL", False))
        use_tls = bool(getattr(settings, "EMAIL_USE_TLS", False))
        timeout = float(getattr(settings, "EMAIL_TIMEOUT", SMTP_STEP_TIMEOUT) or SMTP_STEP_TIMEOUT)
        broker_url = str(getattr(settings, "CELERY_BROKER_URL", "") or getattr(settings, "REDIS_URL", "") or "")
        settings_module = os.environ.get("DJANGO_SETTINGS_MODULE", "(unset)")
        routes = getattr(settings, "CELERY_TASK_ROUTES", {}) or {}
        email_route = routes.get("home.tasks.send_verification_email", {}).get("queue", "(default queue)")

        # Stage 1 -- configuration, with secrets redacted.
        self.stdout.write(self.style.MIGRATE_HEADING("Stage 1: Configuration"))
        self._line("backend", backend)
        self._line("host", f"{host}:{port}")
        self._line("security", "SSL" if use_ssl else ("STARTTLS" if use_tls else "none (plaintext)"))
        self._line("timeout", f"{timeout:g}s")
        self._line("user", user or "(none configured)")
        self._line("password", "set" if password else "(none configured)")
        self._line("from", str(getattr(settings, "DEFAULT_FROM_EMAIL", "") or ""))
        self._line("settings", settings_module)
        self._line("broker", _redact_broker(broker_url))
        self._line("email queue", email_route)

        if "smtp" not in backend:
            self.stdout.write(
                self.style.SUCCESS(
                    "\nThis backend does not use the network, so nothing can time out.\n"
                    "Messages go to the console or to a file -- read them there.\n"
                    "Use this only as a deliberate local diagnostic, never as production delivery."
                )
            )
            return

        problems = []
        if not host or port <= 0:
            self.stdout.write(self.style.ERROR("\nNo EMAIL_HOST/EMAIL_PORT configured."))
            return
        if not user or not password:
            problems.append(
                "EMAIL_HOST_USER or EMAIL_HOST_PASSWORD is empty. "
                "For Gmail use a 16-character App Password, not the account password."
            )
        if use_ssl and use_tls:
            problems.append("EMAIL_USE_SSL and EMAIL_USE_TLS are both enabled; enable exactly one.")
        if use_ssl and port == 587:
            problems.append(
                "Port 587 needs STARTTLS, not SSL. Set EMAIL_USE_SSL=False and EMAIL_USE_TLS=True."
            )
        if use_tls and port == 465:
            problems.append(
                "Port 465 is implicit SSL. Set EMAIL_USE_SSL=True and EMAIL_USE_TLS=False."
            )

        # Stage 2 -- DNS resolution, kept strictly separate from connectivity.
        self.stdout.write(self.style.MIGRATE_HEADING("\nStage 2: DNS resolution"))
        try:
            infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
            addresses = sorted({info[4][0] for info in infos})
            preview = ", ".join(addresses[:4]) + (" ..." if len(addresses) > 4 else "")
            self.stdout.write(self.style.SUCCESS(f"   OK  {host} resolves to {preview}"))
        except socket.gaierror as exc:
            self.stdout.write(self.style.ERROR(f"   FAILED  DNS lookup for {host} failed: {exc}"))
            problems.append(
                f"The hostname {host} did not resolve. Check EMAIL_HOST for typos, the DNS "
                "servers on this machine, and whether a VPN/proxy intercepts DNS. "
                "This is a naming problem, not a port problem -- switching ports cannot help."
            )
            self._report(problems)
            return
        except OSError as exc:
            self.stdout.write(self.style.ERROR(f"   FAILED  {type(exc).__name__}: {exc}"))
            problems.append(
                f"The DNS query for {host} ended with {type(exc).__name__}. Check the local "
                "resolver, firewall rules for UDP/TCP port 53, and any VPN/proxy."
            )
            self._report(problems)
            return

        # Stage 3 -- raw TCP connectivity, with the failure type named.
        self.stdout.write(self.style.MIGRATE_HEADING(f"\nStage 3: TCP connection to {host}:{port}"))
        try:
            with socket.create_connection((host, port), timeout=timeout):
                pass
            self.stdout.write(self.style.SUCCESS(f"   OK  {host}:{port} answered."))
        except (socket.timeout, TimeoutError):
            self.stdout.write(
                self.style.ERROR(
                    f"   FAILED  no answer from {host}:{port} within {timeout:g}s (TimeoutError)."
                )
            )
            problems.append(self._tcp_timeout_guidance(host, port, use_ssl, use_tls))
            self._report(problems)
            return
        except ConnectionRefusedError:
            self.stdout.write(
                self.style.ERROR(
                    f"   FAILED  {host}:{port} refused the connection (ConnectionRefusedError)."
                )
            )
            problems.append(self._tcp_refused_guidance(host, port, use_ssl, use_tls))
            self._report(problems)
            return
        except OSError as exc:
            self.stdout.write(self.style.ERROR(f"   FAILED  {type(exc).__name__}: {exc}"))
            problems.append(self._tcp_unreachable_guidance(host, port, type(exc).__name__))
            self._report(problems)
            return

        # Stage 4 -- SMTP handshake (EHLO; STARTTLS when configured).
        self.stdout.write(self.style.MIGRATE_HEADING("\nStage 4: SMTP handshake"))
        server = None
        try:
            if use_ssl:
                self._line("mode", "implicit SSL (TLS negotiated on connect)")
                server = smtplib.SMTP_SSL(host, port, timeout=timeout)
            else:
                self._line("mode", "STARTTLS" if use_tls else "plain (no TLS!)")
                server = smtplib.SMTP(host, port, timeout=timeout)
            code, _response = server.ehlo()
            self._line("EHLO", f"server answered with code {code}")
            if use_tls:
                server.starttls()
                code, _response = server.ehlo()
                self._line("STARTTLS", f"TLS established; EHLO re-answered with code {code}")
            self.stdout.write(self.style.SUCCESS("   OK  the server accepted the handshake."))
        except ssl.SSLError as exc:
            self.stdout.write(self.style.ERROR(f"   FAILED  TLS/SSL error: {exc}"))
            problems.append(
                "The TLS handshake failed. For implicit SSL the port must speak TLS on connect "
                "(465, EMAIL_USE_SSL=True); for STARTTLS the server must offer STARTTLS after EHLO "
                "(587, EMAIL_USE_TLS=True). A certificate or mode mismatch ends exactly here. "
                "Check that the machine's clock is correct and that no proxy intercepts TLS."
            )
            self._report(self._quit_quietly(server, problems))
            return
        except (smtplib.SMTPException, OSError) as exc:
            self.stdout.write(self.style.ERROR(f"   FAILED  {type(exc).__name__}: {exc}"))
            problems.append(
                f"SMTP handshake failed ({type(exc).__name__}). The TCP port is open but the mail "
                "conversation did not start correctly; check the security mode for this port and "
                "whether an antivirus/proxy intercepts SMTP."
            )
            self._report(self._quit_quietly(server, problems))
            return

        # Stage 5 -- authentication, never before handshake, never mislabeled.
        try:
            if user and password:
                self.stdout.write(self.style.MIGRATE_HEADING("\nStage 5: Authentication"))
                try:
                    server.login(user, password)
                    self.stdout.write(self.style.SUCCESS("   OK  the credentials were accepted."))
                except smtplib.SMTPAuthenticationError as exc:
                    smtp_code = getattr(exc, "smtp_code", "?")
                    self.stdout.write(self.style.ERROR(f"   FAILED  {smtp_code}: the server rejected the credentials."))
                    problems.append(
                        "The mail server rejected the username or password. For Gmail, two-step "
                        "verification must be on and you must use a 16-character App Password, "
                        "not the account password. This is an authentication problem -- the "
                        "connection and handshake succeeded, so no port change can fix it."
                    )
                except smtplib.SMTPServerDisconnected as exc:
                    self.stdout.write(self.style.ERROR(f"   FAILED  {type(exc).__name__}: {exc}"))
                    problems.append(
                        "The server accepted the connection and handshake, then closed the "
                        "connection during sign-in. For Gmail that means the credentials were "
                        "refused outright (unknown user, or not an App Password). Confirm 2-Step "
                        "Verification is on and EMAIL_HOST_PASSWORD is the 16-character App "
                        "Password, never the account password. If the credentials are definitely "
                        "correct, an antivirus/proxy is likely intercepting SMTP AUTH -- test from "
                        "the worker's shell, not a developer laptop."
                    )
                except smtplib.SMTPException as exc:
                    self.stdout.write(self.style.ERROR(f"   FAILED  {type(exc).__name__}: {exc}"))
                    problems.append(f"Sign-in failed ({type(exc).__name__}).")
                except OSError as exc:
                    self.stdout.write(self.style.ERROR(f"   FAILED  {type(exc).__name__}: {exc}"))
                    problems.append(
                        f"The connection dropped during authentication ({type(exc).__name__}). "
                        "This is a network interruption, not a credential verdict."
                    )

            # Stage 6 -- one real test message through the exact active backend.
            recipient = str(options.get("to") or "").strip()
            if recipient:
                if problems:
                    self.stdout.write(
                        self.style.WARNING(
                            "\nStage 6: skipped -- fix the failures above first."
                        )
                    )
                else:
                    self.stdout.write(self.style.MIGRATE_HEADING(f"\nStage 6: Test delivery to {recipient}"))
                    from django.core.mail import send_mail

                    try:
                        sent = send_mail(
                            "Artisan Market: email delivery check",
                            "This is a test message from `python manage.py check_email`.\n"
                            "If you can read this, verification codes will be delivered.",
                            str(getattr(settings, "DEFAULT_FROM_EMAIL", "") or user),
                            [recipient],
                            fail_silently=False,
                        )
                        self.stdout.write(
                            self.style.SUCCESS(f"   OK  the server accepted {sent} message for delivery.")
                        )
                    except Exception as exc:  # noqa: BLE001 - the point is to name whatever broke
                        self.stdout.write(self.style.ERROR(f"   FAILED  {type(exc).__name__}: {exc}"))
                        problems.append(
                            f"The connection, handshake and authentication worked, but the test "
                            f"message was refused ({type(exc).__name__}). The recipient address or "
                            "the sender policy is the likely cause; read the SMTP code above."
                        )
            elif problems:
                self.stdout.write("\n   The test delivery was skipped because of the failures above.")
            else:
                self.stdout.write(
                    "\n   Add --to you@example.com to send one real test message."
                )
        finally:
            self._quit_quietly(server, problems)

        self._report(problems)

    # -- guidance, deliberately dependent on what is configured right now ------

    def _tcp_timeout_guidance(self, host, port, use_ssl, use_tls):
        flavour = "implicit SSL" if use_ssl else ("STARTTLS" if use_tls else "plain SMTP")
        lines = [
            f"Nothing answered on {host}:{port} ({flavour}) within the configured timeout. "
            "This is a network reachability failure, not a credential or code problem. ",
        ]
        if port == 465 and use_ssl:
            lines.append(
                "Port 465 with implicit SSL is the correct, valid configuration for Gmail -- "
                "do NOT simply switch ports because this timed out, and do NOT just raise "
                "EMAIL_TIMEOUT. Something between this machine and smtp.gmail.com is dropping "
                "outbound TCP: Windows Firewall, antivirus/web-shield SMTP filtering, a VPN or "
                "proxy, the ISP, or the hosting platform blocking outbound SMTP egress. "
                "Diagnose from the exact runtime that sends mail (the API host and the Celery "
                "worker), e.g. on Windows: `Test-NetConnection smtp.gmail.com -Port 465`. "
                "Port 587 with EMAIL_USE_SSL=False and EMAIL_USE_TLS=True may work if 465 is the "
                "only blocked route -- verify with Test-NetConnection -Port 587 first, then change "
                "both the port and the SSL/TLS pair together, never one without the other. "
                "If both are blocked, no SMTP port will help: use a transactional provider with an "
                "HTTPS API (a custom Django email backend such as django-anymail with Resend, "
                "Brevo or Postmark), which needs no SMTP egress at all."
            )
        elif port == 587 and use_tls:
            lines.append(
                "Port 587 with STARTTLS is the correct, valid configuration for submission -- "
                "do NOT simply switch ports because this timed out. Check Windows Firewall, "
                "antivirus/web-shield SMTP filtering, VPN/proxy, ISP filtering and hosting-platform "
                "egress rules for the runtime that sends mail, e.g. `Test-NetConnection smtp.gmail.com -Port 587`. "
                "Port 465 with EMAIL_USE_SSL=True and EMAIL_USE_TLS=False may work if 587 is the "
                "only blocked route -- verify with Test-NetConnection -Port 465 first, then change "
                "both the port and the SSL/TLS pair together. If both are blocked, use an HTTPS "
                "transactional provider (custom Django email backend, e.g. django-anymail with "
                "Resend, Brevo or Postmark) instead of more SMTP changes."
            )
        else:
            lines.append(
                "The configured port and security mode do not match a conventional pair "
                "(465/SSL or 587/STARTTLS). Fix the combination first, then re-check "
                "reachability; do not keep the mismatch and only change the port."
            )
        return "".join(lines)

    def _tcp_refused_guidance(self, host, port, use_ssl, use_tls):
        if use_ssl and port == 465:
            pair = "That is the standard implicit-SSL submission port, so a refusal is unusual: "
        elif use_tls and port == 587:
            pair = "That is the standard STARTTLS submission port, so a refusal is unusual: "
        else:
            pair = (
                f"Port {port} does not match the configured security mode "
                f"({'SSL' if use_ssl else ('STARTTLS' if use_tls else 'plain')}): "
            )
        return (
            f"{host}:{port} answered with ECONNREFUSED -- something refused the connection rather "
            "than silently dropping it (connection refused differs from a timeout: the host is "
            "reachable but nothing is listening, or a filtering device is rejecting you). "
            + pair
            + "an SMTP server on a different port, a restrictive egress filter or a provider policy "
            "is likely. Check the provider's documentation for the correct host/port/mode triple "
            "instead of switching ports at random -- the connection is not being dropped, it is "
            "being refused, and the answer is almost always the configuration of the far end or "
            "the egress policy, not this line of code."
        )

    def _tcp_unreachable_guidance(self, host, port, error_name):
        return (
            f"The network reports {error_name} for {host}:{port}. The machine has no route to the "
            "mail server (network unreachable / host unreachable / interface down): check the "
            "network connection itself, the default route/VPN, and any local firewall rules. "
            "DNS resolved, so this is strictly an egress/routing problem on this machine."
        )

    def _quit_quietly(self, server, problems):
        try:
            if server is not None:
                server.quit()
        except Exception:  # pragma: no cover - closing is best effort
            pass
        return problems

    def _report(self, problems):
        if not problems:
            self.stdout.write(
                self.style.SUCCESS("\nEmail delivery looks correctly configured.")
            )
            return
        self.stdout.write(self.style.ERROR("\nWhat to fix:"))
        for index, problem in enumerate(problems, start=1):
            self.stdout.write(f"  {index}. {problem}")
        self.stdout.write(
            "\nNotes:\n"
            "  - A timeout is not fixed by raising EMAIL_TIMEOUT or retrying more \n"
            "    aggressively; it is fixed (or excluded) by diagnosing the network egress\n"
            "    from this exact machine -- see the TCP guidance above.\n"
            "  - Port 587 with STARTTLS requires EMAIL_USE_SSL=False and EMAIL_USE_TLS=True;\n"
            "    port 465 requires EMAIL_USE_SSL=True and EMAIL_USE_TLS=False. Never enable both.\n"
            "  - Run this command from the same runtime as the Celery worker before concluding\n"
            "    anything; a different machine proves nothing about the worker's egress.\n"
            "  - If the host blocks outbound SMTP entirely, use an HTTPS transactional provider\n"
            "    (custom Django email backend, e.g. django-anymail with Resend, Brevo or Postmark).\n"
            "  - For local-only diagnosis you may set EMAIL_BACKEND=django.core.mail.backends.console.EmailBackend\n"
            "    to print codes in the API console -- never as a production delivery mechanism.\n"
        )
