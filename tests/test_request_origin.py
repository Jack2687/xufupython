import unittest
from starlette.requests import Request
from backend.request_origin import configured_origin, origin_allowed


def request(host, origin=None, scheme='http', **headers):
    fields = {'host': host, **headers}
    if origin is not None:
        fields['origin'] = origin
    return Request({'type': 'http', 'method': 'POST', 'scheme': scheme, 'path': '/api/auth/login',
                    'query_string': b'', 'server': ('127.0.0.1', 18970),
                    'headers': [(k.encode(), v.encode()) for k, v in fields.items()]})


class RequestOriginTests(unittest.TestCase):
    def test_https_public_origin_behind_http_proxy(self):
        public = configured_origin('https://pay.example.com')
        self.assertTrue(origin_allowed(request('pay.example.com', 'https://pay.example.com'), public))
        self.assertTrue(origin_allowed(request('pay.example.com:443', 'https://PAY.example.com:443'), public))
        self.assertTrue(origin_allowed(request('pay.example.com', 'https://pay.example.com', scheme='https'), public))

    def test_wrong_origin_host_scheme_port_and_forwarded_headers_denied(self):
        public = configured_origin('https://pay.example.com')
        for host, origin in [('pay.example.com', 'https://evil.example.com'),
                             ('pay.example.com', 'http://pay.example.com'),
                             ('pay.example.com', 'https://pay.example.com:444'),
                             ('evil.example.com', 'https://pay.example.com'),
                             ('127.0.0.1:5496', 'https://pay.example.com')]:
            with self.subTest(host=host, origin=origin):
                self.assertFalse(origin_allowed(request(host, origin, **{'x-forwarded-proto': 'https', 'x-forwarded-host': 'pay.example.com'}), public))

    def test_unconfigured_and_direct_access_keep_exact_origin_validation(self):
        self.assertTrue(origin_allowed(request('localhost:18970', 'http://localhost:18970')))
        self.assertFalse(origin_allowed(request('pay.example.com', 'https://pay.example.com')))
        self.assertTrue(origin_allowed(request('127.0.0.1:5496', 'http://127.0.0.1:5496'), configured_origin('https://pay.example.com')))
        self.assertTrue(origin_allowed(request('localhost:18970')))

    def test_invalid_origin_and_configuration_fail_closed(self):
        for value in ['', 'null', 'https://pay.example.com/path', 'https://pay.example.com?x=1',
                      'https://pay.example.com#fragment', 'https://user@pay.example.com',
                      'https://pay.example.com:bad', 'https://pay.example.com,https://evil.example.com',
                      ' https://pay.example.com', 'ftp://pay.example.com', 'https://pay.example.com\\evil']:
            with self.subTest(value=value):
                self.assertFalse(origin_allowed(request('pay.example.com', value)))
                if value:
                    with self.assertRaises(ValueError):
                        configured_origin(value)


if __name__ == '__main__':
    unittest.main()
