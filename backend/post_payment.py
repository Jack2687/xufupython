"""Persistence and order validation for gener Go post-payment handling."""
from __future__ import annotations

import json
import logging
import re
from urllib.parse import parse_qs, urlencode, urlsplit


from . import db, payment_trace as trace
from .common import decrypt, encrypt, now
from .order_http_context import OrderHTTPContext

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


def _run(order_id, *, plan=None, responses=(), proxy_url=None, request_context=None, executor='go_cslive_v1'):
    # Cross-process exclusion also covers the recovery CLI. No network while holding an order row lock.
    with db.transaction() as lock:
        if not lock.scalar('SELECT pg_try_advisory_xact_lock(1886011,hashtext($1))', order_id):
            return {'state': 'busy', 'warning_code': 'followup_busy'}
        row = db.one('SELECT o.status,o.workflow,o.keep_renewal,s.paid_at,s.checkout_session_id,s.credentials_enc,s.trace_round,s.subscription_flow '
                     'FROM orders o JOIN subscription_runs s ON s.order_id=o.id WHERE o.id=$1', order_id)
        if not row or row['workflow'] != 'subscription' or row['status'] != 'succeeded' or not row['paid_at']:
            raise FollowupError('payment_evidence_missing')
        if executor != 'go_cslive_v1' or row['subscription_flow'] != 'gener_direct_pay':
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
                from .go_post_payment import execute
                execute(cp, lambda phase: _save(order_id, cp, phase))
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


def resume(order_id):
    """Explicit recovery of an existing post-charge checkpoint; no historical replay."""
    return _run(order_id)
