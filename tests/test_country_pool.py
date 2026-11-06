"""Country-pool integration: no provider traffic or payment requests."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from unittest.mock import patch

from backend import db, country_pool as pool, regions, subscription_cslive_go as flow
from backend.common import encrypt, decrypt, uid


class CountryPoolTests:
    @contextmanager
    def pool_fixture(self):
        saved={k:db.scalar('SELECT value FROM settings WHERE key=$1',k) for k in ('proxy','country_pool','regions')}
        try:
            cfg={**deepcopy(pool.DEFAULTS),'token_enc':encrypt('token-fixture'),'password_enc':encrypt('password-fixture'),
                 'username':'proxy-fixture','revision':uid(),'countries':[{'code':'PH','name':'菲律宾','currency':'PHP'},{'code':'US','name':'美国','currency':'USD'}]}
            with db.transaction() as q:pool.write(q,cfg)
            yield cfg
        finally:
            for k,v in saved.items():
                if v is None:db.execute('DELETE FROM settings WHERE key=$1',k)
                else:db.execute('UPDATE settings SET value=$2 WHERE key=$1',k,v)

    def pool_mode(self):
        cfg=db.scalar("SELECT value FROM settings WHERE key='proxy'")
        db.execute("UPDATE settings SET value=$1 WHERE key='proxy'",dict(cfg,mode='country_pool'))

    def pool_order(self,client):
        return self.ok(client.post('/api/v1/orders',json=self.api_payload('subscription'),headers={'Idempotency-Key':uid()}))['id']

    def pool_generated(self,country='ph',session=None,lifetime='3600s'):
        return ['geo.iproyal.com:12321:proxy-fixture:password-fixture_country-'+country+'_session-'+(session or uid()[:8])+'_lifetime-'+lifetime]

    def test_pool_secrets_permissions_and_import_original_lifetime(self):
        with self.pool_fixture(),self.api_fixture() as (_,client,_):
            self.assertEqual(client.get('/api/admin/country-pool').status_code,403)
            self.assertEqual(client.put('/api/admin/country-pool',json={'token':'x'}).status_code,403)
            for path in ('/api/admin/country-pool','/api/admin/settings','/api/public','/api/v1/catalog'):
                response=self.client.get(path)
                self.assertNotIn('token-fixture',response.text);self.assertNotIn('password-fixture',response.text)
                self.assertNotIn('token_enc',response.text);self.assertNotIn('password_enc',response.text)
            proxy,values=self.proxy()
            self.ok(self.client.put('/api/admin/resources/'+proxy,json=dict(values,vendor='iproyal',ip_mode='dynamic',endpoint='geo.iproyal.com:12321:original:base_country-ph_session-abcd1234_lifetime-15m')))
            self.ok(self.client.put('/api/admin/country-pool',json={'import_resource_id':proxy,'token':''}))
            cfg=pool.settings();self.assertEqual(decrypt(cfg['password_enc']),'base');self.assertEqual(cfg['lifetime_seconds'],900)
            self.assertEqual(decrypt(cfg['token_enc']),'token-fixture');self.assertEqual(cfg['countries'],[])
            self.assertIn('_country-ph',decrypt(db.scalar('SELECT endpoint_enc FROM resources WHERE id=$1',proxy)))
            self.assertEqual(self.client.put('/api/admin/country-pool',json={'token_enc':'attacker'}).status_code,400)
            self.assertEqual(self.client.put('/api/admin/country-pool',json={'lifetime_seconds':0}).status_code,400)

    def test_pool_country_sync_merge_disable_deletion_and_failure(self):
        with self.pool_fixture():
            existing=regions.items();existing[0].update(name='菲律宾自定义',currency='USD',enabled=False)
            db.execute("UPDATE settings SET value=$1 WHERE key='regions'",{'items':existing})
            response={'countries':[{'code':'ph','name':'Philippines'},{'code':'br','name':'Brazil'}]}
            with patch.object(pool,'api',return_value=response):
                first=self.ok(self.client.post('/api/admin/country-pool/sync',json={}))
                self.assertEqual(first['added'],1)
                rows={r['code']:r for r in regions.items()}
                self.assertEqual(rows['PH']['name'],'菲律宾自定义');self.assertEqual(rows['PH']['currency'],'USD');self.assertFalse(rows['PH']['enabled'])
                self.assertEqual(rows['BR']['currency'],'BRL');self.assertFalse(rows['BR']['enabled'])
                self.ok(self.client.delete('/api/admin/regions/BR'))
                self.assertEqual(self.ok(self.client.post('/api/admin/country-pool/sync',json={}))['added'],0)
                self.assertNotIn('BR',{r['code'] for r in regions.items()})
            saved=pool.settings()['countries']
            with patch.object(pool,'api',side_effect=pool.PoolError('pool_unavailable')):
                self.assertEqual(self.client.post('/api/admin/country-pool/sync',json={}).status_code,502)
            self.assertEqual(pool.settings()['countries'],saved)

    def test_pool_catalog_flow_filter_and_batch_with_currency_validation(self):
        with self.pool_fixture(),self.api_fixture() as (_,client,_):
            self.pool_mode()
            catalog=self.ok(client.get('/api/v1/catalog'))
            self.assertEqual({r['code'] for r in catalog['countries']},{'PH','US'})
            self.assertEqual(self.ok(client.get('/api/public'))['countries'],catalog['countries'])
            result=self.ok(self.client.post('/api/admin/regions/batch',json={'codes':['PH','JP'],'enabled':True}))
            self.assertEqual(result['changed'],1);self.assertEqual(result['skipped'][0]['code'],'JP')
            row=deepcopy(regions.items()[0]);row['currency']='ZZZ'
            self.assertEqual(self.client.put('/api/admin/regions/PH',json=row).status_code,400)
            self.assertEqual(client.post('/api/v1/orders',json=dict(self.api_payload(),country='JP'),headers={'Idempotency-Key':uid()}).status_code,400)

    def test_pool_same_order_concurrent_acquisition_and_retry_preserve_expiry(self):
        with self.pool_fixture(),self.api_fixture() as (_,client,_):
            self.pool_mode();order=self.pool_order(client)
            with patch.object(pool,'api',return_value=self.pool_generated()) as api:
                with ThreadPoolExecutor(max_workers=3) as executor:
                    values=list(executor.map(lambda _:flow._acquire_order_proxy(order,'PH',600),range(3)))
                self.assertEqual(len(set(values)),1);self.assertEqual(api.call_count,1)
                before=db.one('SELECT * FROM order_proxy_sessions WHERE order_id=$1',order)
                self.assertEqual(flow._acquire_order_proxy(order,'PH',600),values[0])
                self.assertEqual(before,db.one('SELECT * FROM order_proxy_sessions WHERE order_id=$1',order))
                args=api.call_args.args[2]
                self.assertEqual(args['rotation'],'sticky');self.assertEqual(args['location'],'_country-ph');self.assertEqual(args['lifetime'],'1h')
                db.execute("UPDATE order_proxy_sessions SET expires_at=now()-interval '1 second' WHERE order_id=$1",order)
                with self.assertRaisesRegex(RuntimeError,'proxy_session_expired'):flow._acquire_order_proxy(order,'PH',600)
                self.assertEqual(api.call_count,1)

    def test_pool_distinct_orders_never_share_supplier_session(self):
        with self.pool_fixture(),self.api_fixture() as (_,client,_):
            self.pool_mode();first=self.pool_order(client);second=self.pool_order(client)
            session=uid()[:8];other=uid()[:8]
            with patch.object(pool,'api',side_effect=[self.pool_generated(session=session),self.pool_generated(session=session),self.pool_generated(session=other)]) as api:
                a=flow._acquire_order_proxy(first,'PH',600);b=flow._acquire_order_proxy(second,'PH',600)
                self.assertNotEqual(a,b);self.assertEqual(api.call_count,3)
                self.assertEqual(db.scalar('SELECT session_ref FROM order_proxy_sessions WHERE order_id=$1',second),other)

    def test_pool_invalid_response_cannot_fallback_to_manual(self):
        with self.pool_fixture(),self.api_fixture() as (_,client,_):
            self.pool_mode();order=self.pool_order(client)
            for generated in (self.pool_generated(country='us'),self.pool_generated(lifetime='10m'),['http://127.0.0.1:12321'],['geo.iproyal.com:12321:u:p_country-ph']):
                with patch.object(pool,'api',return_value=generated):
                    with self.assertRaises(Exception):flow._acquire_order_proxy(order,'PH',600)
                self.assertIsNone(db.one('SELECT * FROM order_proxy_sessions WHERE order_id=$1',order))
            with patch.object(pool,'api',side_effect=pool.PoolError('pool_auth_failed')):
                with self.assertRaisesRegex(RuntimeError,'pool_auth_failed'):flow._acquire_order_proxy(order,'PH',600)

    def test_pool_capacity_requeues_without_using_payment_retry_budget(self):
        from backend import subscription_retry
        with self.pool_fixture() as cfg,self.api_fixture() as (_,client,_):
            cfg['max_parallel']=1
            with db.transaction() as q:pool.write(q,cfg)
            self.pool_mode();first=self.pool_order(client);second=self.pool_order(client)
            with patch.object(pool,'api',return_value=self.pool_generated()):flow._acquire_order_proxy(first,'PH',600)
            db.execute("UPDATE orders SET status='processing' WHERE id=$1",second)
            with patch.object(pool,'api') as api:
                with self.assertRaisesRegex(RuntimeError,'pool_capacity'):flow._acquire_order_proxy(second,'PH',600)
                api.assert_not_called()
            self.assertTrue(subscription_retry.handle_failure(second,pool.PoolError('pool_capacity')))
            self.assertEqual(db.scalar('SELECT status FROM orders WHERE id=$1',second),'queued')
            self.assertEqual(db.scalar('SELECT network_retry_count FROM subscription_runs WHERE order_id=$1',second),0)

    def test_pool_mode_switch_guard_and_generic_settings_cannot_bypass(self):
        with self.pool_fixture(),self.api_fixture() as (_,client,_):
            self.pool_mode();order=self.pool_order(client)
            with patch.object(pool,'api',return_value=self.pool_generated()):flow._acquire_order_proxy(order,'PH',600)
            self.assertEqual(self.client.put('/api/admin/proxy-mode',json={'mode':'manual'}).status_code,409)
            cfg=self.ok(self.client.get('/api/admin/settings'))['proxy']
            self.assertEqual(self.client.put('/api/admin/settings/proxy',json=dict(cfg,mode='manual')).status_code,409)
            self.assertEqual(pool.mode(),'country_pool')
            # Forced out-of-band changes must never cause silent cross-mode reuse.
            db.execute("UPDATE settings SET value=$1 WHERE key='proxy'",dict(cfg,mode='manual'))
            with self.assertRaisesRegex(RuntimeError,'proxy_mode_changed'):flow._acquire_order_proxy(order,'PH',600)

    def test_pool_admin_order_metadata_is_redacted(self):
        with self.pool_fixture(),self.api_fixture() as (_,client,_):
            self.pool_mode();order=self.pool_order(client);session=uid()[:8]
            with patch.object(pool,'api',return_value=self.pool_generated(session=session)):flow._acquire_order_proxy(order,'PH',600)
            admin=self.ok(self.client.get('/api/admin/orders/'+order));customer=self.ok(client.get('/api/v1/orders/'+order))
            self.assertEqual(admin['proxy']['source_mode'],'country_pool');self.assertNotIn('proxy',customer)
            self.assertNotIn(session,json_string(admin));self.assertNotIn('password-fixture',json_string(admin))

    def test_manual_iproyal_rewrites_country_and_admin_sees_address(self):
        from backend import subscription,subscription_cslive_full
        from urllib.parse import urlsplit,unquote
        with self.pool_fixture(),self.api_fixture() as (_,client,_):
            proxy,values=self.proxy()
            values.update(vendor='iproyal',ip_mode='dynamic',capacity=10,endpoint='geo.iproyal.com:12321:fixture:base_country-us_session-original_lifetime-15m')
            self.ok(self.client.put('/api/admin/resources/'+proxy,json=values))
            original=db.scalar("SELECT value FROM settings WHERE key='proxy'")
            db.execute("UPDATE settings SET value=$1 WHERE key='proxy'",dict(original,mode='manual',default_id=proxy))
            listed=self.ok(self.client.get('/api/admin/resources'))['items']
            self.assertIn('base_country-us',next(r['endpoint'] for r in listed if r['id']==proxy))
            self.assertEqual(client.get('/api/admin/resources').status_code,403)
            for module in (flow,subscription,subscription_cslive_full):
                order=self.pool_order(client)
                endpoint=module._acquire_order_proxy(order,'PH',600)
                password=unquote(urlsplit(endpoint).password)
                self.assertIn('_country-ph',password);self.assertNotIn('_country-us',password)
                self.assertIn('_lifetime-15m',password);self.assertNotIn('_session-original',password)
                self.assertEqual(module._acquire_order_proxy(order,'PH',600),endpoint)
            self.assertIn('base_country-us',decrypt(db.scalar('SELECT endpoint_enc FROM resources WHERE id=$1',proxy)))

    def test_pool_renewal_task_keeps_session_across_checks_and_expiry(self):
        with self.pool_fixture(),self.api_fixture() as (_,client,_):
            self.pool_mode()
            task=self.ok(client.post('/api/v1/renewals',json={'session':self.fake_session(refresh=True),'accepted':True},headers={'Idempotency-Key':uid()}))['id']
            with patch.object(pool,'api',return_value=self.pool_generated()) as api:
                with db.transaction() as q:first=pool.acquire_renewal(q,task)
                before=db.one('SELECT * FROM renewal_proxy_sessions WHERE task_id=$1',task)
                with db.transaction() as q:self.assertEqual(pool.acquire_renewal(q,task),first)
                self.assertEqual(api.call_count,1)
                self.assertEqual(before,db.one('SELECT * FROM renewal_proxy_sessions WHERE task_id=$1',task))
                db.execute("UPDATE renewal_proxy_sessions SET expires_at=now()-interval '1 second' WHERE task_id=$1",task)
                with db.transaction() as q:
                    with self.assertRaisesRegex(RuntimeError,'proxy_session_expired'):pool.acquire_renewal(q,task)
            db.execute("UPDATE renewal_tasks SET status='disabled' WHERE id=$1",task)


def json_string(value):
    import json
    return json.dumps(value)
