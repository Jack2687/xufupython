"""cs-live post-charge follow-up only. Never creates a checkout or submits payment."""
from __future__ import annotations

from datetime import datetime
import json
import logging
import re
import time
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

import httpx

from . import db, payment_trace as trace
from .common import decrypt, encrypt, now
from .proxies import proxy_client
from .renewals import COOKIE_NAME
from .order_http_context import OrderHTTPContext
from .http_evidence import BODY_LIMIT

log = logging.getLogger('xufu.post_payment')
BASE = 'https://chatgpt.com'
SUBSCRIPTIONS = '/backend-api/subscriptions'
PLAN_TYPES = {
    'go': {'go', 'go_plan', 'chatgptgo', 'chatgptgoplan'},
    'plus': {'basic', 'plus', 'basic_plan', 'chatgptplusplan'},
    'pro5x': {'standard', 'prolite', 'standard_plan', 'pro5', 'pro5x', 'chatgptprolite'},
    'pro20x': {'premium', 'pro', 'premium_plan', 'pro20', 'pro20x', 'chatgptpro'},
    'promax': {'promax', 'promax_plan', 'pro_max', 'pro_max_plan', 'chatgptpromax'},
    'team': {'team', 'team_plan', 'chatgptteam', 'chatgptteamplan', 'default'},
    'team5x': {'team5x', 'team_5x', 'prolite', 'chatgptprolite', 'chatgptteamplan', 'team'},
}
HOSTED_TYPES = dict(go='go', plus='basic', pro5x='standard', pro20x='premium',
                    promax='promax', team='team', team5x='team')
RETRY_CANCEL = {'network_error', 'http_408', 'http_429', 'http_500',
                'http_502', 'http_503', 'http_504'}
RETRY_GET_STATUS = {408, 429, 500, 502, 503, 504}


class FollowupError(Exception):
    """Fixed protocol code only; never includes upstream bodies or credentials."""
    def __init__(self, code):
        super().__init__(code)
        self.post_payment_code = code


def canonical_plan(plan):
    plan = {'pro': 'pro20x', 'team_premium': 'team5x'}.get(plan, plan)
    if plan not in PLAN_TYPES:
        raise FollowupError('unsupported_plan')
    return plan


def subscription_active(data, plan):
    return (str(data.get('plan_type', '')).lower() in PLAN_TYPES[canonical_plan(plan)]
            and (any(data.get(k) for k in ('active_until', 'current_period_end', 'expires_at', 'billing_period_end'))
                 or data.get('will_renew') is True))


def renewal_state(data):
    states = set()
    children = data.get('subscriptions') or data.get('items') or []
    for item in [data] + (children if isinstance(children, list) else []):
        if not isinstance(item, dict):
            continue
        if (item.get('will_renew') is False or item.get('cancel_at_period_end') is True
                or item.get('cancels_at') or item.get('cancel_at')):
            states.add('off')
        if item.get('will_renew') is True or item.get('cancel_at_period_end') is False:
            states.add('on')
    return next(iter(states)) if len(states) == 1 else 'unknown'


def validate_return_url(raw, cp):
    try:
        if not isinstance(raw, str) or any(c.isspace() or ord(c) < 32 for c in raw) or '\\' in raw:
            raise ValueError()
        u = urlsplit(raw)
        if (u.scheme != 'https' or u.netloc != 'chatgpt.com' or u.path != '/checkout/verify'
                or u.fragment or re.search(r'%(?![0-9a-fA-F]{2})', u.query) or ';' in u.query):
            raise ValueError()
        q = parse_qs(u.query, keep_blank_values=True, encoding='utf-8', errors='strict')
        if not cp['checkout_id'] or q.get('stripe_session_id') != [cp['checkout_id']]:
            raise ValueError()
        for field, expected in (('account_id', cp['account_id']), ('processor_entity', cp['entity'])):
            if field in q and q[field] != [expected]:
                raise ValueError()
        return raw
    except (ValueError, TypeError, KeyError):
        raise FollowupError('invalid_return_url') from None


def select_return_url(cp, responses):
    for data in responses:
        if not isinstance(data, dict):
            continue
        completion = data.get('after_completion')
        completion = completion if isinstance(completion, dict) else {}
        redirect = completion.get('redirect')
        redirect = redirect if isinstance(redirect, dict) else {}
        for candidate in (data.get('success_url'), data.get('return_url'), data.get('url'),
                          redirect.get('url'), completion.get('url')):
            if candidate:
                try:
                    return validate_return_url(candidate, cp)
                except FollowupError:
                    pass
    # Same merchant verify URL used by cs-live's selectHostedLanding.
    return validate_return_url(BASE + '/checkout/verify?' + urlencode({
        'stripe_session_id': cp['checkout_id'], 'processor_entity': cp['entity'],
        'plan_type': HOSTED_TYPES[cp['plan']],
    }), cp)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def subscription_not_found(method, path, status, headers, raw):
    if method != 'GET' or urlsplit(path).path != SUBSCRIPTIONS or status != 404 or len(raw) > 4096:
        return False
    media = headers.get('content-type', '').split(';', 1)[0].strip().lower()
    if media and media != 'application/json' and not (media.startswith('application/') and media.endswith('+json')):
        return False
    try:
        data = json.loads(raw, object_pairs_hook=_unique_object)
        return isinstance(data, dict) and data.get('detail') == 'No subscription found for account'
    except (ValueError, UnicodeError):
        return False


class Platform:
    """All requests share the order's saved proxy and retain its lifetime checks."""
    def __init__(self, client, cp, pause=time.sleep):
        self.client, self.cp = client, cp
        self.pause = pause
        self.context = OrderHTTPContext(cp['credentials'], cp.get('request_context'))
        client.cookies.update(self.context.cookies)

    def _snapshot_context(self):
        self.context.cookies = self.client.cookies
        self.cp['request_context'] = self.context.snapshot()
        cookies = {c.name: c.value for c in self.client.cookies.jar
                   if c.domain.lstrip('.') == 'chatgpt.com' and COOKIE_NAME.fullmatch(c.name) and not c.is_expired()}
        for name in list(cookies):
            if re.search(r'\.\d+$', name) and name.rsplit('.', 1)[0] in cookies:
                del cookies[name]
        if cookies:
            self.cp['credentials'].update(cookies=cookies, session_token='')

    def _diagnostic_secrets(self):
        c = self.cp['credentials']
        values = [c.get('access_token'), c.get('session_token'), c.get('email'), self.cp.get('account_id')]
        values.extend((c.get('cookies') or {}).values())
        values.extend(cookie.value for cookie in self.client.cookies.jar)
        proxy = urlsplit(self.cp.get('proxy_endpoint') or '')
        values.extend(unquote(value) for value in (proxy.username, proxy.password) if value)
        if proxy.username and proxy.password:
            values.append(unquote(proxy.username) + ':' + unquote(proxy.password))
        values.append(self.cp.get('proxy_endpoint'))
        return values

    def _check_lifetime(self):
        if datetime.fromisoformat(self.cp['proxy_expires_at']) <= now():
            raise FollowupError('proxy_session_expired')

    @staticmethod
    def _read(response, limit=262144):
        raw = bytearray()
        for chunk in response.iter_bytes():
            raw.extend(chunk[:limit + 1 - len(raw)])
            if len(raw) > limit:
                raise FollowupError('invalid_response')
        return bytes(raw)

    @staticmethod
    def _read_failure(response):
        raw = bytearray()
        for chunk in response.iter_bytes():
            raw.extend(chunk[:BODY_LIMIT + 1 - len(raw)])
            if len(raw) > BODY_LIMIT:
                break
        return bytes(raw)

    def headers(self, path, renewal=False):
        h = {'Authorization': 'Bearer ' + self.cp['credentials']['access_token'],
             'Chatgpt-Account-Id': self.cp['account_id'], 'Accept': 'application/json',
             'Origin': BASE, 'Referer': BASE + '/', 'User-Agent': 'Mozilla/5.0'}
        h = dict(httpx.Headers(h).items())
        h.update(self.context.headers)
        target = urlsplit(path).path
        if renewal:
            target = target.replace('/backend-api', '/api/v1')
        if target:
            h.update({'X-Openai-Target-Path': target, 'X-Openai-Target-Route': target})
        return h

    def request(self, method, path, kind, *, renewal=False, payload=None, refresh=False, landing=False):
        self._check_lifetime()
        headers = self.headers('' if landing else path, renewal)
        if refresh:
            headers.pop('authorization', None)
        url = path if landing else BASE + path
        attempts = 3 if method == 'GET' else 1
        for attempt in range(attempts):
            self._check_lifetime()
            retry = False
            try:
                with trace.request(kind) as observation:
                    kwargs = {'headers': headers, 'follow_redirects': False}
                    if payload is not None:
                        kwargs['json'] = payload
                    try:
                        with self.client.stream(method, url, **kwargs) as response:
                            observation.response(response.status_code, response.headers)
                            old_secrets = self._diagnostic_secrets()
                            # httpx absorbs the actual Set-Cookie on this same client.
                            self._snapshot_context()
                            raw = (self._read_failure(response) if response.status_code >= 300
                                   else b'' if landing else self._read(response))
                            observation.response(response.status_code, response.headers,
                                raw.decode('utf-8', errors='replace'),
                                secrets=old_secrets + self._diagnostic_secrets(), failure_details=True)
                            retry = attempt + 1 < attempts and response.status_code in RETRY_GET_STATUS
                            observation.diagnostics['retrying'] = retry
                            if subscription_not_found(method, path, response.status_code, response.headers, raw):
                                observation.code = 'subscription_absent'
                                raise FollowupError('subscription_not_found')
                            if not 200 <= response.status_code < 300:
                                raise FollowupError('http_' + str(response.status_code))
                            if landing:
                                return {}  # A successful visit is not proof of activation.
                            try:
                                data = json.loads(raw)
                            except (ValueError, UnicodeError):
                                raise FollowupError('invalid_response') from None
                            if not isinstance(data, dict):
                                raise FollowupError('invalid_response')
                            return data
                    except httpx.HTTPError as exc:
                        retry = attempt + 1 < attempts
                        observation.diagnostics['retrying'] = retry
                        reason = trace.error_code(exc)
                        if reason in {'timeout', 'proxy_connect', 'dns', 'tls', 'connection'}:
                            observation.diagnostics['network_reason'] = reason
                        raise FollowupError('network_error') from None
            except FollowupError:
                if not retry:
                    raise
            finally:
                self._check_lifetime()
            wait = 0.5 * (2 ** attempt)
            started = time.monotonic()
            trace.record(trace.REQUEST_PHASES[kind], 'waiting', 'request_retry_wait',
                         diagnostics={'request_kind': kind, 'retry_wait_ms': round(wait * 1000)})
            self.pause(wait)
            trace.record(trace.REQUEST_PHASES[kind], 'completed', 'request_retry_wait',
                         diagnostics={'request_kind': kind, 'retry_wait_ms': round(wait * 1000),
                                      'retry_wait_elapsed_ms': trace.round_ms(started)})

    def visit_return(self, url):
        self.request('GET', validate_return_url(url, self.cp), 'merchant_return', landing=True)

    def subscriptions(self, renewal=False):
        return self.request('GET', SUBSCRIPTIONS + '?' + urlencode({'account_id': self.cp['account_id']}),
                            'renewal_read' if renewal else 'subscription_read', renewal=renewal)

    def refresh(self):
        c = self.cp['credentials']
        if not c.get('session_token') and not c.get('cookies'):
            raise FollowupError('session_token_required')
        data = self.request('GET', '/api/auth/session?refresh=true&reason=token_expired',
                            'session_read', renewal=True, refresh=True)
        token = data.get('accessToken')
        if not isinstance(token, str) or not token or data.get('error'):
            raise FollowupError('session_expired')
        account = data.get('account')
        account = account if isinstance(account, dict) else {}
        if account.get('id') and account['id'] != self.cp['account_id']:
            raise FollowupError('account_mismatch')
        c['access_token'] = token
        c['refreshed_at'] = now().isoformat()
        cookies = {x.name: x.value for x in self.client.cookies.jar
                   if COOKIE_NAME.fullmatch(x.name) and x.domain.lstrip('.') == 'chatgpt.com'}
        for key in list(cookies):
            if re.search(r'\.\d+$', key) and key.rsplit('.', 1)[0] in cookies:
                del cookies[key]
        if cookies:
            c.update(cookies=cookies, session_token='')

    def cancel(self):
        self.request('POST', SUBSCRIPTIONS + '/cancel', 'renewal_update', renewal=True,
                     payload={'account_id': self.cp['account_id']})


class Followup:
    """Port of finishPayment / confirmActivation / disableRenewal in cs_live.go."""
    def __init__(self, cp, platform, save, pause=time.sleep):
        self.cp, self.platform, self.save, self.pause = cp, platform, save, pause

    def finish(self):
        self.confirm_activation()
        if not self.cp['cancel_requested']:
            self.save('completed')
        else:
            self.disable_renewal()

    def confirm_activation(self):
        cp, platform = self.cp, self.platform
        self.save('activation')
        if cp.get('return_url'):
            try:
                url = validate_return_url(cp['return_url'], cp)
            except FollowupError:
                trace.record('activation', 'review', 'invalid_return_url')
            else:
                platform.visit_return(url)
        for attempt in range(5):
            if attempt:
                self.pause(2)
            try:
                try:
                    data = platform.subscriptions()
                except FollowupError as exc:
                    if str(exc) != 'http_401':
                        raise
                    platform.refresh()
                    self.save('')
                    data = platform.subscriptions()
            except FollowupError as exc:
                if str(exc) == 'subscription_not_found':
                    continue  # Entry is restricted to durable proof of successful payment.
                raise
            if subscription_active(data, cp['plan']):
                cp.update(activation='active', renewal=renewal_state(data))
                break
        self.save('')
        if cp['activation'] != 'active':
            raise FollowupError('activation_pending')

    def disable_renewal(self):
        cp, platform = self.cp, self.platform
        trace.record('renewal', 'running', 'renewal_pending')
        platform.refresh()
        self.save('cancellation')
        cp['renewal'] = renewal_state(platform.subscriptions(True))
        if cp['renewal'] == 'unknown':
            raise FollowupError('renewal_state_unknown')
        if cp['renewal'] == 'off':
            self.save('completed')
            return
        for attempt in range(2):
            self.save('cancellation_requested')  # Durable fence before every cancellation write.
            try:
                platform.cancel()
                break
            except FollowupError as original:
                # IP/session guard errors cannot authorize any further business request.
                if str(original).startswith('proxy_'):
                    raise
                cp['renewal'] = renewal_state(platform.subscriptions(True))
                if cp['renewal'] == 'off':
                    self.save('completed')
                    return
                if attempt or cp['renewal'] != 'on' or str(original) not in RETRY_CANCEL:
                    raise original
        for attempt in range(5):
            if attempt:
                self.pause(2)
            cp['renewal'] = renewal_state(platform.subscriptions(True))
            if cp['renewal'] == 'off':
                self.save('completed')
                return
        raise FollowupError('cancellation_pending')


def warning_code(exc):
    code = str(exc) if isinstance(exc, FollowupError) else 'internal_error'
    if code.startswith('http_') and re.fullmatch(r'http_[1-5]\d\d', code):
        return code
    return code if code in trace.CODES else 'internal_error'


def _save(order_id, cp, phase='', warning=''):
    saved = dict(cp, phase=phase or cp['phase'], warning_code=warning)
    activation = 'active' if cp['activation'] == 'active' else 'pending' if not warning or warning == 'activation_pending' else 'review'
    renewal = ('disabled' if cp['renewal'] == 'off' else 'kept') if saved['phase'] == 'completed' else 'review' if warning else 'pending'
    events = {
        'activation': ('activation', '正在访问付款回跳地址并核实订阅生效。'),
        'cancellation': ('renewal_pending', '已刷新登录态，正在核实自动续费状态。'),
        'cancellation_requested': ('renewal_pending', '已保存收尾进度，正在提交关闭自动续费。'),
    }
    with db.transaction() as q:
        prior = q.one('SELECT activation,renewal,status FROM orders WHERE id=$1 FOR UPDATE', order_id)
        if not prior or prior['status'] != 'succeeded':
            raise FollowupError('payment_evidence_missing')
        q.execute('INSERT INTO subscription_followups(order_id,checkpoint_enc,phase,warning_code) VALUES($1,$2,$3,$4) '
                  'ON CONFLICT(order_id) DO UPDATE SET checkpoint_enc=$2,phase=$3,warning_code=$4,updated_at=now()',
                  order_id, encrypt(json.dumps(saved)), saved['phase'], warning)
        q.execute('UPDATE orders SET activation=$2,renewal=$3,updated_at=now() WHERE id=$1', order_id, activation, renewal)
        q.execute('UPDATE subscription_runs SET credentials_enc=$2 WHERE order_id=$1',
                  order_id, encrypt(json.dumps(cp['credentials'])))
        milestones = []
        if phase in events:
            milestones.append(events[phase])
        if activation == 'active' and prior['activation'] != 'active':
            milestones.append(('activation_confirmed', '订阅套餐及生效凭据已确认。'))
            trace.record('activation', 'completed', 'activation_confirmed', order_id=order_id, q=q)
        if phase == 'completed':
            code = 'renewal_disabled' if renewal == 'disabled' else 'renewal_kept'
            milestones.append((code, trace.CODES[code]))
            trace.record('renewal', 'completed', code, order_id=order_id, q=q)
            trace.record('complete', 'completed', 'completed', order_id=order_id, q=q)
        elif warning:
            milestones.append(('post_payment_review', '付款成功；后续核实尚未完成，已保存收尾进度。'))
            trace.record('renewal' if activation == 'active' else 'activation', 'review',
                         trace.error_code(FollowupError(warning)), order_id=order_id, q=q)
            trace.record('complete', 'review', 'review', order_id=order_id, q=q)
        for stage, message in milestones:
            q.execute('INSERT INTO order_events(order_id,stage,message) VALUES($1,$2,$3)', order_id, stage, message)
    cp.update(phase=saved['phase'], warning_code=warning)


def _run(order_id, *, plan=None, responses=(), proxy_url=None, request_context=None, executor='python'):
    # Cross-process exclusion also covers the recovery CLI. No network while holding an order row lock.
    with db.transaction() as lock:
        if not lock.scalar('SELECT pg_try_advisory_xact_lock(1886011,hashtext($1))', order_id):
            return {'state': 'busy', 'warning_code': 'followup_busy'}
        row = db.one('SELECT o.status,o.workflow,o.keep_renewal,s.paid_at,s.checkout_session_id,s.credentials_enc,s.trace_round,s.subscription_flow '
                     'FROM orders o JOIN subscription_runs s ON s.order_id=o.id WHERE o.id=$1', order_id)
        if not row or row['workflow'] != 'subscription' or row['status'] != 'succeeded' or not row['paid_at']:
            raise FollowupError('payment_evidence_missing')
        if executor == 'go_cslive_v1' and row['subscription_flow'] != 'gener_direct_pay':
            raise FollowupError('checkpoint_mismatch')
        saved = db.one('SELECT checkpoint_enc FROM subscription_followups WHERE order_id=$1', order_id)
        cp = json.loads(decrypt(saved['checkpoint_enc'])) if saved else None
        if cp and cp['phase'] == 'completed':
            return {'state': 'completed', 'warning_code': ''}
        if cp is None and plan is None:
            raise FollowupError('checkpoint_missing')
        proxy = db.one('SELECT endpoint_enc,expires_at FROM order_proxy_sessions WHERE order_id=$1', order_id)
        if not proxy or not proxy['endpoint_enc']:
            raise FollowupError('proxy_session_missing')
        endpoint = decrypt(proxy['endpoint_enc'])
        if proxy_url is not None and proxy_url != endpoint:
            raise FollowupError('proxy_session_changed')
        if cp is None:
            creds = json.loads(decrypt(row['credentials_enc']))
            cp = {'version': 1, 'phase': 'activation', 'plan': canonical_plan(plan),
                  'checkout_id': row['checkout_session_id'], 'account_id': creds['account_id'],
                  'entity': 'openai_llc', 'cancel_requested': not row['keep_renewal'],
                  'activation': 'pending', 'renewal': 'unknown', 'credentials': creds,
                  'proxy_endpoint': endpoint,
                  'proxy_expires_at': proxy['expires_at'].isoformat()}
            if request_context is not None:
                cp['request_context'] = OrderHTTPContext(snapshot=request_context).snapshot()
            cp['return_url'] = select_return_url(cp, responses)
            cp['executor'] = executor
            _save(order_id, cp)
        cp['executor'] = executor
        with trace.scope(order_id, row['trace_round']):
            try:
                if (cp['proxy_endpoint'] != endpoint
                        or cp['proxy_expires_at'] != proxy['expires_at'].isoformat()):
                    raise FollowupError('proxy_session_changed')
                if proxy['expires_at'] <= now():
                    raise FollowupError('proxy_session_expired')
                if cp['checkout_id'] != row['checkout_session_id']:
                    raise FollowupError('checkpoint_mismatch')
                if executor == 'go_cslive_v1':
                    from .go_post_payment import execute
                    execute(cp, lambda phase: _save(order_id, cp, phase))
                else:
                    with proxy_client(endpoint, 60) as client:
                        Followup(cp, Platform(client, cp), lambda phase: _save(order_id, cp, phase)).finish()
                return {'state': 'completed', 'warning_code': ''}
            except Exception as exc:
                if cp['phase'] == 'completed':
                    return {'state': 'completed', 'warning_code': ''}
                code = warning_code(exc)
                _save(order_id, cp, warning=code)
                return {'state': 'review', 'warning_code': code}


def record_unexpected_failure(order_id, exc):
    """Best effort fallback: never let follow-up failure enter the payment failure handler."""
    code = warning_code(exc)
    log.warning('Post-payment follow-up needs review: order=%s code=%s', order_id, code)
    try:
        with db.transaction() as q:
            changed = q.execute("UPDATE orders SET activation=CASE WHEN activation='active' THEN activation ELSE 'review' END,"
                      "renewal=CASE WHEN renewal IN ('disabled','kept') THEN renewal ELSE 'review' END,updated_at=now() "
                      "WHERE id=$1 AND status='succeeded'", order_id)
            if not changed:
                return {'state': 'review', 'warning_code': code}
            q.execute("INSERT INTO order_events(order_id,stage,message) VALUES($1,'post_payment_review',"
                      "'付款成功；收尾记录异常，请查看技术日志后核实，不要重复付款。')", order_id)
            trace.record('complete', 'review', trace.error_code(exc), order_id=order_id, q=q)
    except Exception:
        log.warning('Could not save follow-up review marker: order=%s', order_id)
    return {'state': 'review', 'warning_code': code}


def finish_payment(order_id, *, plan, responses=(), proxy_url=None, request_context=None):
    try:
        return _run(order_id, plan=plan, responses=responses, proxy_url=proxy_url, request_context=request_context)
    except Exception as exc:
        return record_unexpected_failure(order_id, exc)


def resume(order_id):
    """Explicit recovery of an existing post-charge checkpoint; no historical replay."""
    flow = db.scalar('SELECT subscription_flow FROM subscription_runs WHERE order_id=$1', order_id)
    return _run(order_id, executor='go_cslive_v1' if flow == 'gener_direct_pay' else 'python')
