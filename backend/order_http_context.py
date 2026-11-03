"""Actual per-order ChatGPT cookies and request metadata, never generated signals.

The checkout still uses its existing curl transport. This object carries its real
HTTP state into the post-payment client and encrypted recovery checkpoint.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from email.parser import Parser
from http.cookiejar import Cookie
import re
import time
from urllib.parse import urlsplit

import httpx

_current = ContextVar('order_chatgpt_context', default=None)
HEADER_NAMES = frozenset({'user-agent', 'accept-language', 'oai-language',
                         'openai-client-type', 'openai-client-name'})


def current(url):
    parsed = urlsplit(url)
    return _current.get() if parsed.scheme == 'https' and parsed.netloc == 'chatgpt.com' else None


def response_headers(raw):
    """Ignore proxy CONNECT/interim headers; keep repeated final Set-Cookie."""
    blocks = re.split(r'(?m)^HTTP/\S+\s+\d{3}[^\r\n]*\r?\n', raw)
    if len(blocks) < 2:
        return httpx.Headers()
    return httpx.Headers(list(Parser().parsestr(blocks[-1]).items()))


class OrderHTTPContext:
    def __init__(self, credentials=None, snapshot=None):
        self.cookies = httpx.Cookies()
        self.headers = {}
        snapshot = snapshot or {}
        self.remember_headers(snapshot.get('headers') or {})
        for item in (snapshot.get('cookies') or [])[:100]:
            try:
                domain = item['domain']
                if domain.lstrip('.') != 'chatgpt.com':
                    continue
                expires = item.get('expires')
                if expires is not None and expires <= time.time():
                    continue
                name, value = item['name'], item['value']
                if not self._cookie_valid(name, value):
                    continue
                self.cookies.jar.set_cookie(Cookie(0, name, value, None, False, domain,
                    item.get('domain_specified', False), domain.startswith('.'),
                    item.get('path') or '/', True, item.get('secure', True), expires,
                    expires is None, None, None, {'HttpOnly': None}, False))
            except (KeyError, TypeError, ValueError):
                continue
        if not snapshot:
            credentials = credentials or {}
            values = dict(credentials.get('cookies') or {})
            if not values and credentials.get('session_token'):
                values = {name: credentials['session_token'] for name in (
                    '__Secure-next-auth.session-token', '__Secure-platform.session-token')}
            for name, value in values.items():
                if self._cookie_valid(name, value):
                    self.cookies.set(name, value, domain='chatgpt.com', path='/')

    @staticmethod
    def _cookie_valid(name, value):
        return (isinstance(name, str) and re.fullmatch(r'[!#$%&\'*+.^_`|~0-9A-Za-z-]{1,200}', name)
                and isinstance(value, str) and len(value) <= 32768
                and not any(c in value for c in '\r\n\x00'))

    def remember_headers(self, headers):
        for name, value in headers.items():
            name = name.lower()
            if (name in HEADER_NAMES and isinstance(value, str) and len(value) <= 1024
                    and not any(ord(c) < 32 for c in value)):
                self.headers[name] = value

    def prepare(self, method, url, headers):
        outgoing = httpx.Headers(headers or {})
        self.remember_headers(outgoing)
        request = httpx.Request(method, url, headers=outgoing)
        self.cookies.set_cookie_header(request)
        if 'cookie' in request.headers:
            outgoing['cookie'] = request.headers['cookie']
        return dict(outgoing)

    def absorb(self, method, url, headers):
        response = httpx.Response(200, headers=headers, request=httpx.Request(method, url))
        self.cookies.extract_cookies(response)

    def snapshot(self):
        cookies = []
        for c in self.cookies.jar:
            if c.domain.lstrip('.') != 'chatgpt.com' or c.is_expired():
                continue
            cookies.append(dict(name=c.name, value=c.value, domain=c.domain,
                domain_specified=c.domain_specified, path=c.path, secure=c.secure, expires=c.expires))
        return {'version': 1, 'headers': dict(self.headers), 'cookies': cookies[:100]}

    @contextmanager
    def bind(self):
        token = _current.set(self)
        try:
            yield self
        finally:
            _current.reset(token)
