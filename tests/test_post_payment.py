"""Post-payment protocol tests. All HTTP uses an in-memory transport."""
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.common import now
from backend.post_payment import (Followup, FollowupError, Platform, canonical_plan, renewal_state,
                                  select_return_url, subscription_active, subscription_not_found,
                                  validate_return_url)


def checkpoint(cancel=True):
    return {'phase': 'activation', 'plan': 'pro5x', 'activation': 'pending', 'renewal': 'unknown',
            'cancel_requested': cancel, 'checkout_id': 'cs_test_fixture', 'account_id': 'account-fixture',
            'entity': 'openai_llc', 'return_url': '', 'expected_ip': '8.8.4.4',
            'proxy_expires_at': (now() + timedelta(minutes=10)).isoformat(),
            'credentials': {'access_token': 'original-token', 'session_token': 'session-fixture',
                            'account_id': 'account-fixture'}}


ACTIVE = {'plan_type': 'standard', 'current_period_end': 2000000000, 'will_renew': True}
OFF = {'plan_type': 'standard', 'current_period_end': 2000000000, 'will_renew': False}


class ScriptedPlatform:
    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []

    def call(self, name):
        self.calls.append(name)
        expected, result = self.steps.pop(0)
        assert expected == name, (expected, name)
        if isinstance(result, Exception):
            raise result
        return deepcopy(result)

    def visit_return(self, url): return self.call('return')
    def subscriptions(self, renewal=False): return self.call('renewal' if renewal else 'activation')
    def refresh(self): return self.call('refresh')
    def cancel(self): return self.call('cancel')


class PostPaymentTests(unittest.TestCase):
    def flow(self, steps, cancel=True, cp=None):
        cp = cp or checkpoint(cancel)
        platform = ScriptedPlatform(steps)
        saved, pauses = [], []
        def save(phase):
            if phase: cp['phase'] = phase
            saved.append(deepcopy(cp))
        return Followup(cp, platform, save, pauses.append), platform, saved, pauses

    def test_plan_and_period_evidence_is_required(self):
        aliases = {'go': ['go_plan', 'chatgptgo'], 'plus': ['basic', 'chatgptplusplan'],
                   'pro5x': ['standard', 'prolite'], 'pro': ['premium', 'pro'],
                   'team': ['default', 'chatgptteamplan'], 'team_premium': ['team', 'prolite']}
        for plan, types in aliases.items():
            for kind in types:
                self.assertTrue(subscription_active({'plan_type': kind, 'active_until': 1}, plan))
        self.assertFalse(subscription_active({'plan_type': 'standard', 'is_active': True}, 'pro5x'))
        self.assertFalse(subscription_active(ACTIVE, 'pro'))
        self.assertTrue(subscription_active({'plan_type': 'STANDARD', 'will_renew': True}, 'pro5x'))
        self.assertEqual(canonical_plan('pro'), 'pro20x')

    def test_renewal_missing_conflicting_and_nested_evidence(self):
        cases = [({}, 'unknown'), ({'is_active': False}, 'unknown'), ({'will_renew': False}, 'off'),
                 ({'will_renew': 0}, 'unknown'), ({'will_renew': 'false'}, 'unknown'),
                 ({'cancel_at_period_end': True}, 'off'), ({'cancel_at': 1}, 'off'),
                 ({'will_renew': True, 'cancel_at_period_end': True}, 'unknown'),
                 ({'subscriptions': [{'will_renew': False}]}, 'off'),
                 ({'items': [{'will_renew': True}, {'will_renew': False}]}, 'unknown')]
        for data, state in cases:
            self.assertEqual(renewal_state(data), state, data)

    def test_return_url_is_bound_to_order_and_no_external_redirect(self):
        cp = checkpoint()
        valid = select_return_url(cp, [])
        self.assertEqual(validate_return_url(valid, cp), valid)
        for raw in [valid.replace('https:', 'http:'), valid.replace('chatgpt.com', 'chatgpt.com.evil.test'),
                    valid.replace('chatgpt.com', 'user@chatgpt.com'), valid.replace('chatgpt.com', 'chatgpt.com:443'),
                    valid.replace('/verify', '/other'), valid + '#fragment', valid + '&account_id=other',
                    valid + '&stripe_session_id=cs_test_fixture', valid + '&processor_entity=other',
                    valid + '\\', valid + '\n', valid + '&value=%xx']:
            with self.assertRaisesRegex(FollowupError, 'invalid_return_url'):
                validate_return_url(raw, cp)
        chosen = valid + '&account_id=account-fixture'
        self.assertEqual(select_return_url(cp, [{'success_url': 'https://evil.test',
                                                 'after_completion': {'redirect': {'url': chosen}}}]), chosen)
        cp['plan'] = 'team5x'
        self.assertIn('plan_type=team', select_return_url(cp, []))

    def test_only_precise_404_is_arrival_delay(self):
        raw = b'{"detail":"No subscription found for account"}'
        check = lambda **kw: subscription_not_found(kw.get('method', 'GET'), kw.get('path', '/backend-api/subscriptions?account_id=a'),
                              404, kw.get('headers', {'content-type': 'application/json'}), kw.get('raw', raw))
        self.assertTrue(check())
        for kwargs in ({'method': 'POST'}, {'path': '/other'}, {'headers': {'content-type': 'text/html'}},
                       {'raw': b'{"detail":"subscription_not_found"}'}, {'raw': raw + b'{}'},
                       {'raw': b'{"detail":"other","detail":"No subscription found for account"}'},
                       {'raw': raw + b' ' * 4096}, {'raw': b'[]'}):
            self.assertFalse(check(**kwargs))

    def test_return_then_query_401_refresh_retry_keep(self):
        cp = checkpoint(False); cp['return_url'] = select_return_url(cp, [])
        flow, platform, saved, _ = self.flow([('return', None), ('activation', FollowupError('http_401')),
                                             ('refresh', None), ('activation', ACTIVE)], cp=cp)
        flow.finish()
        self.assertFalse(platform.steps)
        self.assertEqual(cp['phase'], 'completed')
        self.assertEqual(cp['activation'], 'active')
        self.assertEqual(len(saved), 4)

    def test_activation_delay_has_five_checks_no_cancel(self):
        flow, platform, _, pauses = self.flow([('activation', FollowupError('subscription_not_found'))] * 5)
        with self.assertRaisesRegex(FollowupError, '^activation_pending$'):
            flow.finish()
        self.assertEqual(pauses, [2] * 4)
        self.assertNotIn('cancel', platform.calls)

    def test_generic_404_and_refresh_403_stop(self):
        for steps, error in [([('activation', FollowupError('http_404'))], 'http_404'),
                             ([('activation', FollowupError('http_401')), ('refresh', FollowupError('http_403'))], 'http_403')]:
            flow, platform, _, _ = self.flow(steps)
            with self.assertRaisesRegex(FollowupError, error): flow.finish()
            self.assertNotIn('cancel', platform.calls)

    def test_cancel_already_off_skips_post_and_unknown_stops(self):
        for state, error in [(OFF, None), ({}, 'renewal_state_unknown')]:
            flow, platform, saved, _ = self.flow([('activation', ACTIVE), ('refresh', None), ('renewal', state)])
            if error:
                with self.assertRaisesRegex(FollowupError, error): flow.finish()
            else:
                flow.finish(); self.assertEqual(saved[-1]['phase'], 'completed')
            self.assertNotIn('cancel', platform.calls)

    def test_lost_cancel_response_checks_before_retry_and_avoids_duplicate(self):
        flow, platform, saved, _ = self.flow([('activation', ACTIVE), ('refresh', None), ('renewal', ACTIVE),
                                             ('cancel', FollowupError('network_error')), ('renewal', OFF)])
        flow.finish()
        self.assertEqual(platform.calls.count('cancel'), 1)
        self.assertEqual(saved[-2]['phase'], 'cancellation_requested')
        self.assertEqual(saved[-1]['phase'], 'completed')

    def test_retry_only_allowlist_and_explicitly_on(self):
        for error in ['network_error', 'http_408', 'http_429', 'http_500', 'http_502', 'http_503', 'http_504']:
            steps = [('activation', ACTIVE), ('refresh', None), ('renewal', ACTIVE),
                     ('cancel', FollowupError(error)), ('renewal', ACTIVE), ('cancel', {}), ('renewal', OFF)]
            flow, platform, _, _ = self.flow(steps)
            flow.finish(); self.assertEqual(platform.calls.count('cancel'), 2)
        for error, state in [('http_403', ACTIVE), ('http_401', ACTIVE), ('http_400', ACTIVE), ('network_error', {})]:
            flow, platform, _, _ = self.flow([('activation', ACTIVE), ('refresh', None), ('renewal', ACTIVE),
                                             ('cancel', FollowupError(error)), ('renewal', state)])
            with self.assertRaisesRegex(FollowupError, error): flow.finish()
            self.assertEqual(platform.calls.count('cancel'), 1)

    def test_cancellation_polling_stays_pending_not_disabled(self):
        flow, platform, _, pauses = self.flow([('activation', ACTIVE), ('refresh', None), ('renewal', ACTIVE),
                                              ('cancel', {})] + [('renewal', ACTIVE)] * 5)
        with self.assertRaisesRegex(FollowupError, 'cancellation_pending'): flow.finish()
        self.assertEqual(platform.calls.count('cancel'), 1)
        self.assertEqual(pauses, [2] * 4)

    def test_transport_preserves_cookie_auth_routes_and_refresh_order(self):
        cp = checkpoint(); requests = []
        def respond(request):
            requests.append(request)
            if request.url.host == 'ipinfo.io':
                self.assertNotIn('authorization', request.headers)
                self.assertNotIn('cookie', request.headers)
                return httpx.Response(200, json={'ip': '8.8.4.4'})
            self.assertIn('session-fixture', request.headers.get('cookie', ''))
            if request.url.path == '/api/auth/session':
                self.assertNotIn('authorization', request.headers)
                self.assertEqual(str(request.url.query, 'ascii'), 'refresh=true&reason=token_expired')
                return httpx.Response(200, json={'accessToken': 'refreshed-token', 'account': {'id': cp['account_id']}},
                                      headers={'set-cookie': '__Secure-next-auth.session-token=rotated; Path=/; Secure'})
            self.assertEqual(request.headers['authorization'], 'Bearer refreshed-token')
            self.assertEqual(request.headers['x-openai-target-path'], '/api/v1/subscriptions/cancel')
            self.assertEqual(json.loads(request.content), {'account_id': cp['account_id']})
            return httpx.Response(200, json={})
        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            p = Platform(client, cp); p.refresh(); p.cancel()
        self.assertEqual([r.url.host for r in requests], ['ipinfo.io', 'chatgpt.com', 'ipinfo.io'] * 2)
        self.assertEqual(cp['credentials']['cookies']['__Secure-next-auth.session-token'], 'rotated')

    def test_ip_mismatch_and_expiry_prevent_business_requests(self):
        for expired in (False, True):
            cp = checkpoint(); business = []
            if expired: cp['proxy_expires_at'] = (now() - timedelta(seconds=1)).isoformat()
            def respond(request):
                if request.url.host != 'ipinfo.io': business.append(request)
                return httpx.Response(200, json={'ip': '1.1.1.1'})
            with httpx.Client(transport=httpx.MockTransport(respond)) as client:
                with self.assertRaisesRegex(FollowupError, 'proxy_session_expired' if expired else 'proxy_ip_changed'):
                    Platform(client, cp).cancel()
            self.assertEqual(business, [])

    def test_ip_change_after_cancel_is_not_retried(self):
        flow, platform, _, _ = self.flow([('activation', ACTIVE), ('refresh', None), ('renewal', ACTIVE),
                                         ('cancel', FollowupError('proxy_ip_changed'))])
        with self.assertRaisesRegex(FollowupError, 'proxy_ip_changed'): flow.finish()
        self.assertEqual(platform.calls[-1], 'cancel')

    def test_http_status_precedes_html_json_failure(self):
        cp = checkpoint()
        def respond(request):
            if request.url.host == 'ipinfo.io': return httpx.Response(200, json={'ip': '8.8.4.4'})
            return httpx.Response(403, text='<html>challenge</html>', headers={'content-type': 'text/html'})
        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            with self.assertRaisesRegex(FollowupError, '^http_403$'): Platform(client, cp).refresh()

    def test_checkpoint_failure_prevents_cancel(self):
        flow, platform, _, _ = self.flow([('activation', ACTIVE), ('refresh', None), ('renewal', ACTIVE)])
        original = flow.save
        def broken_save(phase):
            if phase == 'cancellation_requested': raise OSError('fixture database failure')
            original(phase)
        flow.save = broken_save
        with self.assertRaises(OSError): flow.finish()
        self.assertNotIn('cancel', platform.calls)


if __name__ == '__main__':
    unittest.main(verbosity=2)
