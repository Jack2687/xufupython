"""API contracts exercised by the disposable-database backend test runner."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

from fastapi.testclient import TestClient
from backend import db
from backend.common import uid


class CustomerApiTests:
    @contextmanager
    def api_fixture(self):
        saved={key:db.scalar('SELECT value FROM settings WHERE key=$1',key)
               for key in ('payment','billing','risk')}
        user_id,_,web=self.new_user()
        other_id,_,other=self.new_user()
        key=self.ok(web.post('/api/keys',json={'name':'API regression'}))['key']
        other_key=self.ok(other.post('/api/keys',json={'name':'Other account'}))['key']
        client=TestClient(self.client.app,headers={'Authorization':'Bearer '+key})
        stranger=TestClient(self.client.app,headers={'Authorization':'Bearer '+other_key})
        profile={'id':'bp_api_test','name':'测试账单','first_name':'Fixture','last_name':'User',
                 'email':'billing@example.test','state':'NH','city':'Concord','line1':'1 Example Street',
                 'postal_code':'03301','enabled':True,'test_only':False}
        try:
            db.execute('UPDATE settings SET value=$2 WHERE key=$1','payment',dict(saved['payment'],enabled=True,subscription_flow='gener_direct_pay'))
            db.execute('UPDATE settings SET value=$2 WHERE key=$1','billing',dict(saved['billing'],country='US',address_templates=[profile]))
            db.execute('UPDATE settings SET value=$2 WHERE key=$1','risk',dict(saved['risk'],order_per_minute=100,api_per_minute=10000))
            self.ok(self.client.post('/api/admin/users/'+user_id+'/balance',json={'amount':10000000,'reason':'API fixture'},headers={'Idempotency-Key':uid()}))
            yield user_id,client,stranger
        finally:
            db.execute("UPDATE orders SET status='failed' WHERE user_id=$1",user_id)
            for key,value in saved.items():db.execute('UPDATE settings SET value=$2 WHERE key=$1',key,value)
            client.close();stranger.close();web.close();other.close()

    def api_payload(self,workflow='link'):
        payload={'workflow':workflow,'product_id':db.scalar('SELECT id FROM products WHERE enabled ORDER BY sort LIMIT 1'),
                 'session':self.fake_session(refresh=True),'country':'PH'}
        if workflow=='subscription':
            payload.update(accepted=True,card={'number':'4242424242424242','month':'12','year':'2030','cvc':'123'},keep_renewal=False)
        return payload

    def test_api_create_order_routes_and_replay(self):
        with self.api_fixture() as (owner,client,other):
            for flow in ('gener_direct_pay','cslive_go_direct_pay','cslive_full_go'):
                cfg=db.scalar("SELECT value FROM settings WHERE key='payment'")
                db.execute("UPDATE settings SET value=$1 WHERE key='payment'",dict(cfg,subscription_flow=flow))
                for workflow in ('link','subscription'):
                    payload=self.api_payload(workflow);headers={'Idempotency-Key':uid()}
                    first=self.ok(client.post('/api/v1/orders',json=payload,headers=headers))
                    before=db.one('SELECT balance,frozen FROM users WHERE id=$1',owner)
                    again=self.ok(client.post('/api/v1/orders',json=payload,headers=headers))
                    self.assertEqual(first['id'],again['id']);self.assertTrue(again['replayed'])
                    self.assertEqual(db.one('SELECT balance,frozen FROM users WHERE id=$1',owner),before)
                    row=db.one('SELECT o.source,s.subscription_flow FROM orders o JOIN subscription_runs s ON s.order_id=o.id WHERE o.id=$1',first['id'])
                    self.assertEqual(row,{'source':'api','subscription_flow':flow})
                    self.assertEqual(client.get('/api/v1/orders/'+first['id']).status_code,200)
                    self.assertEqual(other.get('/api/v1/orders/'+first['id']).status_code,404)
                    changed=dict(payload,keep_renewal=True)
                    self.assertEqual(client.post('/api/v1/orders',json=changed,headers=headers).status_code,409)
                    db.execute("UPDATE orders SET status='failed' WHERE id=$1",first['id'])
            cfg=db.scalar("SELECT value FROM settings WHERE key='payment'")
            db.execute("UPDATE settings SET value=$1 WHERE key='payment'",dict(cfg,enabled=False,subscription_flow=''))
            self.assertTrue(self.ok(client.post('/api/v1/orders',json=payload,headers=headers))['replayed'])

    def test_api_concurrent_subscription_freezes_once(self):
        with self.api_fixture() as (owner,client,_):
            payload=self.api_payload('subscription');headers={'Idempotency-Key':uid()}
            before=db.scalar('SELECT frozen FROM users WHERE id=$1',owner)
            with ThreadPoolExecutor(max_workers=4) as pool:
                responses=list(pool.map(lambda _:client.post('/api/v1/orders',json=payload,headers=headers),range(4)))
            values=[self.ok(r) for r in responses]
            self.assertEqual(len({r['id'] for r in values}),1)
            order=values[0]['id'];fee=db.scalar('SELECT fee FROM orders WHERE id=$1',order)
            self.assertEqual(db.scalar('SELECT frozen FROM users WHERE id=$1',owner),before+fee)
            self.assertEqual(db.scalar("SELECT count(*) FROM ledger WHERE order_id=$1 AND kind='freeze'",order),1)
            self.assertEqual(client.post('/api/v1/orders',json=dict(payload,session=self.fake_session()),headers=headers).status_code,409)
            card=dict(payload['card'],cvc='321')
            self.assertEqual(client.post('/api/v1/orders',json=dict(payload,card=card),headers=headers).status_code,409)

    def test_api_validation_and_closed_switch(self):
        with self.api_fixture() as (owner,client,_):
            payload=self.api_payload('subscription');before=db.scalar('SELECT count(*) FROM orders WHERE user_id=$1',owner)
            for delta in ({'accepted':False},{'card':None},{'seats':True},{'premium_seats':3},{'keep_renewal':'false'},{'workflow':'unknown'},{'internal_flow':'anything'}):
                r=client.post('/api/v1/orders',json=dict(payload,**delta),headers={'Idempotency-Key':uid()})
                self.assertEqual(r.status_code,400,r.text)
            self.assertEqual(client.post('/api/v1/orders',json=payload).status_code,400)
            cfg=db.scalar("SELECT value FROM settings WHERE key='payment'")
            db.execute("UPDATE settings SET value=$1 WHERE key='payment'",dict(cfg,enabled=False))
            self.assertEqual(client.post('/api/v1/orders',json=payload,headers={'Idempotency-Key':uid()}).status_code,403)
            self.assertFalse(self.ok(client.get('/api/v1/quote',params={'product_id':payload['product_id']}))['payment_connected'])
            db.execute("UPDATE settings SET value=$1 WHERE key='payment'",cfg)
            self.assertTrue(self.ok(client.get('/api/v1/quote',params={'product_id':payload['product_id']}))['payment_connected'])
            self.assertEqual(db.scalar('SELECT count(*) FROM orders WHERE user_id=$1',owner),before)

    def test_api_renewals_replay_and_isolation(self):
        with self.api_fixture() as (owner,client,other):
            payload={'session':self.fake_session(refresh=True),'accepted':True};headers={'Idempotency-Key':uid()}
            with ThreadPoolExecutor(max_workers=3) as pool:
                results=[self.ok(r) for r in pool.map(lambda _:client.post('/api/v1/renewals',json=payload,headers=headers),range(3))]
            self.assertEqual(len({r['id'] for r in results}),1)
            task=results[0]['id']
            self.assertEqual(self.ok(client.get('/api/v1/renewals/'+task))['task']['status'],'queued')
            self.assertEqual(other.get('/api/v1/renewals/'+task).status_code,404)
            self.assertEqual(self.ok(other.get('/api/v1/renewals'))['total'],0)
            self.assertEqual(client.post('/api/v1/renewals',json={'session':self.fake_session(),'accepted':True},headers=headers).status_code,409)
            db.execute("UPDATE renewal_tasks SET status='disabled' WHERE id=$1",task)
            self.assertEqual(self.ok(client.post('/api/v1/renewals',json=payload,headers=headers))['status'],'disabled')
            cfg=db.scalar("SELECT value FROM settings WHERE key='payment'")
            db.execute("UPDATE settings SET value=$1 WHERE key='payment'",dict(cfg,enabled=False))
            self.assertEqual(client.post('/api/v1/renewals',json=payload,headers={'Idempotency-Key':uid()}).status_code,403)

    def test_api_topup_pagination_and_permissions(self):
        with self.api_fixture() as (owner,client,other):
            payload={'amount':10000,'proof':'fixture'};headers={'Idempotency-Key':uid()}
            with ThreadPoolExecutor(max_workers=3) as pool:
                results=[self.ok(r) for r in pool.map(lambda _:client.post('/api/v1/topups',json=payload,headers=headers),range(3))]
            self.assertEqual(len({r['id'] for r in results}),1);topup=results[0]['id']
            self.assertEqual(client.post('/api/v1/topups',json=dict(payload,amount=20000),headers=headers).status_code,409)
            self.assertEqual(other.post('/api/v1/topups/'+topup+'/cancel').status_code,404)
            for _ in range(2):self.ok(client.post('/api/v1/topups/'+topup+'/cancel'))
            for _ in range(2):self.ok(client.post('/api/v1/topups',json=payload,headers={'Idempotency-Key':uid()}))
            first=self.ok(client.get('/api/v1/topups?page_size=1'));second=self.ok(client.get('/api/v1/topups?page_size=1&page=2'))
            self.assertEqual(first['total'],3);self.assertTrue(first['has_more'])
            self.assertNotEqual(first['items'][0]['id'],second['items'][0]['id'])
            self.assertNotIn('reviewed_by',first['items'][0])
            db.execute("UPDATE topups SET status='approved' WHERE id=$1",first['items'][0]['id'])
            self.assertEqual(client.post('/api/v1/topups/'+first['items'][0]['id']+'/cancel').status_code,409)
            self.assertEqual(self.ok(other.get('/api/v1/topups'))['total'],0)
            self.assertEqual(self.ok(client.get('/api/v1/me'))['id'],owner)
            self.assertEqual(self.ok(client.get('/api/v1/overview'))['user']['id'],owner)
            self.assertGreater(self.ok(client.get('/api/v1/wallet/ledger'))['total'],0)
            self.assertEqual(self.ok(other.get('/api/v1/wallet/ledger'))['total'],0)
            self.assertEqual(client.get('/api/admin/settings').status_code,403)
            for path in ('orders','renewals','topups','wallet/ledger'):
                self.assertEqual(client.get('/api/v1/'+path+'?page_size=0').status_code,400)

    def test_api_order_pages_and_unauthenticated_routes(self):
        with self.api_fixture() as (_,client,other):
            for _ in range(3):self.ok(client.post('/api/v1/orders',json=self.api_payload(),headers={'Idempotency-Key':uid()}))
            pages=[self.ok(client.get('/api/v1/orders?page_size=1&page='+str(i))) for i in (1,2,3)]
            self.assertEqual(len({p['items'][0]['id'] for p in pages}),3)
            self.assertEqual(pages[0]['total'],3);self.assertFalse(pages[-1]['has_more'])
            self.assertEqual(self.ok(client.get('/api/v1/orders?workflow=subscription'))['total'],0)
            self.assertEqual(self.ok(other.get('/api/v1/orders'))['total'],0)
            spec=self.ok(client.get('/api/openapi.json'))
            anonymous=TestClient(self.client.app)
            try:
                for path,methods in spec['paths'].items():
                    for method in methods:
                        result=anonymous.request(method,path.replace('{id}','missing'),json={} if method=='post' else None)
                        self.assertEqual(result.status_code,401,path)
            finally:anonymous.close()
