"""Pipe-only Go post-payment adapter for gener_direct_pay; never pays an order."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from pathlib import Path
from queue import Queue, Empty
import base64
import json
import os
import select
import socketserver
import ssl
import subprocess
import threading
import time
from urllib.parse import urlsplit, urlunsplit

from . import payment_trace as trace
from .egress import connect_via_local, local_proxy
from .order_http_context import OrderHTTPContext
from .post_payment import FollowupError
from .renewals import COOKIE_NAME

EXECUTOR = 'go_cslive_v1'
ROOT = Path(__file__).resolve().parents[1]
LINE_LIMIT = 1048576


def binary_path():
    return ROOT / 'bin' / ('post-payment.exe' if os.name == 'nt' else 'post-payment')


class _ProxyRelay(socketserver.BaseRequestHandler):
    def handle(self):
        upstream = None
        try:
            remote = self.server.remote
            # Raw TCP relay only: Go performs the real vendor CONNECT with the
            # original credentials/session. The local hop never replaces it.
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
            # Credentials and socket errors must never reach application logs.
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
        # HTTPS vendor proxies use validated TLS with the original SNI above;
        # only the private loopback hop uses plain HTTP CONNECT.
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
    # Shared strict allowlist also handles numeric/boolean network evidence.
    diagnostics = dict(fields)
    for key in ('retry_wait_ms', 'retry_wait_elapsed_ms'):
        if type(diagnostics.get(key)) in (int, float):
            diagnostics[key] = round(diagnostics[key])
    diagnostics = trace._diagnostics(diagnostics)
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
                   'proxy_expires_at': cp['proxy_expires_at']}
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
                    # Commit credentials/fence before acknowledging any cancellation write.
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
                        # Error codes enter warning_code's fixed allowlist later.
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


def finish_payment(order_id, *, plan, responses=(), proxy_url=None, request_context=None):
    from .post_payment import _run, record_unexpected_failure
    try:
        return _run(order_id, plan=plan, responses=responses, proxy_url=proxy_url,
                    request_context=request_context, executor=EXECUTOR)
    except Exception as exc:
        return record_unexpected_failure(order_id, exc)
