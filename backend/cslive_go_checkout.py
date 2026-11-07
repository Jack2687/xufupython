"""cslive_go_direct_pay 流程专属：调用 cs-live Go 提链二进制生成结账链接。

- Go 二进制：cs-live-source/cs_live(.exe)
- 结果 JSON：ok, payable, hostedUrl, checkoutSessionId, processor_entity, price,
             billingCountry, billingCurrency, is_hosted, publishable_key, raw, request_context
- 代理 session：复用 subscription_cslive_go._acquire_order_proxy 结果，透传给 Go
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from . import payment_trace as trace
from .checkout_errors import CheckoutError
from .team_subscription import seat_counts, promo_code as validate_promo_code

log = logging.getLogger('xufu.cslive_go_checkout')
ROOT = Path(__file__).resolve().parents[1]

PLAN_MAP = {
    'pro5x': 'pro5x',
    'pro': 'pro20x',
    'pro20x': 'pro20x',
    'team': 'team',
    'team_premium': 'team5x',
    'team5x': 'team5x',
    'plus': 'plus',
    'go': 'go',
    'promax': 'promax',
}


def binary_path() -> Path:
    name = 'cs_live.exe' if os.name == 'nt' else 'cs_live'
    return ROOT / 'cs-live-source' / name


def _normalize_plan(product_id: str) -> str:
    key = (product_id or '').strip().lower()
    return PLAN_MAP.get(key, key)


def _parse_output(raw: str) -> dict[str, Any]:
    text = (raw or '').strip()
    if not text:
        raise CheckoutError('invalid_response', 'cs_live 无输出')
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        pass
    # Tolerate preamble: the result JSON is the last complete object on stdout.
    last_brace = text.rfind('{')
    while last_brace >= 0:
        try:
            return json.loads(text[last_brace:])
        except (ValueError, TypeError):
            last_brace = text.rfind('{', 0, last_brace)
    raise CheckoutError('invalid_response', 'cs_live 返回非 JSON')


def _build_session_json(session: dict[str, Any]) -> str:
    """Shape order credentials into the exact session JSON cs_live expects.

    cs_live model_ParseSession requires: accessToken (非空), sessionToken (非空),
    account.id, user.email(可选), cookies(字符串)。订单凭据用的是 access_token /
    account_id / session_token / cookies(dict)，必须逐项转换，否则 cs_live 秒退。
    """
    cookies = session.get('cookies')
    if isinstance(cookies, dict):
        cookie_str = '; '.join(f'{k}={v}' for k, v in cookies.items())
        cookie_values = [v for v in cookies.values() if v]
    else:
        cookie_str = str(cookies or '')
        cookie_values = []
    session_token = str(session.get('session_token') or session.get('sessionToken') or '').strip()
    if not session_token and cookie_values:
        # cs_live 要求 sessionToken 非空；没有独立 token 时回退用会话 Cookie 值。
        session_token = cookie_values[0]
    payload = {
        'accessToken': session.get('access_token') or session.get('accessToken') or '',
        'sessionToken': session_token,
        'account': {'id': session.get('account_id') or ''},
        'user': {'email': session.get('email') or ''},
        'cookies': cookie_str,
    }
    return json.dumps(payload, ensure_ascii=False)


def create_checkout(
    *,
    session: dict[str, Any],
    country: str,
    plan: str,
    proxy_url: str,
    seats: int = 1,
    seats_5x: int = 0,
    promo_code: str = '',
    interval: str = 'month',
    checkout_type: str = 'hosted',
    currency: str = '',
    timezone: str = 'Asia/Shanghai',
    browser_profile: str = '',
    with_price: bool = True,
    timeout_sec: int = 180,
    debug: bool = False,
) -> dict[str, Any]:
    """Run cs_live create-checkout and shape the result for subscription_cslive_go.run.

    session 传订单凭据（access_token / account_id / session_token / cookies / email）；
    本函数转成 cs_live 要求的格式，并经标准输入喂入（省略 --session），既不落盘凭据，
    也避开只读文件系统。
    """
    executable = binary_path()
    canonical = _normalize_plan(plan)
    is_team = canonical in ('team', 'team5x')
    promotion=validate_promo_code(promo_code,'team' if is_team else canonical)
    environment=None
    session_json = _build_session_json(session)
    with trace.request('checkout_create') as diagnostic:
        if not executable.is_file():
            raise CheckoutError('checkout_program_unavailable')
        args = [
            str(executable), 'create-checkout',
            '--country', country.upper(),
            '--plan', canonical,
            '--proxy', proxy_url,
            '--checkout-type', checkout_type,
        ]
        if is_team:
            ordinary, premium = seat_counts(seats, seats_5x)
            args += [
                '--seats', str(ordinary),
                '--seats-5x', str(premium),
                '--interval', interval if interval in ('month', 'year') else 'month',
            ]
        if promotion:
            environment=dict(os.environ,XUFU_TEAM_PROMO_CODE=promotion)
            args += ['--promo-code-env','XUFU_TEAM_PROMO_CODE']
        if currency:
            args += ['--currency', currency.upper()]
        if timezone:
            args += ['--timezone', timezone]
        if browser_profile:
            args += ['--browser-profile', browser_profile]
        if with_price:
            args.append('--with-price')
        if debug:
            args.append('--debug')
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        try:
            completed = subprocess.run(
                args,
                input=session_json.encode('utf-8'),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(executable.parent),
                timeout=timeout_sec,
                creationflags=flags,
                env=environment,
            )
        except subprocess.TimeoutExpired:
            raise CheckoutError('timeout') from None
        except OSError:
            raise CheckoutError('checkout_program_unavailable') from None
        stdout = completed.stdout.decode('utf-8', errors='replace')
        try:
            parsed = _parse_output(stdout)
        except CheckoutError as exc:
            raise CheckoutError(exc.code, exit_code=completed.returncode) from None
        if not isinstance(parsed, dict):
            raise CheckoutError('invalid_response', exit_code=completed.returncode)
        err = parsed.get('error_code') or parsed.get('error') or ''
        exit_code = completed.returncode
        hosted_url = str(parsed.get('checkout_url') or parsed.get('hostedUrl') or '').strip()
        cs_id = str(parsed.get('checkout_id') or parsed.get('checkoutSessionId') or '').strip()
        if exit_code != 0 or err or not hosted_url or not cs_id:
            raise CheckoutError(err or 'checkout_failed', exit_code=exit_code, diagnostics=parsed.get('diagnostics'))
        diagnostic.response(200)
        return {
            'ok': True,
            'payable': True,
            'hostedUrl': hosted_url,
            'checkoutSessionId': cs_id,
            'processor_entity': parsed.get('processor_entity') or 'openai_llc',
            'price': parsed.get('price'),
            'billingCountry': country.upper(),
            'billingCurrency': currency.upper() if currency else '',
            'is_hosted': checkout_type == 'hosted',
            'publishable_key': '',
            'flow': parsed.get('flow') or 'cslive_go_direct_pay',
            'request_context': {},
            'raw': parsed,
        }
