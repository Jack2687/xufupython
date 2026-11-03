"""cslive_direct_pay checkout: calls the cs_live.go binary via subprocess.

The cslive flow uses the compiled cs_live binary from cs-live-source/ to perform
the checkout session creation. All other stages (proxy, billing, HTTP payment,
post-payment) are handled by Python code copied from the gener_direct_pay flow.

Binary path: cs-live-source/cs_live.exe (Windows) or cs-live-source/cs_live (Unix).
Build command (manual, PowerShell):
    cd cs-live-source
    go mod download
    go build -o cs_live.exe .
"""
from __future__ import annotations
import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger('xufu.cslive_checkout')

# Plan ID mapping: xufu product_id → cs_live --plan value
PLAN_MAP = {
    'pro5x': 'pro5x',
    'pro': 'pro20x',
    'pro20x': 'pro20x',
    'team': 'team',
    'team_premium': 'team5x',
    'plus': 'plus',
    'go': 'go',
    'promax': 'promax',
}


def _binary_path() -> Path:
    root = Path(__file__).resolve().parents[1]
    name = 'cs_live.exe' if os.name == 'nt' else 'cs_live'
    return root / 'cs-live-source' / name


def binary_available() -> bool:
    return _binary_path().exists()


def _map_plan(product_id: str) -> str:
    key = (product_id or '').strip().lower()
    if key in PLAN_MAP:
        return PLAN_MAP[key]
    # Pass through unknown plan ids; cs_live will reject with error_code
    return key


def create_checkout(
    *,
    session: dict,
    country: str,
    plan: str,
    proxy_url: str,
    seats: int = 0,
    seats_5x: int = 0,
    interval: str = '',
    checkout_type: str = 'hosted',
    currency: str = '',
    timezone: str = '',
    browser_profile: str = 'random',
    with_price: bool = False,
    timeout_sec: float = 45,
    deadline_sec: float = 240,
    debug: bool = False,
) -> dict:
    """Invoke cs_live binary to create a Checkout and return a normalized result.

    Returns a dict with keys compatible with generator.generate_pro_long_link():
        - ok: bool
        - payable: bool
        - hostedUrl: str (checkout URL)
        - checkoutSessionId: str (cs_live_...)
        - processor_entity: str
        - price: dict | None
        - billingCountry: str
        - billingCurrency: str
        - error: str (when ok=False)
    """
    binary = _binary_path()
    if not binary.exists():
        return {
            'ok': False,
            'error': f'cs_live binary not found at {binary}. Run `go build -o cs_live.exe` under cs-live-source/.',
        }

    plan_value = _map_plan(plan)

    # Pass session via a temp file so it does not leak into process args.
    with tempfile.NamedTemporaryFile(
        'w', suffix='.json', delete=False, encoding='utf-8', dir=str(binary.parent)
    ) as f:
        session_path = f.name
        json.dump(session, f, ensure_ascii=False)

    argv: list[str] = [
        str(binary), 'create-checkout',
        '--country', (country or '').upper(),
        '--plan', plan_value,
        '--session', session_path,
        '--checkout-type', checkout_type or 'hosted',
        '--timeout', str(timeout_sec),
        '--deadline', str(deadline_sec),
    ]
    if proxy_url:
        argv += ['--proxy', proxy_url]
    if seats and seats > 0:
        argv += ['--seats', str(seats)]
    if seats_5x and seats_5x > 0:
        argv += ['--seats-5x', str(seats_5x)]
    if interval:
        argv += ['--interval', interval]
    if currency:
        argv += ['--currency', currency]
    if timezone:
        argv += ['--timezone', timezone]
    if browser_profile:
        argv += ['--browser-profile', browser_profile]
    if with_price:
        argv += ['--with-price']
    if debug:
        argv += ['--debug']

    try:
        proc = subprocess.run(
            argv,
            cwd=str(binary.parent),
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=max(deadline_sec + 30, 120),
            check=False,
        )
    except subprocess.TimeoutExpired:
        _unlink_quiet(session_path)
        return {'ok': False, 'error': 'cs_live binary timed out'}
    except FileNotFoundError:
        _unlink_quiet(session_path)
        return {'ok': False, 'error': 'cs_live binary could not be executed'}
    finally:
        _unlink_quiet(session_path)

    stdout = (proc.stdout or '').strip()
    stderr_tail = (proc.stderr or '').strip()[-400:]

    data: dict | None = None
    if stdout:
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            # cs_live may print banners; take the last JSON object line.
            for line in reversed(stdout.splitlines()):
                line = line.strip()
                if line.startswith('{') and line.endswith('}'):
                    try:
                        data = json.loads(line)
                        break
                    except json.JSONDecodeError:
                        continue

    if not isinstance(data, dict):
        return {
            'ok': False,
            'error': f'invalid cs_live output (exit={proc.returncode}): {stderr_tail or stdout[:200]}',
        }

    err_code = str(data.get('error_code') or '').strip()
    checkout_id = str(data.get('checkout_id') or '')
    checkout_url = str(data.get('checkout_url') or '')

    if proc.returncode != 0 or err_code:
        return {
            'ok': False,
            'error': err_code or f'cs_live exit {proc.returncode}',
            'raw': data,
        }

    if not checkout_url or not checkout_id:
        return {'ok': False, 'error': 'cs_live returned empty checkout url', 'raw': data}

    # cs_live returns both hosted (cs_live_*) and oaics (oaics_*) sessions.
    is_hosted = checkout_id.startswith('cs_')
    return {
        'ok': True,
        'payable': True,
        'hostedUrl': checkout_url,
        'checkoutSessionId': checkout_id,
        'processor_entity': str(data.get('processor_entity') or ''),
        'checkout_url': checkout_url,
        'checkout_id': checkout_id,
        'flow': str(data.get('flow') or ''),
        'price': data.get('price'),
        'billingCountry': (country or '').upper(),
        'billingCurrency': (currency or '').upper(),
        'is_hosted': is_hosted,
        'raw': data,
    }


def _unlink_quiet(path: str):
    try:
        os.unlink(path)
    except OSError:
        pass
