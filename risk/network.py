import hashlib
import hmac
import ipaddress

from django.conf import settings
from django.db.models import F
from django.utils import timezone

from .models import CustomerNetworkBlock, CustomerNetworkObservation


def _configured_networks():
    raw = getattr(settings, "TRUSTED_PROXY_IPS", []) or []
    networks = []
    for value in raw:
        try:
            networks.append(ipaddress.ip_network(value, strict=False))
        except ValueError:
            continue
    return networks


def _is_trusted_proxy(remote_addr):
    if not remote_addr:
        return False
    try:
        remote = ipaddress.ip_address(remote_addr)
    except ValueError:
        return False
    return any(remote in network for network in _configured_networks())


def _valid_forwarded_addresses(request):
    addresses = []
    for part in request.META.get("HTTP_X_FORWARDED_FOR", "").split(","):
        part = part.strip()
        try:
            ipaddress.ip_address(part)
        except ValueError:
            continue
        addresses.append(part)
    return addresses


def get_client_ip(request):
    """Return the real client address without trusting client-supplied X-Forwarded-For entries.

    Proxies *append* to X-Forwarded-For, so the leftmost entry is attacker-controlled and is never used.
    * A peer inside ``TRUSTED_PROXY_IPS``: walk the header from the right, skipping trusted proxies; the first
      untrusted address is the client.
    * Otherwise, when ``TRUSTED_PROXY_COUNT`` > 0 (a platform proxy whose address is not fixed, e.g. Render),
      the client is that many entries from the right, exactly like DRF's ``NUM_PROXIES``.
    * Otherwise the socket peer is the client and the header is ignored.
    """
    remote_addr = request.META.get("REMOTE_ADDR", "")
    forwarded = _valid_forwarded_addresses(request)
    if forwarded and _is_trusted_proxy(remote_addr):
        for candidate in reversed(forwarded):
            if not _is_trusted_proxy(candidate):
                return candidate
        return remote_addr
    count = int(getattr(settings, "TRUSTED_PROXY_COUNT", 0) or 0)
    if forwarded and count > 0:
        return forwarded[-count] if len(forwarded) >= count else forwarded[0]
    return remote_addr


def hash_ip(ip_address):
    if not ip_address:
        return ""
    try:
        normalized = str(ipaddress.ip_address(ip_address))
    except ValueError:
        return ""
    return hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        normalized.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def record_customer_network_observation(customer, request, observation_type="ACTIVITY"):
    ip_hash = hash_ip(get_client_ip(request))
    if not ip_hash:
        return None

    observation, created = CustomerNetworkObservation.objects.get_or_create(
        customer=customer,
        ip_hash=ip_hash,
        defaults={"observation_type": observation_type},
    )
    if not created:
        CustomerNetworkObservation.objects.filter(pk=observation.pk).update(
            last_seen_at=timezone.now(),
            observation_count=F("observation_count") + 1,
            observation_type=observation_type,
        )
    return ip_hash


def is_network_blocked(ip_hash, *, scope="AUTH_BOOKING"):
    if not ip_hash:
        return False
    return CustomerNetworkBlock.objects.filter(
        ip_hash=ip_hash,
        scope=scope,
        blocked_until__gt=timezone.now(),
    ).exists()
