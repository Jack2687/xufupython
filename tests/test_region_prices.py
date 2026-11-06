"""Price schedule, cache, access controls, and changing regions; all sources mocked."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event
from unittest.mock import patch
from backend import db, price_reference as prices


def provider(country='PH', currency='PHP', amount=1100, override=982.14):
    month = {'amount': amount, 'tax': 'inclusive'}
    if override is not None:
        month['psp_override'] = {'amount': override, 'tax': 'exclusive'}
    return {'country_code': country, 'currency_config': {'symbol_code': currency, 'plus': {'month': month},
            'pro': {'month': {'amount': amount * 10, 'tax': 'inclusive'}}}}


def fx(moment=1800000000):
    return {'result': 'success', 'base_code': 'CNY', 'time_last_update_unix': moment,
            'rates': {'CNY': 1, 'USD': .14, 'PHP': 8, 'JPY': 20}}


class RegionalPriceTests:
    @contextmanager
    def price_fixture(self):
        keys = (prices.CONFIG_KEY, prices.PRICE_KEY, prices.FX_KEY, 'regions', 'features')
        saved = {key: db.scalar('SELECT value FROM settings WHERE key=$1', key) for key in keys}
        try:
            for key in keys[:3]:
                db.execute('DELETE FROM settings WHERE key=$1', key)
            prices.write('regions', {'items': [
                {'code': 'PH', 'name': '菲律宾', 'currency': 'PHP', 'enabled': True, 'sort': 0},
                {'code': 'JP', 'name': '日本', 'currency': 'JPY', 'enabled': False, 'sort': 1}]})
            yield
        finally:
            for key, value in saved.items():
                if value is None:
                    db.execute('DELETE FROM settings WHERE key=$1', key)
                else:
                    prices.write(key, value)

    def price_source(self, url):
        if url == prices.FX_URL:
            return fx()
        return provider() if url.endswith('/PH') else provider('JP', 'JPY', 3000, 2727)

    def test_region_prices_tax_units_and_invalid_evidence(self):
        ph = prices.parse_prices(provider(), 'PH')['plus']
        self.assertEqual((ph['amount'], ph['website_amount'], ph['tax']), (982.14, 1100, 'exclusive'))
        jp = prices.parse_prices(provider('JP', 'JPY', 3000, 2727), 'JP')['plus']
        self.assertEqual(jp['amount'], 2727)
        self.assertEqual(prices.parse_prices(provider(override=None), 'PH')['plus']['amount'], 1100)
        for value in [True, -1, float('nan'), float('inf'), '1100']:
            with self.assertRaises(prices.SourceError):
                prices.parse_prices(provider(amount=value), 'PH')
        with self.assertRaises(prices.SourceError):
            prices.parse_prices(provider('US'), 'PH')
        with self.assertRaises(prices.SourceError):
            prices.parse_fx(fx(1800000000 + 86400), 1800000000)
        self.assertEqual(prices.parse_fx(fx(), 1800000000)['rates']['PHP'], .125)

    def test_region_prices_schedule_permissions_and_cache_only_get(self):
        with self.price_fixture(), patch.object(prices, 'fetch_json') as fetch:
            _, _, member = self.new_user()
            try:
                self.assertEqual(member.get('/api/admin/region-prices').status_code, 403)
                self.assertEqual(member.put('/api/admin/region-prices', json=prices.DEFAULTS).status_code, 403)
                self.assertEqual(member.post('/api/admin/region-prices/refresh', json={'kind': 'prices'}).status_code, 403)
                default = self.ok(self.client.get('/api/admin/region-prices'))
                self.assertEqual(default['config'], prices.DEFAULTS)
                self.assertEqual(self.ok(member.get('/api/subscription-prices'))['rows'], [])
                cfg = {**prices.DEFAULTS, 'price_interval_hours': 3, 'fx_interval_days': 2}
                self.assertEqual(self.ok(self.client.put('/api/admin/region-prices', json=cfg))['config'], cfg)
                for change in [{'price_interval_hours': 0}, {'fx_interval_days': 0}, {'prices_enabled': 'yes'}, {'price_interval_hours': True}, {'secret': 'x'}]:
                    self.assertEqual(self.client.put('/api/admin/region-prices', json={**cfg, **change}).status_code, 400)
                flags = prices.read('features')
                prices.write('features', {**flags, 'subscribe': False, 'links': False})
                self.assertEqual(member.get('/api/subscription-prices').status_code, 403)
                fetch.assert_not_called()
            finally:
                member.close()

    def test_region_prices_hourly_and_daily_refresh_are_separate(self):
        with self.price_fixture(), patch.object(prices, 'clock', return_value=1800000000) as clock, patch.object(prices, 'fetch_json', side_effect=self.price_source) as fetch:
            prices.refresh_tick()  # Daily FX.
            prices.refresh_tick()  # Two regions, all plans.
            self.assertEqual(fetch.call_count, 3)
            prices.refresh_tick()
            clock.return_value += 3599
            prices.refresh_tick()
            self.assertEqual(fetch.call_count, 3)
            clock.return_value += 1
            prices.refresh_tick()
            self.assertEqual(fetch.call_count, 5)
            self.assertEqual(sum(c.args[0] == prices.FX_URL for c in fetch.call_args_list), 1)
            clock.return_value = 1800000000 + 86400
            prices.refresh_tick()
            self.assertEqual(sum(c.args[0] == prices.FX_URL for c in fetch.call_args_list), 2)
            # A newer fetch time does not falsify the upstream FX publication time.
            self.assertEqual(prices.read(prices.FX_KEY)['source_updated_at'], 1800000000)

    def test_region_prices_regions_added_deleted_and_renamed(self):
        with self.price_fixture(), patch.object(prices, 'clock', return_value=1800000000), patch.object(prices, 'fetch_json', side_effect=self.price_source) as fetch:
            prices.refresh_tick(); prices.refresh_tick()
            region = {'code': 'SG', 'name': '新加坡新名称', 'currency': 'SGD', 'enabled': False, 'sort': 0}
            prices.write('regions', {'items': [region]})
            # Deleted regions disappear without waiting for the next background tick.
            self.assertEqual(self.ok(self.client.get('/api/subscription-prices'))['rows'], [])
            fetch.side_effect = lambda _: provider('SG', 'SGD', 30, None)
            prices.refresh_tick()
            result = self.ok(self.client.get('/api/subscription-prices'))
            self.assertEqual({r['country'] for r in result['rows']}, {'SG'})
            self.assertTrue(all(r['name'] == '新加坡新名称' and r['amount_cny'] is None for r in result['rows']))
            import json
            payload = json.dumps(result)
            for upstream in ('backend-anon', 'open.er-api.com', 'checkout_pricing_config', 'https://'):
                self.assertNotIn(upstream, payload)
            self.assertEqual(set(prices.read(prices.PRICE_KEY)['entries']), {'SG'})

    def test_region_prices_failures_keep_success_timestamps_and_backoff(self):
        with self.price_fixture(), patch.object(prices, 'clock', return_value=1800000000) as clock, patch.object(prices, 'fetch_json', side_effect=self.price_source) as fetch:
            prices.refresh_tick(); prices.refresh_tick()
            before = deepcopy(prices.read(prices.PRICE_KEY)['entries']['PH'])
            clock.return_value += 3600
            fetch.side_effect = prices.SourceError('rate_limited')
            prices.refresh_tick()
            after = prices.read(prices.PRICE_KEY)['entries']['PH']
            self.assertEqual(after['fetched_at'], before['fetched_at'])
            self.assertEqual(after['quotes'], before['quotes'])
            self.assertEqual(after['error'], 'rate_limited')
            calls = fetch.call_count
            prices.refresh_tick()
            self.assertEqual(fetch.call_count, calls)
            result = self.ok(self.client.get('/api/subscription-prices'))
            self.assertTrue(all(r['stale'] for r in result['rows'] if r['country'] == 'PH'))

    def test_region_prices_cross_process_lock_prevents_duplicate_fetch(self):
        entered, release = Event(), Event()
        def blocked(_):
            entered.set()
            self.assertTrue(release.wait(5))
            return fx()
        with self.price_fixture(), patch.object(prices, 'clock', return_value=1800000000), patch.object(prices, 'fetch_json', side_effect=blocked) as fetch:
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(prices.refresh_tick)
                self.assertTrue(entered.wait(5))
                try:
                    second = executor.submit(prices.refresh_tick)
                    second.result(timeout=3)
                    self.assertEqual(fetch.call_count, 1)
                finally:
                    release.set()
                first.result(timeout=5)

    def test_region_prices_manual_refresh_coalesced_and_pause(self):
        with self.price_fixture(), patch.object(prices, 'clock', return_value=1800000000), patch.object(prices, 'fetch_json', side_effect=self.price_source) as fetch:
            cfg = {**prices.DEFAULTS, 'prices_enabled': False, 'fx_enabled': False}
            self.ok(self.client.put('/api/admin/region-prices', json=cfg))
            prices.refresh_tick()
            fetch.assert_not_called()
            self.ok(self.client.post('/api/admin/region-prices/refresh', json={'kind': 'prices'}))
            self.assertEqual(self.client.post('/api/admin/region-prices/refresh', json={'kind': 'prices'}).status_code, 429)
            prices.refresh_tick()
            self.assertEqual(fetch.call_count, 2)
            prices.refresh_tick()
            self.assertEqual(fetch.call_count, 2)

