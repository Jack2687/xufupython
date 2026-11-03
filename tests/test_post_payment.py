"""Post-payment protocol tests. All HTTP uses an in-memory transport."""
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

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
            self.assertEqual(request.url.host, 'chatgpt.com')
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
        self.assertEqual([r.url.host for r in requests], ['chatgpt.com'] * 2)
        self.assertEqual(cp['credentials']['cookies']['__Secure-next-auth.session-token'], 'rotated')

    def test_missing_ip_does_not_block_but_session_expiry_still_does(self):
        for expired in (False, True):
            cp = checkpoint(); cp.pop('expected_ip'); business = []
            if expired: cp['proxy_expires_at'] = (now() - timedelta(seconds=1)).isoformat()
            def respond(request):
                self.assertEqual(request.url.host, 'chatgpt.com')
                business.append(request)
                return httpx.Response(200, json={})
            with httpx.Client(transport=httpx.MockTransport(respond)) as client:
                if expired:
                    with self.assertRaisesRegex(FollowupError, 'proxy_session_expired'):
                        Platform(client, cp).cancel()
                else:
                    Platform(client, cp).cancel()
            self.assertEqual(len(business), 0 if expired else 1)

    def test_session_expiry_after_cancel_is_not_retried(self):
        flow, platform, _, _ = self.flow([('activation', ACTIVE), ('refresh', None), ('renewal', ACTIVE),
                                         ('cancel', FollowupError('proxy_session_expired'))])
        with self.assertRaisesRegex(FollowupError, 'proxy_session_expired'): flow.finish()
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

    def test_get_transient_failures_retry_three_times_with_package_delays(self):
        for status in (408, 429, 500, 502, 503, 504, None):
            calls, pauses = [], []
            def respond(request):
                calls.append(request)
                if len(calls) < 3:
                    if status is None: raise httpx.ConnectError('network fixture')
                    return httpx.Response(status, json={'error': {'code': 'unavailable'}})
                return httpx.Response(200, json=ACTIVE)
            with httpx.Client(transport=httpx.MockTransport(respond)) as client:
                self.assertEqual(Platform(client, checkpoint(), pause=pauses.append).subscriptions(), ACTIVE)
            self.assertEqual(len(calls), 3)
            self.assertEqual(pauses, [.5, 1])

    def test_get_retry_exhaustion_and_non_retry_statuses(self):
        for status, expected in [(503, 3), (400, 1), (401, 1), (403, 1), (404, 1), (302, 1)]:
            calls, pauses = [], []
            def respond(request):
                calls.append(request)
                return httpx.Response(status, text='refused')
            with httpx.Client(transport=httpx.MockTransport(respond)) as client:
                with self.assertRaisesRegex(FollowupError, 'http_' + str(status)):
                    Platform(client, checkpoint(), pause=pauses.append).subscriptions()
            self.assertEqual(len(calls), expected)
            self.assertEqual(pauses, [.5, 1] if expected == 3 else [])

    def test_post_transport_never_replays_cancel(self):
        for status in (503, None):
            calls, pauses = [], []
            def respond(request):
                calls.append(request)
                if status is None: raise httpx.ReadTimeout('response lost')
                return httpx.Response(status, json={})
            with httpx.Client(transport=httpx.MockTransport(respond)) as client:
                with self.assertRaises(FollowupError):
                    Platform(client, checkpoint(), pause=pauses.append).cancel()
            self.assertEqual(len(calls), 1)
            self.assertEqual(pauses, [])

    def test_expiry_during_retry_wait_prevents_next_request(self):
        cp, calls = checkpoint(), []
        def respond(request):
            calls.append(request)
            return httpx.Response(503, json={})
        def pause(seconds): cp['proxy_expires_at'] = (now() - timedelta(seconds=1)).isoformat()
        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            with self.assertRaisesRegex(FollowupError, 'proxy_session_expired'):
                Platform(client, cp, pause=pause).subscriptions()
        self.assertEqual(len(calls), 1)

    def test_403_evidence_is_redacted_and_no_refresh_or_cancel(self):
        from backend import payment_trace as trace
        cp = checkpoint(); events, calls = [], []
        cp['proxy_endpoint'] = 'http://proxy-user:proxy-password@proxy.example.test:8080'
        def respond(request):
            calls.append(request.url.path)
            return httpx.Response(403, json={'error': {'code': 'access_denied', 'type': 'permission_error',
                'message': 'Denied original-token session-fixture proxy-password account-fixture cHJveHktdXNlcjpwcm94eS1wYXNzd29yZA== '
                           'user@example.com 4242 4242 4242 4242 https://example.test/?token=hidden'}},
                headers={'cf-ray': '1234567890abcdef-SJC', 'x-request-id': '11111111-2222-3333-4444-555555555555'})
        with patch.object(trace, 'record', side_effect=lambda *a, **kw: events.append((a, deepcopy(kw)))), trace.scope('fixture', 1):
            with httpx.Client(transport=httpx.MockTransport(respond)) as client:
                with self.assertRaisesRegex(FollowupError, 'http_403'):
                    Followup(cp, Platform(client, cp), lambda phase: None).finish()
        self.assertEqual(calls, ['/backend-api/subscriptions'])
        failed = next(kw for a, kw in events if len(a) > 1 and a[1] == 'failed')
        evidence = failed['diagnostics']; encoded = json.dumps(evidence)
        for value in ('original-token', 'session-fixture', 'proxy-password', 'account-fixture', 'user@example.com', '4242', 'token=hidden', 'cHJveHktdXNlcjpwcm94eS1wYXNzd29yZA=='):
            self.assertNotIn(value, encoded)
        self.assertEqual(evidence['response_error_code'], 'access_denied')
        self.assertEqual(evidence['cf_ray'], '1234567890abcdef-SJC')
        self.assertEqual(evidence['response_content_type'], 'application/json')

    def test_actual_context_survives_refresh_failure_and_resume(self):
        from backend.order_http_context import OrderHTTPContext
        context = OrderHTTPContext(checkpoint()['credentials'])
        context.prepare('POST', 'https://chatgpt.com/backend-api/payments/checkout',
                        {'User-Agent': 'Fixture client/1.0', 'Accept-Language': 'en-US'})
        context.absorb('POST', 'https://chatgpt.com/backend-api/payments/checkout',
                       httpx.Headers([('set-cookie', 'checkout-context=actual-cookie; Path=/; Secure')]))
        cp = checkpoint(); cp['request_context'] = context.snapshot()
        def first(request):
            self.assertEqual(request.headers['user-agent'], 'Fixture client/1.0')
            self.assertIn('checkout-context=actual-cookie', request.headers['cookie'])
            return httpx.Response(503, json={}, headers={'set-cookie': 'checkout-context=rotated-cookie; Path=/; Secure'})
        with httpx.Client(transport=httpx.MockTransport(first)) as client:
            p = Platform(client, cp, pause=lambda _: None)
            # Only the first request sees the original value; retry is tested elsewhere.
            with self.assertRaisesRegex(FollowupError, 'http_503'): p.cancel()
        recovered = deepcopy(cp)
        def second(request):
            self.assertIn('checkout-context=rotated-cookie', request.headers['cookie'])
            self.assertEqual(request.headers['accept-language'], 'en-US')
            self.assertNotIn('authorization', request.headers)
            return httpx.Response(200, json={'accessToken': 'fresh-token'},
                                  headers={'set-cookie': '__Secure-next-auth.session-token=new-session; Path=/; Secure'})
        with httpx.Client(transport=httpx.MockTransport(second)) as client:
            Platform(client, recovered).refresh()
        self.assertEqual(recovered['credentials']['access_token'], 'fresh-token')
        self.assertEqual(recovered['credentials']['cookies']['__Secure-next-auth.session-token'], 'new-session')

    def test_deleted_context_cookie_is_not_resurrected_on_resume(self):
        cp = checkpoint()
        def respond(request):
            return httpx.Response(200, json=ACTIVE, headers=[
                ('set-cookie', '__Secure-next-auth.session-token=; Max-Age=0; Path=/; Secure'),
                ('set-cookie', '__Secure-platform.session-token=; Max-Age=0; Path=/; Secure')])
        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            Platform(client, cp).subscriptions()
        with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=ACTIVE))) as client:
            Platform(client, cp)
            self.assertEqual(list(client.cookies.jar), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
