"""Read local billing profiles and intercept outgoing payloads; never send a payment."""
import json
from pathlib import Path
import sys
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import db
from backend.common import config
from backend.subscription import _billing_snapshot, _payment_billing
from backend.prolong.stripe_hosted import StripeHostedClient, build_payment_method_form
from urllib.parse import urlsplit
import os


def verify():
    url = urlsplit(os.environ['DATABASE_URL'])
    assert url.hostname == '127.0.0.1' and url.port == 18972, 'Local database only'
    cfg = config('billing')
    account_email = db.scalar("SELECT email FROM orders WHERE workflow='subscription' ORDER BY created_at DESC LIMIT 1")
    assert account_email, 'A subscription account email is required for this local check'
    profiles = [p for p in cfg['address_templates'] if p.get('enabled') and not p.get('test_only')]
    assert profiles, 'No active system billing profiles'
    count = 0
    fallback = 0
    regions = set()
    response = Mock(status_code=200, headers={}, text='{}')
    response.json.return_value = {'id': 'pm_offline_fixture'}
    # Guard all requests, then intercept the actual post boundary used by both methods.
    with patch('requests.sessions.Session.request', side_effect=AssertionError('External network forbidden')), \
         patch('requests.sessions.Session.post', return_value=response) as post:
        client = StripeHostedClient(pk='pk_test_offline')
        try:
            for profile in profiles:
                snapshot = _billing_snapshot(dict(profile, country=cfg['country']), account_email)
                address, name, email = _payment_billing(snapshot)
                client.update_tax_region('cs_test_offline', address)
                tax = post.call_args.kwargs['data']
                form = build_payment_method_form(
                    card_number='4242424242424242', exp_month='12', exp_year='2030', cvc='123',
                    name=name, email=email, pk='pk_test_offline', guid='offline', muid='offline',
                    sid='offline', client_session_id='offline', cs='cs_test_offline',
                    checkout_config_id='offline', runtime_version='offline', address=address)
                client.create_payment_method(form)
                payment = post.call_args.kwargs['data']
                for key in ('country', 'state', 'city', 'line1', 'postal_code'):
                    assert tax[f'tax_region[{key}]'] == payment[f'billing_details[address][{key}]'] == snapshot[key]
                assert payment['billing_details[name]'] == name
                assert payment['billing_details[email]'] == (str(profile.get('email') or '').strip() or account_email)
                regions.add(snapshot['state'])
                fallback += snapshot['email_source'] == 'subscription_account'
                count += 1
        finally:
            client.session.close()
    result = {'profiles_checked': count, 'passed': True, 'account_email_fallback': fallback,
              'regions': sorted(regions), 'address_fields': ['country', 'state', 'city', 'line1', 'postal_code'],
              'payment_identity_fields': ['name', 'email'], 'intercepted_posts': post.call_count,
              'external_requests': 0, 'real_payments': 0}
    (ROOT/'.local/billing-payload-verification.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result))


if __name__ == '__main__':
    try:
        verify()
    finally:
        db.close()
