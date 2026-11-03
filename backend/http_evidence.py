"""Bounded, redacted HTTP failure evidence. Never persist raw bodies or headers."""
import base64
import html
from html.parser import HTMLParser
import json
import re
from urllib.parse import quote, quote_plus
from xml.etree import ElementTree

BODY_LIMIT = 65536
TEXT_LIMIT = 500
TEXT_FIELDS = frozenset({'response_message', 'response_detail', 'response_title', 'response_summary'})
CODE_FIELDS = frozenset({'response_error_code', 'response_error_type'})
REQUEST_ID = re.compile(r'(?:req_[A-Za-z0-9]{6,64}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})\Z')
RAY_ID = re.compile(r'[a-fA-F0-9]{16,32}-[A-Za-z0-9]{3,8}\Z')


def safe_text(value, secrets=()):
    if not isinstance(value, str):
        return ''
    # Redact before truncating, including echoed/encoded request credentials.
    variants = set()
    for secret in secrets:
        if isinstance(secret, str) and secret:
            variants.update((secret, quote(secret, safe=''), quote_plus(secret), html.escape(secret),
                             json.dumps(secret, ensure_ascii=False)[1:-1],
                             base64.b64encode(secret.encode()).decode()))
    for secret in sorted(variants, key=len, reverse=True):
        value = value.replace(secret, '[redacted]')
    value = re.sub(r'(?i)\b(?:https?|socks5h?)://[^\s<>"\']+', '[url]', value)
    value = re.sub(r'(?i)\bBearer\s+\S+', 'Bearer [redacted]', value)
    value = re.sub(r'(?i)(?:authorization|proxy-authorization|cookie|set-cookie|access_?token|session_?token|password|client_secret|cvv|cvc|card_number)\s*[=:]\s*(?:"[^"]*"|\'[^\']*\'|[^\s,;}]+)', '[credential]', value)
    value = re.sub(r'[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', '[email]', value)
    value = re.sub(r'\beyJ[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+){1,2}', '[token]', value)
    value = re.sub(r'(?i)\b(?:sk|pk|cs|pi|seti|pm)_(?:live_|test_)?[A-Za-z0-9_]+', '[identifier]', value)
    value = re.sub(r'\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b', '[identifier]', value)
    value = re.sub(r'(?<!\d)(?:\d[ -]?){12,19}(?!\d)', '[number]', value)
    value = re.sub(r'\b[A-Za-z0-9_+/=-]{32,}\b', '[identifier]', value)
    return ' '.join(value.split())[:TEXT_LIMIT]


class _HTMLSummary(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.in_title = False
        self.title, self.visible = [], []

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style', 'noscript'}: self.hidden += 1
        if tag == 'title': self.in_title = True

    def handle_endtag(self, tag):
        if tag in {'script', 'style', 'noscript'}: self.hidden = max(0, self.hidden - 1)
        if tag == 'title': self.in_title = False

    def handle_data(self, data):
        if not self.hidden:
            (self.title if self.in_title else self.visible).append(data)


def failure_evidence(status, headers, body, secrets=()):
    if status < 300:
        return {}
    h = {str(k).lower(): str(v) for k, v in headers.items()}
    result = {}
    media = h.get('content-type', '').split(';', 1)[0].strip().lower()
    if re.fullmatch(r'[a-z0-9.+-]{1,50}/[a-z0-9.+-]{1,50}', media):
        result['response_content_type'] = media
    if RAY_ID.fullmatch(h.get('cf-ray', '')): result['cf_ray'] = h['cf-ray']
    for name in ('request-id', 'x-request-id'):
        if REQUEST_ID.fullmatch(h.get(name, '')):
            result['response_request_id'] = h[name]
            break
    if h.get('cf-mitigated', '').lower() == 'challenge': result['challenge_required'] = True
    raw = body.encode('utf-8') if isinstance(body, str) else body or b''
    if len(raw) > BODY_LIMIT:
        result.update(response_truncated=True, response_summary='Response body omitted: size limit')
        return result
    text = raw.decode('utf-8', errors='replace')
    def put(key, value):
        value = safe_text(value, secrets)
        if value and (key not in CODE_FIELDS or re.fullmatch(r'[A-Za-z0-9_.:-]{1,80}', value)):
            result[key] = value
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        data = None
    if isinstance(data, dict):
        for node in (data, data.get('error'), data.get('detail')):
            if isinstance(node, dict):
                for key in ('code', 'type', 'message', 'detail'):
                    put('response_error_' + key if key in ('code', 'type') else 'response_' + key, node.get(key))
    elif media == 'text/html' or text.lstrip().lower().startswith(('<!doctype html', '<html')):
        parser = _HTMLSummary()
        try:
            parser.feed(text)
            put('response_title', ' '.join(parser.title))
            put('response_summary', ' '.join(parser.visible))
        except (ValueError, AssertionError):
            result['response_summary'] = 'HTML response could not be summarized'
    elif media == 'text/plain':
        put('response_summary', text)
    elif media in {'application/xml', 'text/xml'} and not re.search(r'<!\s*(?:DOCTYPE|ENTITY)', text, re.I):
        try:
            root = ElementTree.fromstring(text)
            put('response_error_code', root.findtext('Code'))
            put('response_message', root.findtext('Message'))
        except ElementTree.ParseError:
            pass
    return result


def filter_field(key, value):
    if key in TEXT_FIELDS:
        return safe_text(value)
    if key in CODE_FIELDS and isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,80}', value):
        return value
    if key == 'response_content_type' and isinstance(value, str) and re.fullmatch(r'[a-z0-9.+-]{1,50}/[a-z0-9.+-]{1,50}', value):
        return value
    if key == 'cf_ray' and isinstance(value, str) and RAY_ID.fullmatch(value): return value
    if key == 'response_request_id' and isinstance(value, str) and REQUEST_ID.fullmatch(value): return value
    if key in {'response_truncated', 'challenge_required', 'retrying'} and type(value) is bool: return value
    if key in {'retry_wait_ms', 'retry_wait_elapsed_ms'} and type(value) is int and 0 <= value <= 60000: return value
    return None
