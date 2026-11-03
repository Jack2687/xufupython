"""Same-origin validation with an explicit deployment origin for TLS proxies."""
from urllib.parse import urlsplit


def parse_origin(value):
    try:
        url = urlsplit(value)
        if (url.scheme not in ('http', 'https') or not url.hostname or url.username is not None
                or url.password is not None or url.path not in ('', '/') or url.query or url.fragment
                or any(c.isspace() for c in value) or '\\' in value):
            return None
        return url.scheme, url.hostname.lower(), url.port or (443 if url.scheme == 'https' else 80)
    except (ValueError, TypeError):
        return None


def configured_origin(value):
    if not value:
        return None
    origin = parse_origin(value)
    if origin is None:
        raise ValueError('PUBLIC_ORIGIN must be an http(s) origin without credentials, path, query or fragment')
    return origin


def origin_allowed(request, public_origin=None):
    origin = request.headers.get('origin')
    if origin is None:
        return True  # Preserve server/API clients without a browser Origin.
    actual = parse_origin(origin)
    transport = parse_origin(f'{request.url.scheme}://{request.url.netloc}')
    if actual is None or transport is None:
        return False
    # Match the configured public authority as well as the Origin. Forwarded
    # headers are intentionally not trusted, including on direct public ports.
    if public_origin and transport[1] == public_origin[1]:
        request_port = request.url.port
        public_port = public_origin[2]
        if request_port is None or request_port == public_port:
            return actual == public_origin
    return actual == transport
