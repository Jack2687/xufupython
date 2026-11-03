"""Offline process-boundary checks; never call upstream services or business DB."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json
import socket
import ssl
import subprocess
import sys
import threading
import unittest
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import go_post_payment as gp
from backend.post_payment import FollowupError


def checkpoint():
    return {'executor': gp.EXECUTOR, 'phase': 'activation', 'plan': 'pro5x', 'entity': 'openai_llc',
            'account_id': 'fixture-account', 'checkout_id': 'cs_fixture', 'cancel_requested': True,
            'activation': 'pending', 'renewal': 'unknown', 'return_url': '',
            'credentials': {'account_id': 'fixture-account', 'access_token': 'old-token', 'session_token': 'session-fixture'},
            'proxy_endpoint': 'http://proxy.example:8080',
            'proxy_expires_at': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}


def go_checkpoint(cp):
    return {**{k: cp[k] for k in ('phase', 'plan', 'entity', 'account_id', 'checkout_id', 'cancel_requested', 'activation', 'renewal')},
            'payment': 'charged', 'access_token': 'new-token', 'session_token': 'new-session', 'email': '',
            'cookies': '__Secure-next-auth.session-token=new-session', 'cancel_attempts': 0, 'metadata': {}}


class GoAdapterTests(unittest.TestCase):
    def test_local_proxy_bridge_preserves_vendor_route_and_credentials(self):
        for scheme in ('http', 'https'):
            with self.subTest(scheme=scheme):
                vendor, bridge = socket.socketpair()
                received, tls_calls = [], []
                class Stream:
                    def get_extra_info(self, key): return bridge
                    def close(self): bridge.close()
                    def start_tls(self, context, hostname, timeout):
                        tls_calls.append((context.verify_mode, context.check_hostname, hostname))
                        return self
                def echo():
                    with vendor:
                        received.append(vendor.recv(4096))
                        vendor.sendall(b'HTTP/1.1 200 Connection established\r\n\r\n')
                thread = threading.Thread(target=echo, daemon=True); thread.start()
                with patch.object(gp, 'local_proxy', return_value='http://127.0.0.1:7897'), \
                     patch.object(gp, 'connect_via_local', return_value=Stream()) as connect:
                    with gp.proxy_route(scheme + '://user:sticky_session@proxy.example:8443') as route:
                        parsed = urlsplit(route)
                        self.assertEqual((parsed.scheme, parsed.username, parsed.password), ('http', 'user', 'sticky_session'))
                        with socket.create_connection((parsed.hostname, parsed.port), timeout=3) as client:
                            request = b'CONNECT chatgpt.com:443 HTTP/1.1\r\n\r\n'
                            client.sendall(request)
                            self.assertIn(b'200', client.recv(4096))
                        thread.join(timeout=3)
                    connect.assert_called_once_with('proxy.example', 8443, 15)
                self.assertEqual(received, [request])
                self.assertEqual(tls_calls, [(ssl.CERT_REQUIRED, True, 'proxy.example')] if scheme == 'https' else [])

    def test_go_diagnostics_keep_wait_and_timing_but_reject_sensitive_fields(self):
        records = []
        def record(*args, **kwargs):
            records.append((args, gp.trace._diagnostics(kwargs['diagnostics'])))
        with patch.object(gp.trace, 'record', side_effect=record):
            gp._event({'event': 'http_retry_wait', 'fields': {'request_kind': 'session_read',
                'attempt': 1, 'retry_wait_ms': 500.0, 'retry_wait_elapsed_ms': 502.67}})
            gp._event({'event': 'http_request', 'fields': {'request_kind': 'session_read',
                'status': 200, 'connection_ready_ms': 30.15, 'network_stage': 'response_body',
                'network_stage_elapsed_ms': 1.25, 'access_token': 'secret', 'path': 'secret-url'}})
        self.assertEqual(records[0][1]['retry_wait_elapsed_ms'], 503)
        self.assertEqual(records[1][1]['network_stage'], 'response_body')
        self.assertEqual(records[1][1]['connection_ready_ms'], 30.15)
        self.assertNotIn('secret', json.dumps(records))
        self.assertEqual(gp.trace._diagnostics({'network_stage': 'secret', 'first_byte_ms': float('nan')}), {})

    def child(self, cp, frames, save):
        native = subprocess.Popen
        with TemporaryDirectory() as temp:
            script = Path(temp) / 'fixture.py'
            script.write_text('import json,sys\n'
                'incoming=json.loads(sys.stdin.readline())\n'
                'assert incoming["state"]["account_id"]=="fixture-account"\n'
                'assert incoming["proxy"]=="http://proxy.example:8080"\n'
                'frames=' + repr(frames) + '\n'
                'for frame in frames:\n'
                ' print(json.dumps(frame),flush=True)\n'
                ' if frame["type"]=="checkpoint":\n'
                '  ack=json.loads(sys.stdin.readline())\n'
                '  assert ack.get("ok") is True\n', encoding='utf-8')
            def start(argv, **kwargs):
                self.assertEqual(argv, [str(script)])
                return native([sys.executable, '-u', str(script)], **kwargs)
            with patch.object(gp, 'binary_path', return_value=script), patch.object(gp, 'local_proxy', return_value=''), \
                 patch.object(gp.subprocess, 'Popen', side_effect=start):
                gp.execute(cp, save)

    def test_real_pipe_acknowledges_saved_checkpoint(self):
        cp = checkpoint(); value = go_checkpoint(cp)
        value.update(phase='completed', activation='active', renewal='off', cancel_attempts=1)
        saved = []
        self.child(cp, [{'type': 'checkpoint', 'phase': 'completed', 'checkpoint': value},
                        {'type': 'result', 'ok': True, 'checkpoint': value}], lambda phase: saved.append(deepcopy(cp)))
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]['credentials']['access_token'], 'new-token')
        self.assertEqual(cp['go_context']['cancel_attempts'], 1)

    def test_save_failure_does_not_claim_completion(self):
        cp = checkpoint(); before = deepcopy(cp); value = go_checkpoint(cp)
        value.update(phase='completed', activation='active', renewal='off')
        def fail(_): raise FollowupError('checkpoint_failed')
        with self.assertRaisesRegex(FollowupError, 'checkpoint_failed'):
            self.child(cp, [{'type': 'checkpoint', 'phase': 'completed', 'checkpoint': value}], fail)
        self.assertEqual(cp, before)

    def test_cross_account_checkpoint_rejected(self):
        cp = checkpoint(); value = go_checkpoint(cp); value['account_id'] = 'different'
        with self.assertRaisesRegex(FollowupError, 'checkpoint_mismatch'):
            gp.apply_checkpoint(cp, value)
        self.assertNotIn('go_context', cp)

    def test_failure_preserves_updated_authorization(self):
        cp = checkpoint(); value = go_checkpoint(cp); value['activation'] = 'active'
        with self.assertRaisesRegex(FollowupError, 'http_403'):
            self.child(cp, [{'type': 'result', 'ok': False, 'error_code': 'http_403', 'checkpoint': value}], lambda _: None)
        self.assertEqual(cp['credentials']['access_token'], 'new-token')
        self.assertEqual(cp['activation'], 'active')

    def test_missing_worker_never_falls_back(self):
        with patch.object(gp, 'binary_path', return_value=ROOT / '.local/nonexistent-worker'), \
             patch.object(gp.subprocess, 'Popen') as child:
            with self.assertRaisesRegex(FollowupError, 'go_followup_unavailable'):
                gp.execute(checkpoint(), lambda _: None)
            child.assert_not_called()

    def test_expired_proxy_prevents_process(self):
        cp = checkpoint(); cp['proxy_expires_at'] = '2000-01-01T00:00:00+00:00'
        with patch.object(gp, 'binary_path', return_value=Path(__file__)), patch.object(gp.subprocess, 'Popen') as child:
            with self.assertRaisesRegex(FollowupError, 'proxy_session_expired'):
                gp.execute(cp, lambda _: None)
            child.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
