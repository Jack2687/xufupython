"""Admin mutations tested only on the backend runner's disposable database."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from fastapi.testclient import TestClient
from backend import db
from backend.common import now, uid
from backend.membership import membership_for


class AdminOrderPricingTests:
    def close_fixture_order(self, order):
        return self.client.post('/api/admin/orders/'+order+'/close',
                                json={'reason':'回归测试关闭','accepted':True})

    def set_account_fixture(self, owner, **options):
        row=db.one('SELECT email,name,role,status,fee_override FROM users WHERE id=$1',owner)
        return self.client.patch('/api/admin/users/'+owner,json={**row,**options})

    def test_admin_close_releases_once_and_keeps_other_holds(self):
        with self.api_fixture() as (owner,client,other):
            payload=self.api_payload('subscription')
            orders=[self.ok(client.post('/api/v1/orders',json=payload,headers={'Idempotency-Key':uid()}))['id'] for _ in range(2)]
            order=orders[0];fee=db.scalar('SELECT fee FROM orders WHERE id=$1',order)
            before=db.one('SELECT balance,frozen FROM users WHERE id=$1',owner)
            self.assertEqual(other.post('/api/admin/orders/'+order+'/close',json={'accepted':True,'reason':'拒绝越权'}).status_code,403)
            self.assertEqual(self.client.post('/api/admin/orders/'+order+'/close',json={'reason':'未确认'}).status_code,400)
            db.execute("UPDATE orders SET status='review' WHERE id=$1",order)
            db.execute('UPDATE subscription_runs SET payment_submit_started_at=now() WHERE order_id=$1',order)
            with ThreadPoolExecutor(max_workers=3) as pool:
                results=[self.ok(r) for r in pool.map(self.close_fixture_order,[order]*3)]
            self.assertEqual(sum(bool(r.get('replayed')) for r in results),2)
            self.assertEqual(db.one('SELECT balance,frozen FROM users WHERE id=$1',owner),
                             {'balance':before['balance'],'frozen':before['frozen']-fee})
            self.assertEqual(db.scalar("SELECT count(*) FROM ledger WHERE order_id=$1 AND kind='unfreeze'",order),1)
            self.assertEqual(db.scalar('SELECT count(*) FROM order_closures WHERE order_id=$1',order),1)
            self.assertEqual(db.scalar("SELECT count(*) FROM audit_events WHERE target=$1 AND action='order.closed'",order),1)
            detail=self.ok(self.client.get('/api/admin/orders/'+order))
            self.assertTrue(detail['closures'][0]['payment_submitted'])
            self.assertEqual(detail['payment']['error_code'],'admin_closed')
            self.assertTrue(any(e['code']=='admin_closed' for e in detail['trace']))
            self.assertEqual(self.ok(client.get('/api/v1/orders/'+order))['order']['status'],'cancelled')
            self.assertEqual(db.scalar('SELECT credentials_enc FROM subscription_runs WHERE order_id=$1',order),'')
            self.ok(self.close_fixture_order(orders[1]))
            self.assertEqual(db.scalar('SELECT frozen FROM users WHERE id=$1',owner),0)

    def test_admin_close_rejects_active_or_paid_and_handles_zero_fee(self):
        with self.api_fixture() as (owner,client,_):
            payload=self.api_payload('subscription')
            order=self.ok(client.post('/api/v1/orders',json=payload,headers={'Idempotency-Key':uid()}))['id']
            before=db.one('SELECT balance,frozen FROM users WHERE id=$1',owner)
            db.execute("UPDATE orders SET status='processing' WHERE id=$1",order)
            self.assertEqual(self.close_fixture_order(order).status_code,409)
            db.execute("UPDATE orders SET status='review' WHERE id=$1",order)
            db.execute('UPDATE subscription_runs SET paid_at=now() WHERE order_id=$1',order)
            self.assertEqual(self.close_fixture_order(order).status_code,409)
            self.assertEqual(db.one('SELECT balance,frozen FROM users WHERE id=$1',owner),before)
            db.execute('UPDATE subscription_runs SET paid_at=NULL WHERE order_id=$1',order)
            self.ok(self.close_fixture_order(order))
            self.ok(self.set_account_fixture(owner,product_prices={payload['product_id']:0}))
            zero=self.ok(client.post('/api/v1/orders',json=payload,headers={'Idempotency-Key':uid()}))['id']
            self.assertEqual(self.ok(self.close_fixture_order(zero))['released'],0)
            self.assertEqual(self.ok(self.close_fixture_order(zero))['replayed'],True)
            self.assertEqual(db.scalar('SELECT frozen FROM users WHERE id=$1',owner),0)

    def test_admin_delete_mixed_states_hidden_from_all_customer_views(self):
        with self.api_fixture() as (owner,client,other):
            payload=self.api_payload('subscription');headers={'Idempotency-Key':uid()}
            order=self.ok(client.post('/api/v1/orders',json=payload,headers=headers))['id']
            active=self.ok(client.post('/api/v1/orders',json=payload,headers={'Idempotency-Key':uid()}))['id']
            self.ok(self.close_fixture_order(order))
            request={'ids':[order,active,order,'missing-order']}
            self.assertEqual(other.post('/api/admin/orders/batch-delete',json=request).status_code,403)
            before=db.one('SELECT balance,frozen FROM users WHERE id=$1',owner)
            self.assertGreater(before['frozen'],0)
            result=self.ok(self.client.post('/api/admin/orders/batch-delete',json=request))
            self.assertEqual(result,{'ok':True,'deleted':2,'released':before['frozen'],'skipped':0})
            self.assertEqual(self.ok(self.client.post('/api/admin/orders/batch-delete',json=request)),
                             {'ok':True,'deleted':0,'released':0,'skipped':0})
            self.assertEqual(client.get('/api/v1/orders/'+order).status_code,404)
            web=TestClient(self.client.app)
            try:
                self.ok(web.post('/api/auth/login',json={'email':db.scalar('SELECT email FROM users WHERE id=$1',owner),'password':'Test-password-123'}))
                self.assertEqual(web.get('/api/orders/'+order).status_code,404)
                self.assertEqual(web.get('/api/subscriptions/'+order).status_code,404)
                self.assertEqual(self.ok(web.get('/api/orders'))['total'],0)
            finally:web.close()
            self.assertEqual(self.client.get('/api/admin/orders/'+order).status_code,404)
            self.assertEqual(self.ok(self.client.get('/api/admin/orders',params={'q':order}))['total'],0)
            self.assertEqual(self.ok(client.get('/api/v1/orders'))['total'],0)
            overview=self.ok(client.get('/api/v1/overview'))
            self.assertEqual(overview['stats']['orders'],0)
            self.assertNotIn(order,[r['id'] for r in overview['recent_orders']])
            self.assertEqual(client.post('/api/v1/orders',json=payload,headers=headers).status_code,409)
            self.assertEqual(db.one('SELECT balance,frozen FROM users WHERE id=$1',owner),dict(before,frozen=0))
            ledger=self.ok(client.get('/api/v1/wallet/ledger'))['items']
            self.assertTrue(any(r['kind']=='unfreeze' and r['related_order'] is None for r in ledger))
            self.assertIsNotNone(db.scalar('SELECT deleted_at FROM orders WHERE id=$1',active))

    def test_admin_delete_keeps_spend_and_skips_only_running_followup(self):
        order,owner=self.paid_followup_order(keep=True)
        db.execute("UPDATE orders SET activation='pending',renewal='pending' WHERE id=$1",order)
        before=membership_for(owner)['spent']
        funds=db.one('SELECT balance,frozen FROM users WHERE id=$1',owner)
        payload={'ids':[order]}
        with db.transaction() as lock:
            for key in (1886005,1886006,1886008):
                lock.execute('SELECT pg_advisory_xact_lock($1)',key)
            for key in (1886011,1886012):
                lock.execute('SELECT pg_advisory_xact_lock($1,hashtext($2))',key,order)
            self.assertEqual(self.ok(self.client.post('/api/admin/orders/batch-delete',json=payload)),
                             {'ok':True,'deleted':0,'released':0,'skipped':1})
            self.assertIsNone(db.scalar('SELECT deleted_at FROM orders WHERE id=$1',order))
        self.assertEqual(self.ok(self.client.post('/api/admin/orders/batch-delete',json=payload)),
                         {'ok':True,'deleted':1,'released':0,'skipped':0})
        self.assertEqual(membership_for(owner)['spent'],before)
        self.assertEqual(db.one('SELECT balance,frozen FROM users WHERE id=$1',owner),funds)
        db.execute("UPDATE orders SET activation='active',renewal='kept' WHERE id=$1",order)
        self.assertEqual(self.client.get('/api/admin/orders/'+order).status_code,404)
        self.assertEqual(self.ok(self.client.post('/api/admin/orders/batch-delete',json=payload))['deleted'],0)

    def test_admin_delete_all_statuses_and_concurrent_repeats(self):
        with self.api_fixture() as (owner,client,_):
            ids=[];states=('draft','queued','processing','awaiting_payment','review','succeeded','failed','cancelled')
            for state in states:
                order=self.ok(client.post('/api/v1/orders',json=self.api_payload('subscription'),headers={'Idempotency-Key':uid()}))['id']
                db.execute('UPDATE orders SET status=$2 WHERE id=$1',order,state)
                ids.append(order)
            funds=db.one('SELECT balance,frozen FROM users WHERE id=$1',owner)
            fee=db.scalar('SELECT fee FROM orders WHERE id=$1',ids[0])
            ledger=db.all('SELECT * FROM ledger WHERE user_id=$1 ORDER BY id',owner)
            payload={'ids':ids}
            with ThreadPoolExecutor(max_workers=3) as pool:
                results=[self.ok(r) for r in pool.map(lambda _:self.client.post('/api/admin/orders/batch-delete',json=payload),range(3))]
            self.assertEqual(sorted(r['deleted'] for r in results),[0,0,len(ids)-1])
            self.assertEqual(sum(r['released'] for r in results),fee*(len(ids)-1))
            self.assertTrue(all(r['skipped']==1 for r in results))
            self.assertEqual(db.scalar('SELECT count(*) FROM orders WHERE id=ANY($1) AND deleted_at IS NOT NULL',ids),len(ids)-1)
            self.assertIsNone(db.scalar('SELECT deleted_at FROM orders WHERE id=$1',ids[2]))
            self.assertEqual(db.one('SELECT balance,frozen FROM users WHERE id=$1',owner),dict(funds,frozen=fee))
            after=db.all('SELECT * FROM ledger WHERE user_id=$1 ORDER BY id',owner)
            self.assertEqual(after[:len(ledger)],ledger)
            self.assertEqual([r['kind'] for r in after[len(ledger):]],['unfreeze']*(len(ids)-1))
            self.assertEqual(self.ok(client.get('/api/v1/orders'))['total'],1)

    def test_admin_deleted_orders_cannot_restart_or_resume(self):
        from backend import subscription,subscription_cslive_go,subscription_cslive_full,subscription_retry,post_payment,cslive_go_post_payment
        from unittest.mock import patch
        with self.api_fixture() as (owner,client,_):
            saved_scheduler=db.scalar("SELECT value FROM settings WHERE key='scheduler'")
            try:
                db.execute("UPDATE settings SET value=$1 WHERE key='scheduler'",dict(saved_scheduler,paused=False))
                for module in (subscription,subscription_cslive_go,subscription_cslive_full):
                    with self.subTest(flow=module.FLOW):
                        cfg=db.scalar("SELECT value FROM settings WHERE key='payment'")
                        db.execute("UPDATE settings SET value=$1 WHERE key='payment'",dict(cfg,subscription_flow=module.FLOW))
                        order=self.ok(client.post('/api/v1/orders',json=self.api_payload('subscription'),headers={'Idempotency-Key':uid()}))['id']
                        result=self.ok(self.client.post('/api/admin/orders/batch-delete',json={'ids':[order]}))
                        self.assertEqual((result['deleted'],result['skipped']),(1,0))
                        self.assertIsNone(module.claim_subscription())
                        db.execute("UPDATE orders SET status='processing' WHERE id=$1",order)
                        self.assertFalse(subscription_retry.can_start(order))
                        with self.assertRaises(RuntimeError):subscription_retry.mark_payment_submission(order)
                        self.assertIsNone(db.scalar('SELECT payment_submit_started_at FROM subscription_runs WHERE order_id=$1',order))
                        self.assertTrue(subscription_retry.handle_failure(order,RuntimeError('network_error')))
                        self.assertEqual(db.scalar('SELECT network_retry_count FROM subscription_runs WHERE order_id=$1',order),0)
                        for followup in (post_payment,cslive_go_post_payment):
                            with patch.object(followup,'_save') as save:
                                with self.assertRaises(followup.FollowupError):followup.resume(order)
                                save.assert_not_called()
            finally:db.execute("UPDATE settings SET value=$1 WHERE key='scheduler'",saved_scheduler)

    def test_account_membership_and_product_price_priority(self):
        with self.api_fixture() as (owner,client,_):
            tier=db.scalar("SELECT value FROM settings WHERE key='tiers'")['items'][-1]
            product=self.api_payload()['product_id'];standard=db.scalar('SELECT fee FROM products WHERE id=$1',product)
            self.ok(self.set_account_fixture(owner,membership_code=tier['code'],membership_until=None,
                                            fee_override=321,product_prices={product:0}))
            quote=self.ok(client.get('/api/v1/quote',params={'product_id':product}))
            self.assertEqual((quote['fee'],quote['price_source']),(0,'product_override'))
            self.assertEqual(quote['membership']['current']['code'],tier['code'])
            self.assertEqual(quote['membership']['source'],'manual')
            self.ok(self.set_account_fixture(owner,product_prices={}))
            quote=self.ok(client.get('/api/v1/quote',params={'product_id':product}))
            self.assertEqual((quote['fee'],quote['price_source']),(321,'account_override'))
            self.ok(self.set_account_fixture(owner,fee_override=None))
            quote=self.ok(client.get('/api/v1/quote',params={'product_id':product}))
            self.assertEqual((quote['fee'],quote['price_source']),((standard*tier['discount']+50)//100,'membership'))
            before=db.one('SELECT name,membership_code FROM users WHERE id=$1',owner)
            for prices in ({product:-1},{product:True},{'unknown':100}):
                self.assertEqual(self.set_account_fixture(owner,name='不应保存',product_prices=prices).status_code,400)
                self.assertEqual(db.one('SELECT name,membership_code FROM users WHERE id=$1',owner),before)
            expired=now()-timedelta(days=1)
            db.execute('UPDATE users SET membership_until=$2 WHERE id=$1',owner,expired)
            self.assertEqual(membership_for(owner)['source'],'automatic')
            self.ok(self.set_account_fixture(owner,membership_code=tier['code'],membership_until=expired.isoformat()))
            self.assertEqual(self.set_account_fixture(owner,membership_until=(expired-timedelta(days=1)).isoformat()).status_code,400)

    def test_pro20x_catalog_quote_and_admission_all_flows(self):
        with self.api_fixture() as (owner,client,_):
            enabled=db.scalar("SELECT enabled FROM products WHERE id='pro'")
            db.execute("UPDATE products SET enabled=true WHERE id='pro'")
            try:
                self.ok(self.set_account_fixture(owner,product_prices={'pro20x':765}))
                catalog=self.ok(client.get('/api/v1/catalog'))
                p=next(p for p in catalog['items'] if p['id']=='pro20x')
                self.assertEqual(p['member_fee'],765);self.assertNotIn('pro',[p['id'] for p in catalog['items']])
                self.assertEqual(self.ok(client.get('/api/v1/quote?product_id=pro20x'))['fee'],765)
                for flow in ('gener_direct_pay','cslive_go_direct_pay','cslive_full_go'):
                    cfg=db.scalar("SELECT value FROM settings WHERE key='payment'")
                    db.execute("UPDATE settings SET value=$1 WHERE key='payment'",dict(cfg,subscription_flow=flow))
                    payload=dict(self.api_payload('subscription'),product_id='pro20x');headers={'Idempotency-Key':uid()}
                    order=self.ok(client.post('/api/v1/orders',json=payload,headers=headers))['id']
                    stored=db.one('SELECT fee,product_id,price_snapshot FROM orders WHERE id=$1',order)
                    self.assertEqual((stored['fee'],stored['product_id']),(765,'pro'))
                    self.assertEqual(stored['price_snapshot']['member_fee'],765)
                    self.assertEqual(self.ok(client.get('/api/v1/orders/'+order))['order']['product_id'],'pro20x')
                    self.assertEqual(self.ok(client.get('/api/v1/orders'))['items'][0]['product_id'],'pro20x')
                    self.assertEqual(self.ok(client.get('/api/v1/overview'))['recent_orders'][0]['product_id'],'pro20x')
                    self.assertEqual(self.ok(client.post('/api/v1/orders',json=dict(payload,product_id='pro'),headers=headers))['id'],order)
                    self.ok(self.close_fixture_order(order))
            finally:db.execute("UPDATE products SET enabled=$1 WHERE id='pro'",enabled)

    def test_dynamic_product_catalog_prices_and_server_search_pages(self):
        with self.api_fixture() as (owner,client,_):
            product='future_'+uid()[:8]
            db.execute('INSERT INTO products(id,name,description,fee,features,enabled,sort) VALUES($1,$2,$3,$4,$5,true,99)',
                       product,'未来套餐','仅测试接口标识',1000,[])
            self.ok(self.set_account_fixture(owner,product_prices={product:432}))
            self.assertEqual(self.ok(client.get('/api/v1/quote',params={'product_id':product}))['fee'],432)
            self.assertIn(product,[p['id'] for p in self.ok(client.get('/api/v1/catalog'))['items']])
            # No upstream execution for fictional plans: only verify dynamic catalog and pricing.
            payload=self.api_payload('link');ids=[]
            for _ in range(3):ids.append(self.ok(client.post('/api/v1/orders',json=payload,headers={'Idempotency-Key':uid()}))['id'])
            email=db.scalar('SELECT email FROM users WHERE id=$1',owner)
            pages=[self.ok(self.client.get('/api/admin/orders',params={'q':email,'page_size':1,'page':i})) for i in (1,2,3)]
            self.assertEqual(pages[0]['total'],3);self.assertFalse(pages[-1]['has_more'])
            self.assertEqual({p['items'][0]['id'] for p in pages},set(ids))
            self.assertEqual(self.ok(self.client.get('/api/admin/orders',params={'q':ids[0]}))['total'],1)
            db.execute('UPDATE products SET enabled=false WHERE id=$1',product)
            self.assertEqual(self.set_account_fixture(owner,product_prices={product:1}).status_code,400)
            self.ok(self.set_account_fixture(owner,product_prices={}))
            self.assertEqual(db.scalar('SELECT fee FROM user_product_prices WHERE user_id=$1 AND product_id=$2',owner,product),432)

    def test_customer_documentation_describes_every_property(self):
        spec=json.loads((Path(__file__).resolve().parents[1]/'backend/openapi.json').read_text(encoding='utf-8'))
        def visit(value):
            if isinstance(value,dict):
                for name,field in value.get('properties',{}).items():
                    self.assertIsInstance(field,dict,name)
                    self.assertTrue(field.get('description','').strip(),name)
                for child in value.values():visit(child)
            elif isinstance(value,list):
                for child in value:visit(child)
        visit(spec)
        self.assertNotIn('enum',spec['components']['schemas']['CreateOrder']['properties']['product_id'])
        example=spec['paths']['/api/v1/orders']['post']['requestBody']['content']['application/json']['examples']['subscription']['value']
        self.assertEqual(example['product_id'],'pro20x')
