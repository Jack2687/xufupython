"""cslive_go_direct_pay 流程专属：付款后续（post_payment + go_post_payment 合并副本）。

与 gener_direct_pay 流程完全独立：
- 自己的 lock 编号（1886012）
- 自己的 FLOW 名（'cslive_go_direct_pay'）
- 共享 bin/post-payment.exe Go 二进制与 subscription_followups 表（按 flow 区分）
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from pathlib import Path
from queue import Queue, Empty
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
import base64
import json
import logging
import os
import re
import select
import socketserver
import ssl
import subprocess
import threading
import time

from . import db, payment_trace as trace
from .common import config, decrypt, encrypt, now
from .egress import connect_via_local, local_proxy
from .order_http_context import OrderHTTPContext
from .renewals import COOKIE_NAME

log = logging.getLogger('xufu.cslive_go_post_payment')
BASE = 'https://chatgpt.com'
SUBSCRIPTIONS = '/backend-api/subscriptions'
FLOW = 'cslive_go_direct_pay'
EXECUTOR = 'go_cslive_v1'
FOLLOWUP_LOCK = 1886012
ROOT = Path(__file__).resolve().parents[1]
LINE_LIMIT = 1048576

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


def binary_path():
    return ROOT / 'bin' / ('post-payment.exe' if os.name == 'nt' else 'post-payment')


class _ProxyRelay(socketserver.BaseRequestHandler):
    def handle(self):
        upstream = None
        try:
            remote = self.server.remote
            upstream = connect_via_local(remote.hostname, remote.port or (443 if remote.scheme == 'https' else 80), 15)
            if remote.scheme == 'https':
                upstream = upstream.start_tls(ssl.create_default_context(), remote.hostname, 15)
            remote_socket = upstream.get_extra_info('socket')
            self.request.settimeout(5)
            remote_socket.settimeout(5)
            with self.server.guard:
                self.server.sockets.update((self.request, remote_socket))
            while not self.server.stopping.is_set():
                ready, _, _ = select.select((self.request, remote_socket), (), (), 1)
                if isinstance(remote_socket, ssl.SSLSocket) and remote_socket.pending() and remote_socket not in ready:
                    ready.append(remote_socket)
                for stream in ready:
                    data = stream.recv(65536)
                    if not data:
                        return
                    target = remote_socket if stream is self.request else self.request
                    target.sendall(data)
        except Exception:
            pass
        finally:
            if upstream:
                with self.server.guard:
                    self.server.sockets.discard(self.request)
                    self.server.sockets.discard(upstream.get_extra_info('socket'))
                upstream.close()


class _RelayServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False


@contextmanager
def proxy_route(endpoint):
    if not local_proxy():
        yield endpoint
        return
    remote = urlsplit(endpoint)
    if remote.scheme not in ('http', 'https', 'socks5', 'socks5h'):
        raise FollowupError('go_local_proxy_unsupported')
    with _RelayServer(('127.0.0.1', 0), _ProxyRelay) as relay:
        relay.remote, relay.guard = remote, threading.Lock()
        relay.stopping, relay.sockets = threading.Event(), set()
        thread = threading.Thread(target=relay.serve_forever, daemon=True)
        thread.start()
        auth = remote.netloc.rsplit('@', 1)[0] + '@' if '@' in remote.netloc else ''
        scheme = 'http' if remote.scheme == 'https' else remote.scheme
        route = urlunsplit((scheme, auth + '127.0.0.1:' + str(relay.server_address[1]), '', '', ''))
        try:
            yield route
        finally:
            relay.stopping.set()
            relay.shutdown()
            with relay.guard:
                for sock in relay.sockets:
                    try:
                        sock.close()
                    except OSError:
                        pass


def apply_checkpoint(cp, value):
    if not isinstance(value, dict):
        raise FollowupError('checkpoint_mismatch')
    for field in ('account_id', 'checkout_id', 'plan', 'entity', 'cancel_requested'):
        if value.get(field) != cp.get(field):
            raise FollowupError('checkpoint_mismatch')
    if (value.get('payment') != 'charged'
            or value.get('phase') not in ('activation', 'cancellation', 'cancellation_requested', 'completed')
            or value.get('activation') not in ('pending', 'active')
            or value.get('renewal') not in ('unknown', 'on', 'off')
            or type(value.get('cancel_attempts')) is not int or not 0 <= value['cancel_attempts'] <= 2):
        raise FollowupError('checkpoint_mismatch')
    if value['phase'] == 'completed' and (value['activation'] != 'active' or cp['cancel_requested'] and value['renewal'] != 'off'):
        raise FollowupError('checkpoint_mismatch')
    if value['cancel_attempts'] < (cp.get('go_context') or {}).get('cancel_attempts', 0):
        raise FollowupError('checkpoint_mismatch')
    c = cp['credentials']
    token = value.get('access_token')
    if not isinstance(token, str) or not token:
        raise FollowupError('session_expired')
    if token != c.get('access_token'):
        c['refreshed_at'] = datetime.now(timezone.utc).isoformat()
        try:
            part = token.split('.')[1]
            claims = json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))
            c['token_expires_at'] = datetime.fromtimestamp(claims['exp'], timezone.utc).isoformat()
        except (ValueError, KeyError, IndexError, TypeError, OverflowError):
            pass
    cookies = SimpleCookie()
    try:
        cookies.load(value.get('cookies') or '')
    except Exception:
        raise FollowupError('checkpoint_mismatch') from None
    c.update(access_token=token, session_token=value.get('session_token') or '',
             cookies={k: v.value for k, v in cookies.items() if COOKIE_NAME.fullmatch(k)})
    cp.update(go_context=value, executor=EXECUTOR, phase=value['phase'],
              activation=value['activation'], renewal=value['renewal'])


def _event(message):
    fields = message.get('fields') or {}
    kind = fields.get('request_kind', '')
    phase = trace.REQUEST_PHASES.get(kind)
    if not phase:
        return
    event = message.get('event')
    diagnostics = {k: v for k, v in fields.items() if k.startswith('response_') or k in
                   ('cf_ray', 'retry_wait_ms', 'retry_wait_elapsed_ms', 'challenge_required',
                    'connection_ready_ms', 'request_write_ms', 'first_byte_ms',
                    'network_stage', 'network_stage_elapsed_ms')}
    for key in ('retry_wait_ms', 'retry_wait_elapsed_ms'):
        if type(diagnostics.get(key)) in (int, float):
            diagnostics[key] = round(diagnostics[key])
    diagnostics['request_kind'] = kind
    if type(fields.get('attempt')) is int:
        diagnostics['request_attempt'] = fields['attempt']
    if event == 'request_started':
        trace.record(phase, 'running', 'request_started', diagnostics=diagnostics)
    elif event == 'http_request':
        code = fields.get('error_code') or ''
        state = 'failed' if code else 'completed'
        diagnostics['retrying'] = fields.get('outcome') == 'retry'
        trace.record(phase, state, trace.error_code(FollowupError(code)) if code else 'response_received',
                     elapsed_ms=fields.get('duration_ms', 0), http_status=fields.get('status', 0),
                     request_id=fields.get('response_request_id', ''), diagnostics=diagnostics)
    elif event == 'http_retry_wait':
        trace.record(phase, 'failed' if fields.get('error_code') else 'completed',
                     'request_retry_wait', diagnostics=diagnostics)


def execute(cp, save, *, operation='finish'):
    """Only called after Python verifies durable payment evidence, or refresh-only diagnostics."""
    executable = binary_path()
    if not executable.is_file():
        raise FollowupError('go_followup_unavailable')
    if cp.get('executor') != EXECUTOR or operation not in ('finish', 'refresh'):
        raise FollowupError('checkpoint_mismatch')
    expires = datetime.fromisoformat(cp['proxy_expires_at'])
    remaining = (expires - datetime.now(timezone.utc)).total_seconds()
    if remaining <= 0:
        raise FollowupError('proxy_session_expired')
    budget = min(240, remaining) + 10
    with proxy_route(cp['proxy_endpoint']) as endpoint:
        payload = {'operation': operation, 'state': cp, 'proxy': endpoint,
                   'proxy_expires_at': cp['proxy_expires_at'],
                   'refresh_retries': int(cp.get('refresh_retries') or 0),
                   'refresh_interval_seconds': float(cp.get('refresh_interval_seconds') or 0)}
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        process = subprocess.Popen([str(executable)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, cwd=ROOT, creationflags=flags)
        messages = Queue()
        def read():
            try:
                while True:
                    line = process.stdout.readline(LINE_LIMIT + 1)
                    if len(line) > LINE_LIMIT:
                        messages.put(('invalid', None)); return
                    if not line:
                        messages.put(('eof', None)); return
                    messages.put(('line', line))
            except Exception:
                messages.put(('invalid', None))
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        deadline = time.monotonic() + budget
        try:
            process.stdin.write(json.dumps(payload, ensure_ascii=True).encode() + b'\n')
            process.stdin.flush()
            while True:
                try:
                    kind, raw = messages.get(timeout=max(0.01, deadline - time.monotonic()))
                except Empty:
                    raise FollowupError('deadline_exceeded') from None
                if kind != 'line':
                    raise FollowupError('go_followup_protocol_error')
                try:
                    message = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise FollowupError('go_followup_protocol_error') from None
                if not isinstance(message, dict):
                    raise FollowupError('go_followup_protocol_error')
                if message.get('type') == 'event':
                    _event(message)
                elif message.get('type') == 'checkpoint':
                    previous = deepcopy(cp)
                    try:
                        apply_checkpoint(cp, message.get('checkpoint'))
                        phase = message.get('phase') or ''
                        if phase and phase != cp['phase']:
                            raise FollowupError('checkpoint_mismatch')
                        save(phase)
                    except Exception:
                        cp.clear(); cp.update(previous)
                        raise
                    process.stdin.write(b'{"ok":true}\n'); process.stdin.flush()
                elif message.get('type') == 'result':
                    if message.get('checkpoint') is not None:
                        apply_checkpoint(cp, message['checkpoint'])
                    if not message.get('ok'):
                        code = message.get('error_code')
                        if not isinstance(code, str) or len(code) > 80:
                            code = 'go_followup_protocol_error'
                        raise FollowupError(code)
                    if operation == 'finish' and cp['phase'] != 'completed':
                        raise FollowupError('go_followup_protocol_error')
                    return
                else:
                    raise FollowupError('go_followup_protocol_error')
                if time.monotonic() >= deadline:
                    raise FollowupError('deadline_exceeded')
        except (OSError, subprocess.SubprocessError):
            raise FollowupError('go_followup_protocol_error') from None
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait(timeout=3)
            process.stdin.close()
            reader.join(timeout=2)
            process.stdout.close()


def _run(order_id, *, plan=None, responses=(), proxy_url=None, request_context=None, executor=EXECUTOR):
    with db.transaction() as lock:
        if not lock.scalar('SELECT pg_try_advisory_xact_lock($1,hashtext($2))', FOLLOWUP_LOCK, order_id):
            return {'state': 'busy', 'warning_code': 'followup_busy'}
        row = db.one('SELECT o.status,o.workflow,o.keep_renewal,s.paid_at,s.checkout_session_id,s.credentials_enc,s.trace_round,s.subscription_flow '
                     'FROM orders o JOIN subscription_runs s ON s.order_id=o.id WHERE o.id=$1', order_id)
        if not row or row['workflow'] != 'subscription' or row['status'] != 'succeeded' or not row['paid_at']:
            raise FollowupError('payment_evidence_missing')
        if executor != EXECUTOR or row['subscription_flow'] != FLOW:
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
        pcfg = config('payment')
        cp['refresh_retries'] = pcfg.get('post_refresh_retries', 4)
        cp['refresh_interval_seconds'] = pcfg.get('post_refresh_interval_seconds', 3)
        with trace.scope(order_id, row['trace_round']):
            try:
                if (cp['proxy_endpoint'] != endpoint
                        or cp['proxy_expires_at'] != proxy['expires_at'].isoformat()):
                    raise FollowupError('proxy_session_changed')
                if proxy['expires_at'] <= now():
                    raise FollowupError('proxy_session_expired')
                if cp['checkout_id'] != row['checkout_session_id']:
                    raise FollowupError('checkpoint_mismatch')
                execute(cp, lambda phase: _save(order_id, cp, phase))
                return {'state': 'completed', 'warning_code': ''}
            except Exception as exc:
                if cp['phase'] == 'completed':
                    return {'state': 'completed', 'warning_code': ''}
                code = warning_code(exc)
                _save(order_id, cp, warning=code)
                return {'state': 'review', 'warning_code': code}


def record_unexpected_failure(order_id, exc):
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
        return _run(order_id, plan=plan, responses=responses, proxy_url=proxy_url,
                    request_context=request_context, executor=EXECUTOR)
    except Exception as exc:
        return record_unexpected_failure(order_id, exc)


def resume(order_id):
    """Explicit recovery of an existing post-charge checkpoint; no historical replay."""
    return _run(order_id)
