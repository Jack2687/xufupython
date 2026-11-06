"""Scheduled public subscription price references; never creates checkout sessions."""
from datetime import datetime, timezone
import logging
import math
import re
import time

import httpx
from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool
from . import db, regions, features
from .common import administrator, member, current, audit, body, integer, require

log = logging.getLogger('xufu')
CONFIG_KEY = 'regional_price_schedule'
PRICE_KEY = 'regional_price_cache'
FX_KEY = 'regional_fx_cache'
LOCK = 1886070
DEFAULTS = {'prices_enabled': True, 'price_interval_hours': 1, 'fx_enabled': True, 'fx_interval_days': 1}
PRICE_ORIGIN = 'https://chatgpt.com/backend-anon/checkout_pricing_config/configs/'
FX_URL = 'https://open.er-api.com/v6/latest/CNY'
PLAN_KEYS = {'plus': 'plus', 'pro5x': 'prolite', 'pro20x': 'pro', 'pro50x': 'promax',
             'team': 'business', 'team_premium': 'business_prolite'}
ERRORS = {'access_denied': '价格来源暂时拒绝访问', 'rate_limited': '价格来源暂时限流',
          'unavailable': '价格来源暂时不可用', 'invalid_response': '价格来源返回格式异常',
          'not_found': '暂未取得该地区的价格配置', 'network_error': '连接价格来源失败'}
router = APIRouter(prefix='/api')
admin_router = APIRouter(prefix='/api/admin/region-prices', dependencies=[Depends(administrator)])


def clock():
    return time.time()


class SourceError(Exception):
    pass


def read(key, q=None):
    return (q or db).scalar('SELECT value FROM settings WHERE key=$1', key, default={}) or {}


def write(key, value, q=None):
    (q or db).execute('INSERT INTO settings(key,value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value', key, value)


def settings(q=None):
    return {**DEFAULTS, **read(CONFIG_KEY, q)}


def stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value else None


def number(value):
    return type(value) in (float, int) and math.isfinite(value) and 0 < value < 1_000_000_000


def fetch_json(url):
    # Fixed public origins only. No customer credentials, payment session, or order proxy.
    try:
        with httpx.Client(timeout=httpx.Timeout(12, connect=6), follow_redirects=False) as client:
            with client.stream('GET', url, headers={'Accept': 'application/json'}) as response:
                if response.status_code != 200:
                    raise SourceError({403: 'access_denied', 429: 'rate_limited', 404: 'not_found'}.get(response.status_code, 'unavailable'))
                chunks = bytearray()
                started = time.monotonic()
                for chunk in response.iter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > 262144 or time.monotonic() - started > 15:
                        raise SourceError('invalid_response')
                import json
                return json.loads(chunks)
    except SourceError:
        raise
    except httpx.HTTPError:
        raise SourceError('network_error') from None
    except (ValueError, TypeError):
        raise SourceError('invalid_response') from None


def parse_prices(data, country):
    if not isinstance(data, dict) or data.get('country_code') != country:
        raise SourceError('invalid_response')
    cfg = data.get('currency_config')
    if not isinstance(cfg, dict) or not re.fullmatch('[A-Z]{3}', str(cfg.get('symbol_code', ''))):
        raise SourceError('invalid_response')
    result = {}
    for plan, source_key in PLAN_KEYS.items():
        product = cfg.get(source_key)
        if product is None:
            continue
        if not isinstance(product, dict):
            raise SourceError('invalid_response')
        month = product.get('month')
        if month is None:
            continue
        if not isinstance(month, dict) or not number(month.get('amount')) or month.get('tax') not in ('inclusive', 'exclusive'):
            raise SourceError('invalid_response')
        # Values are already major currency units, including JPY. Never divide by 100.
        original = {'amount': month['amount'], 'tax': month['tax']}
        amount, tax = original['amount'], original['tax']
        override = month.get('psp_override')
        if override is not None:
            if not isinstance(override, dict) or not number(override.get('amount')) or override.get('tax') not in ('inclusive', 'exclusive'):
                raise SourceError('invalid_response')
            if override['tax'] == 'exclusive':
                amount, tax = override['amount'], override['tax']
        result[plan] = {'amount': amount, 'currency': cfg['symbol_code'], 'tax': tax,
                        'website_amount': original['amount'], 'website_tax': original['tax'],
                        'period': 'month', 'per_seat': plan.startswith('team')}
    return result


def parse_fx(data, moment):
    if not isinstance(data, dict) or data.get('result') != 'success' or data.get('base_code') != 'CNY':
        raise SourceError('invalid_response')
    rates = data.get('rates')
    updated = data.get('time_last_update_unix')
    if not isinstance(rates, dict) or type(updated) not in (int, float) or not math.isfinite(updated) or updated > moment + 300 or moment - updated > 7 * 86400:
        raise SourceError('invalid_response')
    if rates.get('CNY') != 1 or not number(rates.get('USD')):
        raise SourceError('invalid_response')
    clean = {code: 1 / rate for code, rate in rates.items()
             if isinstance(code, str) and re.fullmatch('[A-Z]{3}', code) and number(rate)}
    return {'rates': clean, 'source_updated_at': updated}


def due(entry, seconds, requested, moment):
    attempt = entry.get('attempted_at', 0)
    return not attempt or attempt < requested or attempt + seconds <= moment


def enabled_or_requested(cfg, kind, entry):
    return cfg[kind + '_enabled'] or entry.get('attempted_at', 0) < cfg.get(kind + '_requested_at', 0)


def refresh_tick(stop=None):
    """One bounded batch. Cross-process advisory lock prevents duplicate source requests."""
    if stop is not None and stop.is_set():
        return
    moment = clock()
    with db.transaction() as q:
        if not q.scalar('SELECT pg_try_advisory_xact_lock($1)', LOCK):
            return
        cfg = settings(q)
        fx = read(FX_KEY, q)
        if enabled_or_requested(cfg, 'fx', fx) and due(fx, cfg['fx_interval_days'] * 86400, cfg.get('fx_requested_at', 0), moment):
            try:
                fx.update(parse_fx(fetch_json(FX_URL), moment), fetched_at=moment, error='')
            except SourceError as exc:
                fx['error'] = str(exc)
            fx['attempted_at'] = moment
            write(FX_KEY, fx, q)
            return
        cache = read(PRICE_KEY, q)
        entries = cache.get('entries', {})
        original_entries = entries
        listed = regions.items(q)
        codes = {r['code'] for r in listed}
        entries = {code: entry for code, entry in entries.items() if code in codes}
        cache['entries'] = entries
        if cache.get('cooldown_until', 0) > moment:
            if entries != original_entries:
                write(PRICE_KEY, cache, q)
            return
        candidates = [r['code'] for r in listed
                      if enabled_or_requested(cfg, 'prices', entries.get(r['code'], {}))
                      and due(entries.get(r['code'], {}), cfg['price_interval_hours'] * 3600,
                              cfg.get('prices_requested_at', 0), moment)]
        for code in candidates[:2]:
            if stop is not None and stop.is_set():
                break
            entry = dict(entries.get(code, {}))
            checked = clock()
            try:
                quotes = parse_prices(fetch_json(PRICE_ORIGIN + code), code)
                entry.update(quotes=quotes, fetched_at=checked, error='')
            except SourceError as exc:
                entry['error'] = str(exc)
                if str(exc) in ('access_denied', 'rate_limited'):
                    cache['cooldown_until'] = checked + 900
            entry['attempted_at'] = checked
            entries[code] = entry
            if cache.get('cooldown_until', 0) > checked:
                break
        # An administrator may delete a country while a network request is in flight.
        current_codes = {r['code'] for r in regions.items(q)}
        cache['entries'] = {code: value for code, value in entries.items() if code in current_codes}
        if cache['entries'] != original_entries:
            write(PRICE_KEY, cache, q)


def stats(cfg, cache, fx, listed, moment):
    entries = cache.get('entries', {})
    attempts = [entries.get(r['code'], {}).get('attempted_at', 0) for r in listed]
    successes = [entries.get(r['code'], {}).get('fetched_at', 0) for r in listed]
    pending = sum(due(entries.get(r['code'], {}), cfg['price_interval_hours'] * 3600,
                      cfg.get('prices_requested_at', 0), moment) for r in listed)
    next_price = min((max(moment, v + cfg['price_interval_hours'] * 3600)
                      if v >= cfg.get('prices_requested_at', 0) and v else moment for v in attempts), default=0)
    next_price = max(next_price, cache.get('cooldown_until', 0)) if next_price else 0
    next_fx = max(moment, fx.get('attempted_at', 0) + cfg['fx_interval_days'] * 86400)
    if fx.get('attempted_at', 0) < cfg.get('fx_requested_at', 0):
        next_fx = moment
    errors = [{'country': r['code'], 'name': r['name'],
               'message': ERRORS.get(entries[r['code']].get('error'), '查询未完成')}
              for r in listed if entries.get(r['code'], {}).get('error')]
    return {'countries': len(listed), 'quoted_countries': sum(bool(entries.get(r['code'], {}).get('quotes')) for r in listed),
            'pending': pending, 'last_price_attempt_at': stamp(max(attempts, default=0)),
            'last_price_success_at': stamp(max(successes, default=0)),
            'next_price_at': stamp(next_price) if cfg['prices_enabled'] else None,
            'next_fx_at': stamp(next_fx) if cfg['fx_enabled'] else None,
            'last_fx_attempt_at': stamp(fx.get('attempted_at')),
            'fx_updated_at': stamp(fx.get('source_updated_at')),
            'fx_error': ERRORS.get(fx.get('error'), '') if fx.get('error') else '',
            'errors': errors, 'cooldown_until': stamp(cache.get('cooldown_until', 0))}


@admin_router.get('')
def admin_status():
    cfg = settings()
    return {'config': {key: cfg[key] for key in DEFAULTS},
            'status': stats(cfg, read(PRICE_KEY), read(FX_KEY), regions.items(), clock())}


@admin_router.put('')
async def save_schedule(request: Request):
    value = await body(request)
    require(isinstance(value, dict) and set(value) == set(DEFAULTS), '定时配置字段无效')
    require(type(value['prices_enabled']) is bool and type(value['fx_enabled']) is bool, '请使用有效的定时开关')
    integer(value['price_interval_hours'], 1, 168, '价格更新周期为 1～168 小时')
    integer(value['fx_interval_days'], 1, 30, '汇率更新周期为 1～30 天')
    def save():
        with db.transaction() as q:
            q.execute('SELECT pg_advisory_xact_lock($1)', LOCK + 1)
            write(CONFIG_KEY, {**settings(q), **value}, q)
            audit(q, request, 'region_prices.schedule', 'regions', '更新地区价格及汇率定时配置')
        return admin_status()
    return await run_in_threadpool(save)


@admin_router.post('/refresh')
async def request_refresh(request: Request):
    value = await body(request)
    require(value.get('kind') in ('prices', 'fx'), '请选择价格或汇率')
    def schedule():
        with db.transaction() as q:
            q.execute('SELECT pg_advisory_xact_lock($1)', LOCK + 1)
            cfg = settings(q)
            key = value['kind'] + '_requested_at'
            # Coalesce repeated manual requests; do not restart a batch already queued.
            require(clock() - cfg.get(key, 0) >= 60, '更新已安排，请稍后刷新查看', 429)
            cfg[key] = clock()
            write(CONFIG_KEY, cfg, q)
            audit(q, request, 'region_prices.refresh', value['kind'], '安排地区参考价数据更新')
        return {'queued': True}
    return await run_in_threadpool(schedule)


@router.get('/subscription-prices', dependencies=[Depends(member)])
def customer_prices(request: Request):
    features.require_any(current(request), 'subscribe', 'links')
    cfg, cache, fx = settings(), read(PRICE_KEY), read(FX_KEY)
    moment, rows = clock(), []
    fx_stale = bool(fx.get('error')) or not fx.get('fetched_at') or moment - fx['fetched_at'] >= cfg['fx_interval_days'] * 86400
    for region in regions.items():
        entry = cache.get('entries', {}).get(region['code'], {})
        for plan, price in entry.get('quotes', {}).items():
            rate = fx.get('rates', {}).get(price['currency'])
            rows.append({**price, 'plan': plan, 'country': region['code'], 'name': region['name'],
                         'configured_currency': region['currency'],
                         'rate_to_cny': rate, 'amount_cny': round(price['amount'] * rate, 4) if rate else None,
                         'website_amount_cny': round(price['website_amount'] * rate, 4) if rate else None,
                         'updated_at': stamp(entry.get('fetched_at')),
                         'stale': bool(entry.get('error')) or moment - entry.get('fetched_at', 0) >= cfg['price_interval_hours'] * 3600})
    return {'rows': rows, 'fx_updated_at': stamp(fx.get('source_updated_at')), 'fx_stale': fx_stale,
            'scheduled': cfg['prices_enabled'], 'price_interval_hours': cfg['price_interval_hours']}

