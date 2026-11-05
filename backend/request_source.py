"""Resolve forwarded clients only through explicitly trusted reverse proxies."""
from functools import lru_cache
import ipaddress
import os
from .request_origin import configured_origin


def address(value):
    try:
        parsed = ipaddress.ip_address(value)
        return parsed.ipv4_mapped if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped else parsed
    except ValueError:
        return None


@lru_cache(maxsize=8)
def networks(value):
    result = []
    for entry in value.split(','):
        if not entry.strip():
            continue
        network = ipaddress.ip_network(entry.strip(), strict=False)
        if network.prefixlen == 0:
            raise ValueError('TRUSTED_PROXIES must name specific proxies, not all addresses')
        result.append(network)
    return tuple(result)


def trusted(value):
    parsed = address(value)
    return parsed is not None and any(parsed in network for network in networks(os.getenv('TRUSTED_PROXIES', '127.0.0.1,::1')))


def client_ip(request):
    peer = request.client.host if request.client else '127.0.0.1'
    # Internal background audit events can have a client but no HTTP headers.
    raw = getattr(request, 'headers', {}).get('x-forwarded-for', '')
    if not trusted(peer) or not raw or len(raw) > 1024:
        return peer
    hops = [address(item.strip()) for item in raw.split(',')]
    if len(hops) > 20 or any(hop is None for hop in hops):
        return peer
    current = peer
    for hop in reversed(hops):
        if not trusted(current):
            break
        current = str(hop)
    return current


def cookie_secure(request):
    if os.getenv('COOKIE_SECURE') == 'true' or request.url.scheme == 'https':
        return True
    origin = configured_origin(os.getenv('PUBLIC_ORIGIN', ''))
    if origin and origin[0] == 'https' and request.url.hostname == origin[1]:
        return True
    return bool(request.client and trusted(request.client.host) and request.headers.get('x-forwarded-proto') == 'https')
