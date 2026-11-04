"""cslive_full_go 订阅办理：直接调用 cs_live Go 完成提链+付款+付款后续一体流程。

link 工作流只生成结账链接；subscription 工作流用 cs_live pay 一体完成（同一浏览器会话，
IP/指纹/Stripe 会话全程一致，自带 Sentinel/Turnstile 破解与生效核实、关续费）。
"""
from __future__ import annotations

import json
import logging
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, Request
from .customer_contract import order_hash

from . import db, payment_trace as trace
from . import cslive_full_checkout
from . import subscription_retry
from .common import *
from .membership import fee_for, membership_for
from .runtime import order_workflows, queue_message
from .renewals import ProviderError, inspect as inspect_session, credentials as extract_credentials

log = logging.getLogger('xufu.subscription_cslive_full')
router = APIRouter(prefix='/api/cslive-full', dependencies=[Depends(member)])
FLOW = 'cslive_full_go'
CLAIM_LOCK = 1886008
PROXY_LOCK = 1886009

COUNTRIES = {'PH', 'US', 'JP', 'SG', 'EG', 'TR', 'GB'}
CURRENCY = {'PH': 'PHP', 'US': 'USD', 'JP': 'JPY', 'SG': 'SGD', 'EG': 'EGP', 'TR': 'TRY', 'GB': 'GBP'}


def _resolve_checkout_type(cfg: dict, product_id: str) -> str:
    types = cfg.get('checkout_types') or {}
    val = types.get(product_id)
    if val in ('hosted', 'oaics'):
        return val
    default = cfg.get('checkout_type') or 'hosted'
    return default if default in ('hosted', 'oaics') else 'hosted'


def _proxy_url_with_session(resource: dict, country: str) -> tuple[str, int]:
    import re as _re
    endpoint = decrypt(resource['endpoint_enc'])
    vendor = resource.get('vendor', '')
    mode = resource.get('ip_mode', 'dynamic')
    if mode != 'dynamic':
        return endpoint, 3600
    p = urlsplit(endpoint)
    username = unquote(p.username or '')
    password = unquote(p.password or '')
    if vendor == 'cliproxy':
        username = _re.sub(r'(?i)-sid-[^-]*', '', username)
        username = _re.sub(r'(?i)-t-[^-]*', '', username)
        if '-region-' not in username.lower():
            username += '-region-' + country.upper()
        username += '-sid-' + uid()[:16] + '-t-15'
        netloc = f'{quote(username, safe="")}:{quote(password, safe="")}@{p.hostname}:{p.port}'
        return urlunsplit((p.scheme, netloc, '', '', '')), 15 * 60
    if vendor == 'iproyal' or (p.hostname or '').lower() == 'geo.iproyal.com':
        if '_country-' not in password.lower():
            password += '_country-' + country.lower()
        token = '_session-' + uid()[:16]
        password = _re.sub(r'(?i)_session-[^_]*', token, password) if _re.search(r'(?i)_session-', password) else password + token
        m = _re.search(r'(?i)_lifetime-([^_]+)', password)
        lifetime_sec = 3600
        if m:
            try:
                val = m.group(1).lower()
                if val.endswith('h'):
                    lifetime_sec = int(float(val[:-1]) * 3600)
                elif val.endswith('m'):
                    lifetime_sec = int(float(val[:-1]) * 60)
                elif val.endswith('s'):
                    lifetime_sec = int(float(val[:-1]))
                if lifetime_sec < 60:
                    raise ValueError('too short')
            except Exception:
                lifetime_sec = 3600
        else:
            password += '_lifetime-1h'
        if lifetime_sec > 86400:
            lifetime_sec = 86400
        netloc = f'{quote(username, safe="")}:{quote(password, safe="")}@{p.hostname}:{p.port}'
        return urlunsplit((p.scheme, netloc, '', '', '')), lifetime_sec
    return endpoint, 3600


@trace.phase('proxy')
def _acquire_order_proxy(order_id: str, country: str, duration_sec: int) -> str:
    with db.transaction() as q:
        q.execute('SELECT pg_advisory_xact_lock($1)', PROXY_LOCK)
        existing = q.one("SELECT endpoint_enc,expires_at FROM order_proxy_sessions WHERE order_id=$1 FOR UPDATE", order_id)
        if existing:
            if existing['expires_at'] > now():
                return decrypt(existing['endpoint_enc'])
            raise RuntimeError('proxy_session_expired')
        proxy_cfg = config('proxy', q)
        default_id = proxy_cfg.get('default_id', '')
        row = q.one(
            "SELECT r.id,r.endpoint_enc,r.vendor,r.ip_mode FROM resources r "
            "WHERE r.enabled AND r.kind='proxy' AND r.country=$2 "
            "AND (SELECT count(*) FROM proxy_leases l WHERE l.resource_id=r.id AND l.expires_at>now())<r.capacity "
            "ORDER BY (r.id=$1) DESC,(r.last_status='failed'),random()*r.weight DESC LIMIT 1",
            default_id, country)
        require(row, '无可用代理，请检查代理配置', 503)
        endpoint, lifetime_sec = _proxy_url_with_session(
            {'endpoint_enc': row['endpoint_enc'], 'vendor': row.get('vendor', ''), 'ip_mode': row.get('ip_mode', 'dynamic')}, country)
        lease = uid()
        q.execute("INSERT INTO proxy_leases(id,resource_id,expires_at) VALUES($1,$2,now()+($3*interval '1 second'))",
                  lease, row['id'], duration_sec)
        q.execute("INSERT INTO order_proxy_sessions(order_id,resource_id,endpoint_enc,expires_at) VALUES($1,$2,$3,now()+($4*interval '1 second'))",
                  order_id, row['id'], encrypt(endpoint), lifetime_sec)
        return endpoint


def _pick_billing(q) -> dict:
    cfg = config('billing', q)
    profiles = [p for p in (cfg.get('address_templates') or []) if p.get('enabled') and not p.get('test_only')]
    import secrets as _sec
    if not profiles:
        return {}
    return dict(_sec.choice(profiles), country=cfg['country'])


def _payment_billing(snapshot):
    keys = ('country', 'state', 'city', 'line1', 'postal_code', 'first_name', 'last_name', 'email')
    require(all(isinstance(snapshot.get(k), str) and snapshot[k].strip() for k in keys),
            '本单账单快照不完整，请重新选择系统账单资料', 500)
    address = {k: snapshot[k] for k in keys[:5]}
    return address, f"{snapshot['first_name']} {snapshot['last_name']}", snapshot['email']


def _billing_snapshot(profile, account_email):
    snapshot = {k: str(profile.get(k) or '').strip() for k in
                ('country', 'state', 'city', 'line1', 'postal_code', 'first_name', 'last_name', 'email')}
    snapshot['profile_id'] = profile.get('id', '')
    snapshot['email_source'] = 'system_billing' if snapshot['email'] else 'subscription_account'
    snapshot['email'] = snapshot['email'] or str(account_email or '').strip()
    _payment_billing(snapshot)
    return snapshot


@router.post('/orders')
async def create_order(request: Request):
    v = await body(request)
    workflow = v.get('workflow', 'link')
    user = current(request)
    cfg = config('payment')
    risk = config('risk')
    require(cfg.get('enabled') is True, '订阅办理暂未开放', 403)
    require(cfg.get('subscription_flow') == FLOW, '当前配置不支持此办理方式', 403)
    require(limit('order:' + user['id'], risk.get('order_per_minute', 5)), '提交过于频繁', 429)
    if workflow not in ('subscription', 'link'):
        raise BusinessError('不支持的工作流', 400)
    require(workflow in order_workflows(), queue_message(workflow), 503)
    if workflow == 'subscription':
        require(v.get('accepted') is True, '请确认服务约定')
    product_id = str(v.get('product_id') or '').strip()
    require(product_id, '请选择套餐')
    country = str(v.get('country') or 'PH').strip().upper()
    require(country in COUNTRIES, '不支持的订阅地区')
    try:
        seats = int(v.get('seats', 1))
        premium_seats = int(v.get('premium_seats', 1))
    except (TypeError, ValueError):
        raise BusinessError('席位数量无效')
    require(1 <= seats <= 100, '席位数量无效')
    require(1 <= premium_seats <= seats, '高级席位数量无效')
    keep_renewal = bool(v.get('keep_renewal', False))
    workspace_name = str(v.get('workspace_name') or 'work').strip() or 'work'
    idem_key = request.headers.get('Idempotency-Key', '')
    require(8 <= len(idem_key) <= 120, '请提供 8～120 位 Idempotency-Key')
    session_raw = v.get('session')
    require(session_raw, '请提供账号 Session')
    _, sess_info = inspect_session(session_raw)
    c, expiry = extract_credentials(
        session_raw if isinstance(session_raw, dict) else json.loads(session_raw) if isinstance(session_raw, str) else {}, sess_info)
    card = v.get('card')
    if workflow == 'subscription':
        require(
            isinstance(card, dict)
            and isinstance(card.get('number'), str) and card['number'].isdigit() and 12 <= len(card['number']) <= 19
            and isinstance(card.get('month'), str) and card['month'].isdigit() and 1 <= int(card['month']) <= 12
            and isinstance(card.get('year'), str) and card['year'].isdigit()
            and isinstance(card.get('cvc'), str) and card['cvc'].isdigit() and 3 <= len(card['cvc']) <= 4,
            '请提供有效的银行卡信息')
    with db.transaction() as q:
        product = q.one('SELECT id,name,fee,enabled FROM products WHERE id=$1', product_id)
        require(product and product['enabled'], '套餐不存在或已下架', 404)
        rh = order_hash(q, request, digest(json.dumps([workflow, product_id, country, seats], ensure_ascii=False)))
        old = q.one('SELECT id,status,request_hash FROM orders WHERE user_id=$1 AND idempotency_key=$2', user['id'], idem_key)
        if old:
            require(old.get('request_hash') == rh, '同一 Idempotency-Key 不能用于不同请求', 409)
            return {'id': old['id'], 'status': old['status'], 'replayed': True}
        m = membership_for(user['id'], q)
        fee = fee_for(m, product['fee']) if workflow == 'subscription' else 0
        if workflow == 'subscription':
            fresh_user = q.one('SELECT balance,frozen FROM users WHERE id=$1 FOR UPDATE', user['id'])
            require(fresh_user['balance'] - fresh_user['frozen'] >= fee, '可用余额不足，请充值后再试')
        open_count = q.scalar("SELECT count(*) FROM orders WHERE user_id=$1 AND status IN ('queued','processing','awaiting_payment')", user['id'])
        require(open_count < risk.get('max_open_orders', 10), '未结束的订单已达上限')
        billing = _pick_billing(q)
        require(billing, '暂无可用账单资料，请联系管理员', 503)
        price_snapshot = {'standard': product['fee'], 'member_fee': fee, 'membership': m['current']['code']}
        billing_address = _billing_snapshot(billing, sess_info['email'])
        order_id = uid()
        q.execute(
            "INSERT INTO orders(id,user_id,product_id,product_name,email,country,seats,status,fee,source,workflow,keep_renewal,price_snapshot,billing_address,idempotency_key,request_hash) "
            "VALUES($1,$2,$3,$4,$5,$6,$7,'queued',$8,$15,$9,$10,$11,$12,$13,$14)",
            order_id, user['id'], product_id, product['name'], sess_info['email'],
            country, seats, fee, workflow, keep_renewal, price_snapshot, billing_address, idem_key, rh,
            'api' if hasattr(request.state, 'customer_hash') else 'web')
        trace.record('accepted', 'completed', order_id=order_id, q=q)
        trace.record('billing', 'completed', 'billing_selected', order_id=order_id,
                     diagnostics={'email_source': billing_address['email_source']}, q=q)
        trace.record('queue', 'waiting', order_id=order_id, q=q)
        if workflow == 'subscription':
            q.execute("UPDATE users SET frozen=frozen+$2 WHERE id=$1", user['id'], fee)
            q.execute("INSERT INTO ledger(user_id,kind,amount,balance_after,frozen_after,reference,description,order_id) "
                      "SELECT id,'freeze',0,balance,frozen,$3,'服务费冻结',$4 FROM users WHERE id=$1",
                      user['id'], fee, 'freeze:' + order_id, order_id)
            q.execute("INSERT INTO subscription_runs(order_id,user_id,credentials_enc,credentials_expires_at,card_enc,workspace_name,premium_seats,billing_snapshot,subscription_flow) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)",
                      order_id, user['id'], encrypt(json.dumps(c)), expiry, encrypt(json.dumps(card)), workspace_name, premium_seats, billing_address, FLOW)
            q.execute("INSERT INTO order_events(order_id,stage,message) VALUES($1,'queued','订单已受理，等待分配资源。')", order_id)
            trace.record('funds', 'completed', 'hold', order_id=order_id, q=q)
        else:
            q.execute("INSERT INTO subscription_runs(order_id,user_id,credentials_enc,credentials_expires_at,card_enc,workspace_name,premium_seats,billing_snapshot,subscription_flow) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)",
                      order_id, user['id'], encrypt(json.dumps(c)), expiry, encrypt(json.dumps({})), workspace_name, premium_seats, billing_address, FLOW)
            q.execute("INSERT INTO order_events(order_id,stage,message) VALUES($1,'queued','结账链接请求已受理，等待生成。')", order_id)
    return {'id': order_id, 'status': 'queued'}


def claim_subscription():
    cfg = config('payment')
    sched = config('scheduler')
    workflows = order_workflows()
    if not workflows or sched.get('paused') or cfg.get('enabled') is not True:
        return None
    with db.transaction() as q:
        q.execute('SELECT pg_advisory_xact_lock($1)', CLAIM_LOCK)
        active = q.scalar("SELECT count(*) FROM orders o JOIN subscription_runs s ON s.order_id=o.id "
                          "WHERE o.status='processing' AND o.workflow IN ('subscription','link') AND s.subscription_flow=$1", FLOW)
        if active >= cfg['max_parallel']:
            return None
        row = q.one(
            "SELECT o.id,o.user_id,o.product_id,o.product_name,o.email,o.country,o.seats,"
            "o.keep_renewal,o.fee,o.billing_address,o.price_snapshot,o.workflow,"
            "s.credentials_enc,s.credentials_expires_at,s.card_enc,s.workspace_name,s.premium_seats,s.billing_snapshot,s.session_id,s.subscription_flow,"
            "s.network_retry_count,s.checkout_checkpoint_enc"
            " FROM orders o JOIN subscription_runs s ON s.order_id=o.id"
            " WHERE o.status='queued' AND o.workflow=ANY($2::text[]) AND s.subscription_flow=$3"
            " AND s.network_retry_at<=now() AND s.payment_submit_started_at IS NULL AND s.paid_at IS NULL"
            " AND (SELECT count(*) FROM orders x JOIN subscription_runs y ON y.order_id=x.id "
            "      WHERE x.user_id=o.user_id AND x.status='processing' AND x.workflow IN ('subscription','link') AND y.subscription_flow=$3)<$1"
            " ORDER BY o.created_at LIMIT 1 FOR UPDATE OF o SKIP LOCKED",
            cfg['per_user_parallel'], list(workflows), FLOW)
        if not row:
            return None
        q.execute("UPDATE orders SET status='processing',updated_at=now() WHERE id=$1", row['id'])
        row['trace_round'] = q.scalar('UPDATE subscription_runs SET trace_round=trace_round+1 WHERE order_id=$1 RETURNING trace_round', row['id'])
        trace.record('queue', 'completed', 'completed', order_id=row['id'], round=row['trace_round'], q=q)
        msg = '正在分配代理资源。' if row['workflow'] == 'subscription' else '正在生成结账链接。'
        q.execute("INSERT INTO order_events(order_id,stage,message) VALUES($1,'processing',$2)", row['id'], msg)
        return row


def _add_event(order_id: str, stage: str, message: str):
    db.execute("INSERT INTO order_events(order_id,stage,message) VALUES($1,$2,$3)", order_id, stage, message)
    milestones = {
        'checkout': ('checkout', 'running', ''), 'checkout_done': ('checkout', 'completed', 'completed'),
        'payment': ('card_prepare', 'running', ''), 'confirming': ('confirming', 'running', ''),
        'activation_confirmed': ('activation', 'completed', 'activation_confirmed'),
        'activation_pending': ('activation', 'waiting', 'activation_pending'),
        'renewal_disabled': ('renewal', 'completed', 'renewal_disabled'),
        'renewal_kept': ('renewal', 'completed', 'renewal_kept'),
        'post_payment_review': ('activation', 'review', 'review'),
    }
    if stage in milestones:
        trace.record(*milestones[stage], order_id=order_id)


def _fail_order(order_id, user_id, fee, message, code='internal_error'):
    with db.transaction() as q:
        q.execute("UPDATE orders SET status='failed',renewal='not_started',updated_at=now() WHERE id=$1", order_id)
        if fee > 0:
            q.execute("UPDATE users SET frozen=GREATEST(0,frozen-$2) WHERE id=$1", user_id, fee)
            q.execute("INSERT INTO ledger(user_id,kind,amount,balance_after,frozen_after,reference,description,order_id) "
                      "SELECT id,'unfreeze',0,balance,frozen,$3,'服务费解冻（办理失败）',$4 FROM users WHERE id=$1",
                      user_id, fee, 'unfreeze:' + order_id, order_id)
        q.execute("INSERT INTO order_events(order_id,stage,message) VALUES($1,'failed',$2)", order_id, message)
        if fee > 0:
            trace.record('funds', 'completed', 'release', order_id=order_id, q=q)
        trace.record('complete', 'failed', code, order_id=order_id, q=q)


def _succeed_order(order_id, user_id, fee, last4, charged_amount, charged_currency, activation, renewal, keep_renewal):
    act = 'active' if activation == 'active' else 'pending'
    ren = 'disabled' if renewal == 'off' else 'kept' if keep_renewal else 'pending'
    with db.transaction() as q:
        q.execute("UPDATE orders SET status='succeeded',renewal=$2,activation=$3,updated_at=now() WHERE id=$1", order_id, ren, act)
        q.execute("UPDATE subscription_runs SET payment_last4=$2,payment_amount=$3,payment_currency=$4,paid_at=now() WHERE order_id=$1",
                  order_id, last4, charged_amount, charged_currency)
        q.execute("UPDATE users SET balance=balance-$2,frozen=GREATEST(0,frozen-$2) WHERE id=$1", user_id, fee)
        q.execute("INSERT INTO ledger(user_id,kind,amount,balance_after,frozen_after,reference,description,order_id) "
                  "SELECT id,'charge',-$2,balance,frozen,$3,'订阅服务费',$4 FROM users WHERE id=$1",
                  user_id, fee, 'payment:' + order_id, order_id)
        q.execute("INSERT INTO order_events(order_id,stage,message) VALUES($1,'payment_succeeded','付款成功，订阅已办理。')", order_id)
        trace.record('result', 'completed', 'payment_succeeded', order_id=order_id, q=q)
        trace.record('funds', 'completed', 'charge', order_id=order_id, q=q)
        if act == 'active':
            trace.record('activation', 'completed', 'activation_confirmed', order_id=order_id, q=q)
        code = 'renewal_disabled' if ren == 'disabled' else 'renewal_kept'
        trace.record('renewal', 'completed', code, order_id=order_id, q=q)
        trace.record('complete', 'completed' if act == 'active' else 'review', 'completed' if act == 'active' else 'review', order_id=order_id, q=q)


def _review_order(order_id, message, code):
    with db.transaction() as q:
        q.execute("UPDATE orders SET status='review',updated_at=now() WHERE id=$1 AND status='processing'", order_id)
        q.execute("INSERT INTO order_events(order_id,stage,message) VALUES($1,'payment_review',$2)", order_id, message)
        trace.record('result', 'review', code, order_id=order_id, q=q)


@trace.order_run
def run_subscription(job: dict):
    order_id = job['id']
    if not subscription_retry.can_start(order_id) and job.get('workflow', 'subscription') == 'subscription':
        return
    user_id = job['user_id']
    fee = job['fee']
    country = job['country']
    keep_renewal = job['keep_renewal']
    workflow = job.get('workflow', 'subscription')
    cfg = config('payment')
    product_id = job['product_id']
    checkout_type = _resolve_checkout_type(cfg, product_id)
    try:
        if not job['credentials_enc'] or job['credentials_expires_at'] <= now():
            raise ProviderError('invalid_session')
        creds = json.loads(decrypt(job['credentials_enc']))
        card = json.loads(decrypt(job['card_enc'])) if workflow == 'subscription' and job.get('card_enc') else {}
        billing = job['billing_snapshot'] or {}
        plan = cslive_full_checkout.normalize_plan(product_id)
        currency = CURRENCY.get(country, 'USD')
        proxy_url = _acquire_order_proxy(order_id, country, 900)

        # link 工作流：只生成结账链接
        if workflow == 'link':
            _add_event(order_id, 'checkout', '正在调用 cs-live Go 生成结账链接…')
            link_result = cslive_full_checkout.create_checkout(
                session=creds, country=country, plan=plan, proxy_url=proxy_url, checkout_type=checkout_type)
            cs_id = link_result['checkoutSessionId']
            subscription_retry.save_checkout(order_id, FLOW, link_result, {})
            _add_event(order_id, 'checkout_done', f'结账链接已生成，cs={cs_id[:24]}…')
            with db.transaction() as q:
                q.execute("UPDATE orders SET status='awaiting_payment',updated_at=now() WHERE id=$1", order_id)
                q.execute("INSERT INTO order_events(order_id,stage,message) VALUES($1,'awaiting_payment','结账链接已生成，等待用户完成付款。')", order_id)
                trace.record('complete', 'waiting', 'link_ready', q=q)
            return

        # subscription 工作流：cs_live pay 一体完成
        _payment_billing(billing)
        _add_event(order_id, 'checkout', '正在调用 cs-live Go 办理（提链+付款+后续）…')
        subscription_retry.mark_payment_submission(order_id)
        result = cslive_full_checkout.pay(
            order_id, session=creds, card=card, billing=billing, country=country, plan=plan,
            proxy_url=proxy_url, checkout_type=checkout_type, cancel_renewal=not keep_renewal,
            trace_round=job.get('trace_round'))

        cs_id = str(result.get('checkout_id') or '').strip()
        if cs_id:
            db.execute("UPDATE subscription_runs SET checkout_session_id=$2 WHERE order_id=$1", order_id, cs_id)
        recovery = result.get('_recovery') or {}
        if recovery:
            db.execute("UPDATE subscription_runs SET checkout_checkpoint_enc=$2 WHERE order_id=$1",
                       order_id, encrypt(json.dumps(recovery)))

        payment = str(result.get('payment') or '').lower()
        if payment == 'charged':
            amount = result.get('amount')
            cur = str(result.get('currency') or currency).upper()
            last4 = card['number'][-4:]
            _succeed_order(order_id, user_id, fee, last4,
                           int(amount) if isinstance(amount, (int, float)) and amount else None,
                           cur, str(result.get('activation') or ''), str(result.get('renewal') or ''), keep_renewal)
        elif payment == 'declined':
            dc = result.get('decline_code') or result.get('provider_code') or 'card_declined'
            _fail_order(order_id, user_id, fee, '付款被拒绝，办理未完成。', 'payment_declined')
            log.info('[%s] declined: %s', order_id, dc)
        else:
            # action_required / pending / unknown / authorized → 保留交易，转 review，勿重复付款
            _review_order(order_id, '付款请求已提交，结果尚未确认；已保留原交易，请勿再次付款。', 'payment_result_unconfirmed')
    except ProviderError as e:
        if workflow == 'subscription' and subscription_retry.handle_failure(order_id, e):
            return
        code = trace.error_code(e)
        _fail_order(order_id, user_id, fee, '账号授权失效，办理未完成。' if code in ('invalid_session', 'account_mismatch') else trace.CODES.get(code, '办理失败'), code)
    except cslive_full_checkout.CheckoutError as e:
        if workflow == 'subscription' and subscription_retry.handle_failure(order_id, e):
            return
        code = trace.error_code(e)
        log.error('[%s] cslive_full error: %s', order_id, e.code)
        _fail_order(order_id, user_id, fee, trace.CODES.get(code, '办理失败'), code)
    except Exception as e:
        if workflow == 'subscription' and subscription_retry.handle_failure(order_id, e):
            return
        code = trace.error_code(e)
        log.error('[%s] subscription error: %s', order_id, code)
        _fail_order(order_id, user_id, fee, trace.CODES.get(code, '办理失败'), code)


@router.get('/subscriptions/{id}')
def subscription_detail(id: str, request: Request):
    user = current(request)
    row = db.one(
        "SELECT o.id,o.status,o.product_name,o.email,o.country,o.fee,o.renewal,o.activation,o.created_at,o.updated_at,"
        "s.payment_last4,s.payment_amount,s.payment_currency,s.paid_at,s.checkout_session_id"
        " FROM orders o JOIN subscription_runs s ON s.order_id=o.id"
        " WHERE o.id=$1 AND o.user_id=$2 AND o.workflow='subscription' AND s.subscription_flow=$3",
        id, user['id'], FLOW)
    require(row, '订单不存在', 404)
    events = db.all('SELECT stage,message,created_at FROM order_events WHERE order_id=$1 ORDER BY id', id)
    payment = {}
    if row.get('payment_last4'):
        payment = {'state': 'succeeded' if row['status'] == 'succeeded' else 'processing', 'last4': row['payment_last4'],
                   'charged_amount': row['payment_amount'], 'charged_currency': row['payment_currency'], 'paid_at': row['paid_at']}
    return {'order': row, 'events': events, 'payment': payment, 'checkout': {'checkout_session_id': row.get('checkout_session_id')}}
