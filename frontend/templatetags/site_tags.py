from datetime import date, datetime

from django import template
from django.templatetags.static import static
from decimal import Decimal, InvalidOperation

from django.utils.html import format_html

register = template.Library()

@register.filter
def get_item(value, key):
    if isinstance(value, dict):
        return value.get(key, '')
    try:
        return getattr(value, key)
    except Exception:
        return ''

@register.filter
def pretty(value):
    if value is None:
        return '—'
    if isinstance(value, bool):
        return 'Yes' if value else 'No'
    if isinstance(value, (dict, list)):
        return 'Available'
    return value

@register.filter
def chat_time(value):
    """Wall-clock time only, e.g. "12:34 pm" — the chat never prints a date."""
    if not value:
        return ''
    from zoneinfo import ZoneInfo

    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        except (TypeError, ValueError):
            return str(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo('Africa/Lagos'))
    local = parsed.astimezone(ZoneInfo('Africa/Lagos'))
    hour = local.hour % 12 or 12
    suffix = 'am' if local.hour < 12 else 'pm'
    return f'{hour}:{local.minute:02d} {suffix}'


@register.filter
def ngn(value):
    """Format a numeric API amount as Nigerian naira with grouped thousands."""
    try:
        amount = Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError):
        amount = Decimal('0.00')
    return f'₦{amount:,.2f}'


@register.filter
def ngn_kobo(value):
    """Format an integer aggregate in kobo as Nigerian naira."""
    try:
        amount = Decimal(str(value or 0)) / Decimal('100')
    except (InvalidOperation, TypeError, ValueError):
        amount = Decimal('0.00')
    return f'₦{amount:,.2f}'


@register.filter
def manager_mask(value):
    """Render a value only in a server-authorized, explicitly revealed manager response."""
    if value is None:
        value = '—'
    return format_html('<span class="manager-sensitive-value">{}</span>', str(value))


@register.simple_tag(takes_context=True)
def private_value(context, value):
    """Mask promotion details unless the one-use authorized reveal was consumed."""
    if value is None:
        value = '—'
    display = str(value) if context.get('promotion_values_revealed') else '***'
    return format_html('<span class="manager-sensitive-value">{}</span>', display)


@register.filter
def humanize_code(value):
    """Render API enum values as readable labels without losing word boundaries."""
    text = str(value or '').strip()
    labels = {
        'UNDER_REVIEW': 'Under review',
        'PROCESSING_UNKNOWN': 'Outcome unknown',
        'PARTIAL_REFUND': 'Partial refund',
        'REFUND': 'Full refund',
        'NO_ADJUSTMENT': 'No financial adjustment',
        'CASE_NOTE_ADDED': 'Private staff note',
        'CASE_MESSAGE_SENT': 'Public case message',
        'CASE_CREATED': 'Case created',
        'CASE_ASSIGNED': 'Case assigned',
        'CASE_CLAIMED': 'Case claimed',
        'CASE_RESOLVED': 'Case resolved',
        'CASE_CLOSED': 'Case closed',
    }
    return labels.get(text.upper(), text.replace('_', ' ').title())


@register.filter
def delta_label(change):
    """Say "< 0.1%" rather than "0.0%" when a real change rounds away.

    A movement of ₦500 on ₦7m is a real movement; printing "0.0% vs previous
    period" makes it look as though nothing happened.
    """
    if change is None:
        return ''
    try:
        value = float(change)
    except (TypeError, ValueError):
        return ''
    if value == 0:
        return '0%'
    if abs(value) < 0.1:
        return '<0.1%'
    return f'{value:.1f}%'


@register.filter
def status_class(value):
    text = str(value or '').upper()
    if any(x in text for x in ('COMPLETED','SUCCEEDED','ACTIVE','VERIFIED','NORMAL','OPEN')):
        return 'ok'
    if any(x in text for x in ('CANCEL','REJECT','FAILED','LOCKED','SUSPENDED','URGENT')):
        return 'bad'
    if any(x in text for x in ('PROCESS','REVIEW','REQUESTED','ACCEPTED','IN_PROGRESS','PENDING')):
        return 'warn'
    return 'neutral'


@register.simple_tag
def asset(path):
    """Static URL plus a content stamp.

    Browsers (and the offline service worker) cache ``/static/`` aggressively.
    Appending the file's modification time means a shipped stylesheet or script
    is fetched again the moment it changes, instead of staying pinned to an old
    copy inside a long-lived cache.
    """
    import os

    from django.conf import settings

    try:
        url = static(path)
    except ValueError:
        # Manifest storage raises for files missing from the manifest; fall back to
        # the plain static path so a single missing asset cannot break the page.
        url = f"{settings.STATIC_URL.rstrip('/')}/{str(path).lstrip('/')}"
    for directory in getattr(settings, "STATICFILES_DIRS", ()) or ():
        candidate = os.path.join(str(directory), path)
        if os.path.exists(candidate):
            return format_html("{}?v={}", url, int(os.path.getmtime(candidate)))
    return url


@register.simple_tag
def page_script(path):
    """Emit the per-page script only when that file exists in the static manifest.

    Pages without a dedicated script must render normally instead of failing with a missing-manifest error.
    """
    if not path:
        return ""
    # asset() always returns a URL (it falls back to the plain static path), so
    # existence has to be checked here. Otherwise every page without a dedicated
    # script requests a 404 - invisible in the HTML, noisy in the network tab.
    import os

    from django.conf import settings

    found = any(
        os.path.exists(os.path.join(str(directory), path))
        for directory in (getattr(settings, "STATICFILES_DIRS", ()) or ())
    )
    if not found:
        # In production the files live behind a manifest storage backend, where
        # existence can only be answered by Django's finders.
        hashed = getattr(settings, "STORAGES", {}).get("staticfiles", {}).get("BACKEND", "")
        if "Manifest" in str(hashed):
            try:
                from django.contrib.staticfiles import finders

                found = finders.find(path) is not None
            except Exception:
                found = False
    if not found:
        return ""
    return format_html('<script src="{}"></script>', asset(path))

@register.simple_tag
def discovery_url(params, *drop):
    """Rebuild the discovery URL without the named keys (used by the filter tags)."""
    from urllib.parse import urlencode

    from django.urls import reverse

    values = params or {}
    query = [
        (key, value)
        for key, value in values.items()
        if value not in (None, '', False) and key not in drop
    ]
    base = reverse('artisans')
    return f"{base}?{urlencode(query)}" if query else base


@register.filter
def institution_label(value, institutions):
    """Human label for the institution currently applied to the discovery filters."""
    for item in institutions or ():
        if not isinstance(item, dict):
            continue
        if str(item.get('id') or '') != str(value or ''):
            continue
        name = str(item.get('name') or '').strip()
        campus = str(item.get('campus') or '').strip()
        if name and campus:
            return f"{name} — {campus}"
        return name or 'Selected institution'
    return 'Selected institution'


@register.filter
def human_date(value):
    """Render an ISO date (or date object) as "19 Oct 2026"; pass anything else through."""
    if not value:
        return ''
    if isinstance(value, datetime):
        return f"{value.day} {value.strftime('%b %Y')}"
    if isinstance(value, date):
        return f"{value.day} {value.strftime('%b %Y')}"
    text = str(value).strip()
    try:
        parsed = date.fromisoformat(text[:10])
    except ValueError:
        return text
    return f"{parsed.day} {parsed.strftime('%b %Y')}"


@register.filter
def human_datetime(value):
    """Render an ISO datetime as "5 Nov 2026, 9:00 am" in Africa/Lagos.

    Booking schedules come from the API as UTC ISO strings; printing them raw is
    both unreadable and ambiguous, so every schedule surface uses this filter.
    """
    if not value:
        return ''
    from zoneinfo import ZoneInfo

    parsed = value
    if not isinstance(parsed, datetime):
        text = str(value).strip().replace('Z', '+00:00')
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return str(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo('UTC'))
    local = parsed.astimezone(ZoneInfo('Africa/Lagos'))
    hour = local.strftime('%I').lstrip('0') or '12'
    return f"{local.day} {local.strftime('%b %Y')}, {hour}:{local.strftime('%M')} {local.strftime('%p').lower()}"


@register.filter
def short_system_message(value):
    """Chat shows only the short event line, never the long workflow instructions.

    System events stored by the backend append a "Next steps: ..." guidance tail.
    That guidance lives in the Service tools panel now, so the message stream
    keeps just the first line (e.g. "Offer accepted."). User text is untouched.
    """
    text = '' if value is None else str(value)
    marker = text.find('Next steps:')
    if marker != -1:
        text = text[:marker]
    return text.strip()
