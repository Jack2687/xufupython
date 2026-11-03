"""cslive_full_go 流程专属：直接调用 cs_live 的 Go 程序完成提链/付款/付款后续一体流程。

- link 工作流 → cs_live create-checkout（只生成结账链接，不付款）
- subscription 工作流 → cs_live pay（提链+付款+生效核实+关续费，同一浏览器会话）

卡片经 PTY（/dev/tty）喂入；cs_live stderr 的结构化事件流逐行解析成 payment_trace。
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

from . import payment_trace as trace

log = logging.getLogger('xufu.cslive_full_checkout')
ROOT = Path(__file__).resolve().parents[1]

PLAN_MAP = {
    'pro5x': 'pro5x', 'pro': 'pro20x', 'pro20x': 'pro20x',
    'team': 'team', 'team_premium': 'team5x', 'team5x': 'team5x',
    'plus': 'plus', 'go': 'go', 'promax': 'promax',
}


class CheckoutError(Exception):
    def __init__(self, code: str, message: str = ''):
        super().__init__(message or code)
        self.code = code


def binary_path() -> Path:
    name = 'cs_live.exe' if os.name == 'nt' else 'cs_live'
    return ROOT / 'cs-live-source' / name


def _work_root() -> Path:
    # Container rootfs is read-only; /tmp is a writable tmpfs. cs_live writes its
    # checkpoint key + file under the working directory, so it must be writable.
    base = Path('/tmp/cslive_full') if os.name != 'nt' else ROOT / '.local' / 'cslive_full'
    base.mkdir(parents=True, exist_ok=True)
    return base


def normalize_plan(product_id: str) -> str:
    key = (product_id or '').strip().lower()
    return PLAN_MAP.get(key, key)


def _session_json(session: dict[str, Any]) -> str:
    cookies = session.get('cookies')
    if isinstance(cookies, dict):
        cookie_str = '; '.join(f'{k}={v}' for k, v in cookies.items())
        vals = [v for v in cookies.values() if v]
    else:
        cookie_str = str(cookies or '')
        vals = []
    stok = str(session.get('session_token') or session.get('sessionToken') or '').strip() or (vals[0] if vals else '')
    payload = {
        'accessToken': session.get('access_token') or session.get('accessToken') or '',
        'sessionToken': stok,
        'account': {'id': session.get('account_id') or ''},
        'user': {'email': session.get('email') or ''},
        'cookies': cookie_str,
    }
    return json.dumps(payload, ensure_ascii=False)


def _billing_json(billing: dict[str, Any]) -> str:
    # cs_live cli_ReadBilling requires exactly these 6 keys (no email).
    name = f"{billing.get('first_name', '').strip()} {billing.get('last_name', '').strip()}".strip()
    payload = {
        'name': name,
        'line1': str(billing.get('line1') or '').strip(),
        'city': str(billing.get('city') or '').strip(),
        'state': str(billing.get('state') or '').strip(),
        'postal_code': str(billing.get('postal_code') or '').strip(),
        'country': str(billing.get('country') or 'US').strip(),
    }
    return json.dumps(payload, ensure_ascii=False)


def _parse_result(stdout: str) -> dict[str, Any]:
    text = (stdout or '').strip()
    if not text:
        raise CheckoutError('invalid_response', 'cs_live 无输出')
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith('{'):
            try:
                return json.loads(line)
            except (ValueError, TypeError):
                continue
    raise CheckoutError('invalid_response', 'cs_live 返回非 JSON')


# ---- event stream (stderr) → payment_trace ----
_PAIR = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|[^\s]+)')


def _parse_fields(line: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in _PAIR.finditer(line):
        k, v = m.group(1), m.group(2)
        if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
            v = v[1:-1]
        out[k] = v
    return out


def _map_phase(path: str, method: str) -> str:
    p = path or ''
    if '/sentinel' in p:
        return 'network'
    if 'payment_pages/' in p:
        if p.endswith('/init'):
            return 'opening'
        if p.endswith('/confirm'):
            return 'confirming'
        if '/payment_methods' in p:
            return 'card_prepare'
        return 'pricing' if method == 'POST' else 'checking'
    if '/payment_methods' in p:
        return 'card_prepare'
    if '/payment_intents/' in p:
        return 'checking'
    if '/payments/checkout/taxes' in p:
        return 'pricing'
    if '/payments/checkout/confirm' in p or '/payments/checkout/approve' in p:
        return 'confirming'
    if '/payments/checkout' in p:
        return 'checkout'
    if '/api/auth/session' in p:
        return 'session_refresh'
    if '/subscriptions/cancel' in p:
        return 'renewal'
    if '/subscriptions' in p:
        return 'activation'
    return ''


def _record_event(order_id: str, round, fields: dict[str, str]) -> None:
    event = fields.get('event')
    if event != 'http_request':
        return
    phase = _map_phase(fields.get('path', ''), fields.get('method', ''))
    if not phase:
        return
    code = (fields.get('errorCode') or fields.get('responseErrorCode') or '').strip()
    try:
        status = int(float(fields.get('status') or 0))
    except (TypeError, ValueError):
        status = 0
    try:
        ms = int(float(fields.get('durationMs') or 0))
    except (TypeError, ValueError):
        ms = 0
    failed = bool(code) or status >= 400
    result_code = trace.error_code(CheckoutError(code)) if code else 'response_received'
    rid = fields.get('responseRequestId') or fields.get('requestId') or ''
    trace.record(phase, 'failed' if failed else 'completed', result_code, order_id=order_id,
                 round=round if isinstance(round, int) else None, http_status=status, elapsed_ms=ms,
                 request_id=rid, diagnostics={'request_kind': fields.get('operation', '')})


def _reader(stream, on_line: Callable[[str], None]) -> None:
    try:
        for raw in iter(stream.readline, b''):
            try:
                on_line(raw.decode('utf-8', 'replace').rstrip('\r\n'))
            except Exception:
                pass
    except Exception:
        pass
    finally:
        try:
            stream.close()
        except Exception:
            pass


def create_checkout(*, session, country, plan, proxy_url, checkout_type='hosted',
                    timezone='Asia/Shanghai', timeout_sec=180) -> dict[str, Any]:
    """link 工作流：只生成结账链接，不付款。"""
    executable = binary_path()
    if not executable.is_file():
        raise CheckoutError('go_followup_unavailable', 'cs-live 提链程序未就绪')
    canonical = normalize_plan(plan)
    session_json = _session_json(session)
    with trace.request('checkout_create') as diagnostic:
        work = _work_root()
        args = [str(executable), 'create-checkout', '--country', country.upper(), '--plan', canonical,
                '--proxy', proxy_url, '--checkout-type', checkout_type]
        if timezone:
            args += ['--timezone', timezone]
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        try:
            completed = subprocess.run(args, input=session_json.encode('utf-8'), stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, cwd=str(work), timeout=timeout_sec, creationflags=flags)
        except subprocess.TimeoutExpired as exc:
            diagnostic.response(0)
            raise CheckoutError('timeout', '提链超时') from exc
        except OSError as exc:
            diagnostic.response(0)
            raise CheckoutError('connection', '提链进程无法启动') from exc
        parsed = _parse_result(completed.stdout.decode('utf-8', 'replace'))
        err = str(parsed.get('error_code') or parsed.get('error') or '').strip()
        url = str(parsed.get('checkout_url') or '').strip()
        cs = str(parsed.get('checkout_id') or '').strip()
        if completed.returncode != 0 or err or not url or not cs:
            diagnostic.response(0)
            raise CheckoutError(err or 'checkout_failed', err or '提链失败')
        diagnostic.response(200)
        return {
            'ok': True, 'payable': True, 'hostedUrl': url, 'checkoutSessionId': cs,
            'processor_entity': parsed.get('processor_entity') or 'openai_llc',
            'price': parsed.get('price'), 'billingCountry': country.upper(),
            'billingCurrency': '', 'is_hosted': checkout_type == 'hosted',
            'flow': parsed.get('flow') or 'cslive_full_go', 'request_context': {}, 'raw': parsed,
        }


def pay(order_id: str, *, session, card, billing, country, plan, proxy_url,
        checkout_type='hosted', cancel_renewal=True, expected_amount=None,
        trace_round=None, timeout_sec=300) -> dict[str, Any]:
    """subscription 工作流：cs_live pay 一体完成提链+付款+付款后续。卡经 PTY 喂入。"""
    if os.name == 'nt':
        raise CheckoutError('go_local_proxy_unsupported', 'cs_live pay 需在 Linux 容器运行')
    import fcntl  # noqa: F401
    import pty
    import select

    executable = binary_path()
    if not executable.is_file():
        raise CheckoutError('go_followup_unavailable', 'cs-live 付款程序未就绪')
    canonical = normalize_plan(plan)
    work = _work_root() / order_id
    (work / 'data').mkdir(parents=True, exist_ok=True)
    session_path = work / 'session.json'
    billing_path = work / 'billing.json'
    checkpoint_path = work / 'payment.enc'
    session_path.write_text(_session_json(session), encoding='utf-8')
    billing_path.write_text(_billing_json(billing), encoding='utf-8')

    args = [str(executable), 'pay', '--country', country.upper(), '--plan', canonical,
            '--session', str(session_path), '--billing', str(billing_path),
            '--checkout-type', checkout_type, '--renewal', 'cancel' if cancel_renewal else 'keep',
            '--proxy', proxy_url, '--checkpoint', str(checkpoint_path)]
    if expected_amount is not None:
        args += ['--expected-amount', str(int(expected_amount))]

    mm = str(card['month']).zfill(2)
    yy = str(card['year'])[-2:]
    card_line = f"{card['number']}----{mm}/{yy}----{card['cvc']}\n"

    master, slave = pty.openpty()
    slave_name = os.ttyname(slave)

    def _preexec():
        os.setsid()
        fd = os.open(slave_name, os.O_RDWR)
        os.close(fd)

    stdout_buf: list[str] = []
    try:
        proc = subprocess.Popen(args, stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                cwd=str(work), preexec_fn=_preexec, close_fds=True)
    except OSError as exc:
        os.close(master); os.close(slave)
        raise CheckoutError('connection', '付款进程无法启动') from exc
    os.close(slave)

    t_err = threading.Thread(target=_reader, args=(proc.stderr, lambda ln: _on_err(order_id, trace_round, ln)), daemon=True)
    t_out = threading.Thread(target=_reader, args=(proc.stdout, lambda ln: stdout_buf.append(ln)), daemon=True)
    t_err.start()
    t_out.start()

    card_sent = False
    tty_buf = b''
    deadline = __import__('time').monotonic() + timeout_sec
    try:
        while True:
            if proc.poll() is not None:
                break
            remaining = deadline - __import__('time').monotonic()
            if remaining <= 0:
                proc.terminate()
                raise CheckoutError('deadline_exceeded', '付款超时')
            try:
                r, _, _ = select.select([master], [], [], min(1.0, remaining))
            except (OSError, ValueError):
                break
            if master in r:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    chunk = b''
                if not chunk:
                    continue
                tty_buf += chunk
                if not card_sent and ('卡片'.encode() in tty_buf or b'----' in tty_buf or b'CVV' in tty_buf):
                    try:
                        os.write(master, card_line.encode('utf-8'))
                    except OSError:
                        pass
                    card_sent = True
        # drain remaining / wait
        try:
            proc.wait(timeout=max(1, int(deadline - __import__('time').monotonic())))
        except subprocess.TimeoutExpired:
            proc.terminate()
    finally:
        try:
            os.close(master)
        except OSError:
            pass
        t_out.join(timeout=3)
        t_err.join(timeout=3)

    result = _parse_result('\n'.join(stdout_buf))
    # read checkpoint + local key for durable recovery (best effort)
    recovery = {}
    try:
        cp_file = result.get('checkpoint_file') or str(checkpoint_path)
        key_file = work / 'data' / '.checkpoint-key'
        import base64
        if Path(cp_file).is_file():
            recovery['checkpoint_b64'] = base64.b64encode(Path(cp_file).read_bytes()).decode()
        if key_file.is_file():
            recovery['key_b64'] = base64.b64encode(key_file.read_bytes()).decode()
    except Exception:
        pass
    result['_recovery'] = recovery
    result['_returncode'] = proc.returncode
    return result


def _on_err(order_id, trace_round, line: str) -> None:
    if 'event=' not in line or 'protocol' not in line:
        return
    try:
        _record_event(order_id, trace_round, _parse_fields(line))
    except Exception:
        pass
