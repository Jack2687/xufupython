"""Offline redaction and actual HTTP-context handoff checks."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.http_evidence import BODY_LIMIT, failure_evidence
from backend.order_http_context import OrderHTTPContext, current, response_headers


class EvidenceTests(unittest.TestCase):
    def test_html_only_logs_visible_redacted_summary(self):
        raw = '<html><title>Access denied</title><script>secret-script</script><style>secret-style</style><body>Try later, cookie-secret person@example.com</body></html>'
        e = failure_evidence(403, {'content-type': 'text/html', 'cf-mitigated': 'challenge'}, raw, ['cookie-secret'])
        self.assertEqual(e['response_title'], 'Access denied')
        self.assertTrue(e['challenge_required'])
        for value in ('secret-script', 'secret-style', 'cookie-secret', 'person@example.com'):
            self.assertNotIn(value, json.dumps(e))

    def test_oversized_response_omits_body_instead_of_partial_secrets(self):
        e = failure_evidence(503, {'content-type': 'text/plain'}, b'x' * (BODY_LIMIT + 1))
        self.assertTrue(e['response_truncated'])
        self.assertEqual(e['response_summary'], 'Response body omitted: size limit')

    def test_no_success_body_or_arbitrary_response_headers_logged(self):
        self.assertEqual(failure_evidence(200, {'set-cookie': 'secret'}, '{"accessToken":"secret"}'), {})
        e = failure_evidence(403, {'set-cookie': 'secret', 'authorization': 'secret', 'cf-ray': 'bad secret'}, '{}')
        self.assertEqual(e, {})

    def test_encoded_secrets_and_unknown_tokens_are_removed(self):
        e = failure_evidence(403, {'content-type': 'application/json'}, json.dumps({'detail':
            'token%2Fsecret token/secret dG9rZW4vc2VjcmV0 Bearer abc.def.ghi session_token=private '
            '123456789012345678901234567890123456'}), ['token/secret'])
        value = json.dumps(e)
        for secret in ('token%2Fsecret', 'token/secret', 'dG9rZW4vc2VjcmV0', 'abc.def.ghi', 'private', '123456789012345678901234567890123456'):
            self.assertNotIn(secret, value)

    def test_xml_error_is_bounded_and_does_not_expand_entities(self):
        e = failure_evidence(403, {'content-type': 'application/xml'},
            '<Error><Code>AccessDenied</Code><Message>Denied secret-value</Message></Error>', ['secret-value'])
        self.assertEqual(e['response_error_code'], 'AccessDenied')
        self.assertNotIn('secret-value', json.dumps(e))
        e = failure_evidence(403, {'content-type': 'application/xml'},
            '<!DOCTYPE Error [<!ENTITY e "private">]><Error><Message>&e;</Message></Error>')
        self.assertNotIn('response_message', e)


class ContextTests(unittest.TestCase):
    def test_curl_proxy_interim_headers_and_cookie_domain_scope(self):
        raw = ('HTTP/1.1 200 Connection established\r\nSet-Cookie: proxy=wrong\r\n\r\n'
               'HTTP/2 200\r\nSet-Cookie: real=one; Path=/; Secure\r\n'
               'Set-Cookie: route=two; Path=/backend-api; Secure\r\n'
               'Set-Cookie: foreign=bad; Domain=stripe.com; Path=/\r\n\r\n')
        c = OrderHTTPContext(); c.absorb('POST', 'https://chatgpt.com/backend-api/payments/checkout', response_headers(raw))
        headers = c.prepare('POST', 'https://chatgpt.com/backend-api/subscriptions', {'User-Agent': 'Fixture'})
        self.assertIn('real=one', headers['cookie']); self.assertIn('route=two', headers['cookie'])
        self.assertNotIn('proxy', headers['cookie']); self.assertNotIn('foreign', headers['cookie'])
        self.assertNotIn('content-length', headers)
        self.assertNotIn('route=two', c.prepare('GET', 'https://chatgpt.com/', {})['cookie'])

    def test_parallel_orders_do_not_share_context_or_cookies(self):
        def order(value):
            context = OrderHTTPContext({'session_token': value})
            with context.bind():
                self.assertIsNone(current('https://api.stripe.com/test'))
                self.assertIs(current('https://chatgpt.com/test'), context)
                return current('https://chatgpt.com/test').prepare('GET', 'https://chatgpt.com/', {})['cookie']
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(order, ['order-one', 'order-two']))
        self.assertIn('order-one', results[0]); self.assertNotIn('order-two', results[0])
        self.assertIn('order-two', results[1]); self.assertNotIn('order-one', results[1])
        self.assertIsNone(current('https://chatgpt.com/'))

    def test_generator_captures_real_headers_and_removes_temporary_files(self):
        import generator
        context = OrderHTTPContext({'session_token': 'fixture-session'})
        files = []
        def run(command, **kwargs):
            header_file = Path(command[command.index('--dump-header') + 1]); files.append(header_file)
            self.assertFalse(any('Content-Length: 0' in item for item in command))
            self.assertTrue(any('fixture-session' in item for item in command))
            header_file.write_bytes(b'HTTP/2 200\r\nSet-Cookie: actual=observed; Path=/; Secure\r\n\r\n')
            from types import SimpleNamespace
            return SimpleNamespace(returncode=0, stdout='{}\n__HTTP_STATUS__:200', stderr='')
        with context.bind(), patch.object(generator, '_find_curl', return_value='curl'), \
             patch.object(generator, 'build_curl_proxy_args', return_value=[]), patch.object(generator.subprocess, 'run', side_effect=run):
            status, _ = generator.curl_request('POST', 'https://chatgpt.com/backend-api/payments/checkout',
                proxy_url='http://proxy.example.test:8080', headers={'User-Agent': 'Fixture'}, json_body={'test': True})
        self.assertEqual(status, 200)
        self.assertEqual(context.snapshot()['headers']['user-agent'], 'Fixture')
        self.assertTrue(any(c['name'] == 'actual' for c in context.snapshot()['cookies']))
        self.assertTrue(all(not f.exists() for f in files))


if __name__ == '__main__':
    unittest.main(verbosity=2)
