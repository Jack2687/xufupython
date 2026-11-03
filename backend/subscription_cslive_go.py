"""cslive_go_direct_pay 订阅办理：cs-live Go 提链 + HTTP 直连付款 + Go 付款后续。

完全独立复制自 subscription.py 的流程骨架；http 付款走 cslive_go_prolong，
付款后续走 cslive_go_post_payment，均为本流程专属副本，不与 gener_direct_pay 共享代码。
"""
from __future__ import annotations

import json
import logging
import time
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import requests
from fastapi import APIRouter, Depends, Request

from . import db, payment_trace as trace
from . import cslive_go_checkout
from . import cslive_go_post_payment
from . import subscription_retry
from .common import *
from .membership import fee_for, membership_for
from .runtime import order_workflows, queue_message
from .payment_transport import payment_session
from .renewals import ProviderError, inspect as inspect_session, credentials as extract_credentials
from .cslive_go_prolong.stripe_checksum import build_runtime_checksums, fetch_stripe_build_constants
from .cslive_go_prolong.stripe_hosted import (
    StripeHostedClient, PK_FALLBACK,
    build_payment_method_form, build_confirm_form,
    resolve_checkout_config_id, resolve_expected_amount,
)

log = logging.getLogger('xufu.subscription_cslive_go')
router = APIRouter(prefix='/api/cslive-go', dependencies=[Depends(member)])
FLOW = 'cslive_go_direct_pay'
CLAIM_LOCK = 1886006
PROXY_LOCK = 1886007


COUNTRIES = {'PH', 'US', 'JP', 'SG', 'EG', 'TR', 'GB'}
CURRENCY = {'PH': 'PHP', 'US': 'USD', 'JP': 'JPY', 'SG': 'SGD', 'EG': 'EGP', 'TR': 'TRY', 'GB': 'GBP'}
_CHECKOUT_PLAN_IDS = ('pro5x', 'pro', 'team', 'team_premium', 'plus', 'go')


def _resolve_checkout_type(cfg: dict, product_id: str) -> str:
    types = cfg.get('checkout_types') or {}
    val = types.get(product_id)
    if val in ('hosted', 'oaics'):
        return val
    default = cfg.get('checkout_type') or 'hosted'
    return default if default in ('hosted', 'oaics') else 'hosted'


def _proxy_url_with_session(resource: dict, country: str) -> tuple[str, int]:
    """Prepare a session-bound proxy URL. Returns (url, lifetime_seconds)."""
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
    """Acquire a session-bound proxy for this order. Same order always gets same IP."""
    with db.transaction() as q:
        q.execute('SELECT pg_advisory_xact_lock($1)', PROXY_LOCK)
        existing = q.one(
            "SELECT endpoint_enc,expires_at FROM order_proxy_sessions WHERE order_id=$1 FOR UPDATE",
            order_id
        )
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
            default_id, country
        )
        require(row, '无可用代理，请检查代理配置', 503)

        endpoint, lifetime_sec = _proxy_url_with_session({
            'endpoint_enc': row['endpoint_enc'],
            'vendor': row.get('vendor', ''),
            'ip_mode': row.get('ip_mode', 'dynamic'),
        }, country)

        lease = uid()
        q.execute(
            "INSERT INTO proxy_leases(id,resource_id,expires_at) VALUES($1,$2,now()+($3*interval '1 second'))",
            lease, row['id'], duration_sec
        )
        q.execute(
            "INSERT INTO order_proxy_sessions(order_id,resource_id,endpoint_enc,expires_at) VALUES($1,$2,$3,now()+($4*interval '1 second'))",
            order_id, row['id'], encrypt(endpoint), lifetime_sec
        )
        return endpoint


def _check_proxy_session_lifetime(order_id: str):
    row = db.one('SELECT expires_at FROM order_proxy_sessions WHERE order_id=$1', order_id)
    if row and row['expires_at'] <= now():
        raise RuntimeError('proxy_session_expired')


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
        session_raw if isinstance(session_raw, dict) else json.loads(session_raw) if isinstance(session_raw, str) else {},
        sess_info,
    )

    card = v.get('card')
    if workflow == 'subscription':
        require(
            isinstance(card, dict)
            and isinstance(card.get('number'), str) and card['number'].isdigit() and 12 <= len(card['number']) <= 19
            and isinstance(card.get('month'), str) and card['month'].isdigit() and 1 <= int(card['month']) <= 12
            and isinstance(card.get('year'), str) and card['year'].isdigit()
            and isinstance(card.get('cvc'), str) and card['cvc'].isdigit() and 3 <= len(card['cvc']) <= 4,
            '请提供有效的银行卡信息'
        )

    with db.transaction() as q:
        product = q.one('SELECT id,name,fee,enabled FROM products WHERE id=$1', product_id)
        require(product and product['enabled'], '套餐不存在或已下架', 404)

        rh = digest(json.dumps([workflow, product_id, country, seats], ensure_ascii=False))
        old = q.one('SELECT id,status,request_hash FROM orders WHERE user_id=$1 AND idempotency_key=$2',
                    user['id'], idem_key)
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
            "VALUES($1,$2,$3,$4,$5,$6,$7,'queued',$8,'web',$9,$10,$11,$12,$13,$14)",
            order_id, user['id'], product_id, product['name'], sess_info['email'],
            country, seats, fee, workflow, keep_renewal, price_snapshot, billing_address, idem_key, rh
        )
        trace.record('accepted', 'completed', order_id=order_id, q=q)
        trace.record('billing', 'completed', 'billing_selected', order_id=order_id,
                     diagnostics={'email_source': billing_address['email_source']}, q=q)
        trace.record('queue', 'waiting', order_id=order_id, q=q)

        if workflow == 'subscription':
            q.execute("UPDATE users SET frozen=frozen+$2 WHERE id=$1", user['id'], fee)
            q.execute(
                "INSERT INTO ledger(user_id,kind,amount,balance_after,frozen_after,reference,description,order_id) "
                "SELECT id,'freeze',0,balance,frozen,$3,'服务费冻结',$4 FROM users WHERE id=$1",
                user['id'], fee, 'freeze:' + order_id, order_id
            )
            q.execute(
                "INSERT INTO subscription_runs(order_id,user_id,credentials_enc,credentials_expires_at,card_enc,workspace_name,premium_seats,billing_snapshot,subscription_flow) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)",
                order_id, user['id'], encrypt(json.dumps(c)), expiry,
                encrypt(json.dumps(card)), workspace_name, premium_seats, billing_address, FLOW
            )
            q.execute("INSERT INTO order_events(order_id,stage,message) VALUES($1,'queued','订单已受理，等待分配资源。')", order_id)
            trace.record('funds', 'completed', 'hold', order_id=order_id, q=q)
        else:
            q.execute(
                "INSERT INTO subscription_runs(order_id,user_id,credentials_enc,credentials_expires_at,card_enc,workspace_name,premium_seats,billing_snapshot,subscription_flow) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)",
                order_id, user['id'], encrypt(json.dumps(c)), expiry,
                encrypt(json.dumps({})), workspace_name, premium_seats, billing_address, FLOW
            )
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
                          "WHERE o.status='processing' AND o.workflow IN ('subscription','link') AND s.subscription_flow=$1",
                          FLOW)
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
            cfg['per_user_parallel'], list(workflows), FLOW
        )
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
        'approving': ('confirming', 'running', ''), 'renewal_pending': ('renewal', 'waiting', 'renewal_pending'),
        'activation_confirmed': ('activation', 'completed', 'activation_confirmed'),
        'activation_pending': ('activation', 'waiting', 'activation_pending'),
        'renewal_disabled': ('renewal', 'completed', 'renewal_disabled'),
        'renewal_kept': ('renewal', 'completed', 'renewal_kept'),
        'renewal_review': ('renewal', 'review', 'review'),
        'post_payment_review': ('activation', 'review', 'review'),
    }
    if stage in milestones:
        trace.record(*milestones[stage], order_id=order_id)


def _fail_order(order_id: str, user_id: str, fee: int, message: str, code='internal_error'):
    with db.transaction() as q:
        q.execute("UPDATE orders SET status='failed',renewal='not_started',updated_at=now() WHERE id=$1", order_id)
        if fee > 0:
            q.execute("UPDATE users SET frozen=GREATEST(0,frozen-$2) WHERE id=$1", user_id, fee)
            q.execute(
                "INSERT INTO ledger(user_id,kind,amount,balance_after,frozen_after,reference,description,order_id) "
                "SELECT id,'unfreeze',0,balance,frozen,$3,'服务费解冻（办理失败）',$4 FROM users WHERE id=$1",
                user_id, fee, 'unfreeze:' + order_id, order_id
            )
        q.execute("INSERT INTO order_events(order_id,stage,message) VALUES($1,'failed',$2)", order_id, message)
        if fee > 0:
            trace.record('funds', 'completed', 'release', order_id=order_id, q=q)
        trace.record('complete', 'failed', code, order_id=order_id, q=q)


def _succeed_order(order_id: str, user_id: str, fee: int, last4: str, charged_amount: int, charged_currency: str):
    with db.transaction() as q:
        q.execute(
            "UPDATE orders SET status='succeeded',renewal='pending',activation='pending',updated_at=now() WHERE id=$1",
            order_id
        )
        q.execute(
            "UPDATE subscription_runs SET payment_last4=$2,payment_amount=$3,payment_currency=$4,paid_at=now() WHERE order_id=$1",
            order_id, last4, charged_amount, charged_currency
        )
        q.execute(
            "UPDATE users SET balance=balance-$2,frozen=GREATEST(0,frozen-$2) WHERE id=$1",
            user_id, fee
        )
        q.execute(
            "INSERT INTO ledger(user_id,kind,amount,balance_after,frozen_after,reference,description,order_id) "
            "SELECT id,'charge',-$2,balance,frozen,$3,'订阅服务费',$4 FROM users WHERE id=$1",
            user_id, fee, 'payment:' + order_id, order_id
        )
        q.execute("INSERT INTO order_events(order_id,stage,message) VALUES($1,'payment_succeeded','付款成功，等待生效核实。')", order_id)
        trace.record('result', 'completed', 'payment_succeeded', order_id=order_id, q=q)
        trace.record('funds', 'completed', 'charge', order_id=order_id, q=q)


@trace.order_run
def run_subscription(job: dict):
    order_id = job['id']
    if not subscription_retry.can_start(order_id):
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
        if workflow == 'subscription':
            billing_addr, cardholder_name, billing_email = _payment_billing(billing)

        # 1. Session-bound proxy by order country (shared between checkout and payment)
        proxy_url = _acquire_order_proxy(order_id, country, 600)

        cached = subscription_retry.load_checkout(job, FLOW)
        if cached:
            _add_event(order_id, 'checkout_reused', '沿用本单已生成的结账链接，从付款阶段重试。')
        else:
            _add_event(order_id, 'checkout', '正在调用 cs-live Go 程序生成结账链接…')

        plan_canonical = cslive_go_checkout._normalize_plan(product_id)
        currency = CURRENCY.get(country, 'USD')

        if cached:
            link_result = cached['result']
            request_context = cached.get('request_context') or {}
        else:
            session_payload = {
                'access_token': creds['access_token'],
                'account_id': creds['account_id'],
                'email': creds.get('email') or '',
                'session_token': creds.get('session_token') or '',
                'cookies': creds.get('cookies') or {},
            }
            link_result = cslive_go_checkout.create_checkout(
                session=session_payload,
                country=country,
                plan=plan_canonical,
                proxy_url=proxy_url,
                seats=job['seats'],
                seats_5x=job.get('premium_seats', 1),
                interval='month',
                checkout_type=checkout_type,
                currency=currency,
                with_price=True,
            )
            request_context = link_result.get('request_context') or {}

        require(link_result.get('ok'), f"提链失败: {link_result.get('error','unknown')}")
        require(link_result.get('payable') is not False, '提链结果不可付款，请更换账号或套餐')
        hosted_url = link_result['hostedUrl']
        cs_id = link_result['checkoutSessionId']

        actual_country = str(link_result.get('billingCountry') or country).upper()
        actual_currency = str(link_result.get('billingCurrency') or CURRENCY.get(country, 'USD')).upper()
        if actual_country != country or actual_currency != CURRENCY.get(country, 'USD'):
            _add_event(order_id, 'country_aligned',
                       f'账单国家/币种已对齐出口：{actual_country}/{actual_currency}')
            db.execute("UPDATE orders SET country=$2,updated_at=now() WHERE id=$1", order_id, actual_country)

        if not cached:
            subscription_retry.save_checkout(order_id, FLOW, link_result, request_context)
            _add_event(order_id, 'checkout_done', f'长链已生成，cs={cs_id[:24]}…')

        # link workflow ends here — mark awaiting_payment and return checkout URL
        if workflow == 'link':
            with db.transaction() as q:
                q.execute("UPDATE orders SET status='awaiting_payment',updated_at=now() WHERE id=$1", order_id)
                q.execute("INSERT INTO order_events(order_id,stage,message) VALUES($1,'awaiting_payment','结账链接已生成，等待用户完成付款。')", order_id)
                trace.record('complete', 'waiting', 'link_ready', q=q)
            return

        # 2. oaics (短链) 流程不代付：由用户在 ChatGPT 侧完成付款
        if checkout_type == 'oaics' or link_result.get('is_hosted') is False:
            with db.transaction() as q:
                q.execute("UPDATE orders SET status='awaiting_payment',updated_at=now() WHERE id=$1", order_id)
                q.execute("INSERT INTO order_events(order_id,stage,message) VALUES($1,'awaiting_payment','短链已生成，等待用户在 ChatGPT 侧完成付款。')", order_id)
                trace.record('complete', 'waiting', 'link_ready', q=q)
            return

        # 3. hosted 长链：直接 HTTP 付款
        _add_event(order_id, 'payment', '正在发起付款…')
        pk = link_result.get('publishable_key') or PK_FALLBACK
        stripe = StripeHostedClient(pk=pk, proxy=proxy_url, timeout=90)

        guid, muid, sid = stripe.register_device_fingerprint()
        init_resp = stripe.init(cs_id)
        init_checksum = str(init_resp['init_checksum'])
        config_id = resolve_checkout_config_id(init_resp)

        tax_resp = stripe.update_tax_region(cs_id, billing_addr)
        expected_amount = resolve_expected_amount(tax_resp, init_resp)
        currency_from_stripe = str(tax_resp.get('currency') or init_resp.get('currency') or currency).upper()

        import uuid as _uuid
        client_session_id = str(_uuid.uuid4())
        _check_proxy_session_lifetime(order_id)

        build = fetch_stripe_build_constants(session=stripe.session)
        pm_form = build_payment_method_form(
            card_number=card['number'],
            exp_month=card['month'].zfill(2),
            exp_year=str(card['year'])[-2:] if len(str(card['year'])) == 2 else str(card['year']),
            cvc=card['cvc'],
            name=cardholder_name,
            email=billing_email,
            pk=pk,
            guid=guid,
            muid=muid,
            sid=sid,
            client_session_id=client_session_id,
            cs=cs_id,
            checkout_config_id=config_id,
            runtime_version=build.version,
            address=billing_addr,
        )
        pm_resp = stripe.create_payment_method(pm_form)
        pm_id = str(pm_resp['id'])
        last4 = card['number'][-4:]

        computed = build_runtime_checksums(pm_id, session=stripe.session)
        runtime = {
            'version': computed['version'],
            'js_checksum': computed['js_checksum'],
            'rv_timestamp': computed['rv_timestamp'],
        }
        _check_proxy_session_lifetime(order_id)

        confirm_form = build_confirm_form(
            cs=cs_id,
            pm_id=pm_id,
            expected_amount=expected_amount,
            init_checksum=init_checksum,
            pk=pk,
            guid=guid,
            muid=muid,
            sid=sid,
            client_session_id=client_session_id,
            checkout_config_id=config_id,
            runtime=runtime,
        )
        _add_event(order_id, 'confirming', '正在提交付款确认…')
        subscription_retry.mark_payment_submission(order_id)
        confirm_resp = stripe.confirm(cs_id, confirm_form)

        def _classify(data: dict) -> tuple[bool, str, int]:
            status = str(data.get('status') or '').lower()
            payment_status = str(data.get('payment_status') or '').lower()
            sa = data.get('submission_attempt') or {}
            sa_state = str(sa.get('state') or sa.get('status') or '').lower()
            pi_obj = None
            for container in [data, sa, data.get('error') or {}]:
                raw = container.get('payment_intent')
                if isinstance(raw, dict) and raw.get('id', '').startswith('pi_'):
                    pi_obj = raw
                    break
            pi_status = str((pi_obj or {}).get('status') or '').lower()
            amount_received = (pi_obj or {}).get('amount_received')
            charged_amount = int(amount_received) if isinstance(amount_received, (int, float)) and amount_received > 0 else 0
            paid = (status == 'complete' or payment_status == 'paid' or pi_status == 'succeeded')
            if paid:
                return True, '', charged_amount
            declined_states = {'canceled', 'cancelled', 'declined', 'failed', 'expired'}
            if any(s in declined_states for s in [status, pi_status, sa_state]):
                pi_err = (pi_obj or {}).get('last_payment_error') or data.get('error') or {}
                if isinstance(pi_err, dict):
                    code = str(pi_err.get('decline_code') or pi_err.get('code') or 'card_declined')
                else:
                    code = 'card_declined'
                return False, code, 0
            return False, '', 0

        paid, decline_code, charged_minor = _classify(confirm_resp)

        if not paid:
            if decline_code:
                raise RuntimeError(f'payment_declined:{decline_code}')
            sa = confirm_resp.get('submission_attempt') or {}
            sa_state = str(sa.get('state') or sa.get('status') or '').lower()
            if sa_state in ('requires_approval', 'requires_action'):
                access_token = creds.get('access_token', '')
                account_id = creds.get('account_id', '')
                if access_token and account_id:
                    _add_event(order_id, 'approving', '正在调用 ChatGPT 确认付款…')
                    try:
                        approve_sess = payment_session(proxy_url)
                        resp = trace.http_call('payment_approve', approve_sess.post,
                            'https://chatgpt.com/backend-api/payments/checkout/approve',
                            json={'checkout_session_id': cs_id, 'processor_entity': 'openai_llc'},
                            headers={
                                'Authorization': f'Bearer {access_token}',
                                'Chatgpt-Account-Id': account_id,
                                'Content-Type': 'application/json',
                                'Accept': 'application/json',
                                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36',
                                'Origin': 'https://chatgpt.com',
                                'Referer': 'https://chatgpt.com/',
                            },
                            timeout=30,
                        )
                        approve_body = resp.json() if resp.headers.get('content-type', '').startswith('application/json') else {}
                        result_val = str(approve_body.get('result') or approve_body.get('status') or approve_body.get('state') or '').lower()
                        if result_val == 'blocked':
                            raise RuntimeError('approval_blocked')
                    except RuntimeError:
                        raise
                    except Exception as ae:
                        log.warning('[%s] approve error: %s', order_id, trace.error_code(ae))
                    finally:
                        if 'approve_sess' in locals():
                            approve_sess.close()

            poll_data = None
            for _ in range(5):
                time.sleep(1.5)
                try:
                    poll = trace.http_call('payment_read', stripe.session.get,
                        f'https://api.stripe.com/v1/payment_pages/{cs_id}',
                        params={'key': pk, 'eid': 'NA'},
                        timeout=30,
                    )
                    if poll.status_code == 200:
                        poll_data = poll.json()
                        paid, decline_code, charged_minor = _classify(poll_data)
                        if paid:
                            break
                        if decline_code:
                            raise RuntimeError(f'payment_declined:{decline_code}')
                except RuntimeError:
                    raise
                except Exception:
                    pass

        if not paid:
            raise RuntimeError('payment_result_unconfirmed')

        actual_charged = charged_minor if charged_minor > 0 else expected_amount
        _succeed_order(order_id, user_id, fee, last4, actual_charged, currency_from_stripe)

        # 4. Go 付款后续（独立副本），任何异常不触发付款退款
        try:
            cslive_go_post_payment.finish_payment(
                order_id,
                plan=plan_canonical,
                proxy_url=proxy_url,
                request_context=request_context,
                responses=(locals().get('poll_data'), confirm_resp, init_resp, link_result),
            )
        except Exception as pe:
            log.warning('[%s] post-payment setup error: %s', order_id, trace.error_code(pe))
            try:
                db.execute("UPDATE orders SET activation=CASE WHEN activation='active' THEN activation ELSE 'review' END,"
                           "renewal=CASE WHEN renewal IN ('disabled','kept') THEN renewal ELSE 'review' END,"
                           "updated_at=now() WHERE id=$1 AND status='succeeded'", order_id)
                trace.record('complete', 'review', trace.error_code(pe), order_id=order_id)
            except Exception:
                log.warning('[%s] post-payment review marker unavailable', order_id)

    except ProviderError as e:
        if subscription_retry.handle_failure(order_id, e):
            return
        msg = str(e)
        if msg in ('invalid_session', 'account_mismatch'):
            _fail_order(order_id, user_id, fee, '账号授权失效，办理未完成。', trace.error_code(e))
        else:
            code = trace.error_code(e)
            _fail_order(order_id, user_id, fee, trace.CODES[code], code)
    except Exception as e:
        if subscription_retry.handle_failure(order_id, e):
            return
        code = trace.error_code(e)
        log.error('[%s] subscription error: %s', order_id, code)
        _fail_order(order_id, user_id, fee, trace.CODES[code], code)
    finally:
        if 'stripe' in locals():
            stripe.session.close()


@router.get('/subscriptions/{id}')
def subscription_detail(id: str, request: Request):
    user = current(request)
    row = db.one(
        "SELECT o.id,o.status,o.product_name,o.email,o.country,o.fee,o.renewal,o.activation,o.created_at,o.updated_at,"
        "s.payment_last4,s.payment_amount,s.payment_currency,s.paid_at,s.checkout_session_id"
        " FROM orders o JOIN subscription_runs s ON s.order_id=o.id"
        " WHERE o.id=$1 AND o.user_id=$2 AND o.workflow='subscription' AND s.subscription_flow=$3",
        id, user['id'], FLOW,
    )
    require(row, '订单不存在', 404)
    events = db.all('SELECT stage,message,created_at FROM order_events WHERE order_id=$1 ORDER BY id', id)
    payment = {}
    if row.get('payment_last4'):
        payment = {
            'state': 'succeeded' if row['status'] == 'succeeded' else 'processing',
            'last4': row['payment_last4'],
            'charged_amount': row['payment_amount'],
            'charged_currency': row['payment_currency'],
            'paid_at': row['paid_at'],
        }
    return {'order': row, 'events': events, 'payment': payment,
            'checkout': {'checkout_session_id': row.get('checkout_session_id')}}
