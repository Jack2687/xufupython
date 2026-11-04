"""Behavioral checks against the backend runner's isolated database."""
import asyncio
import json
import os
import select
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
import psycopg2
from fastapi.testclient import TestClient
from starlette.requests import Request
from backend import db
from backend.common import uid, digest, encrypt, now


class ExperienceTests:
    def test_recharge_redemption_history_and_balance_deltas(self):
        owner,_,user=self.new_user()
        other,_,outsider=self.new_user()
        code='XP-'+uid().upper();rid=uid()
        db.execute("INSERT INTO redeem_codes(id,code_hash,code_enc,prefix,amount,batch,expires_at) VALUES($1,$2,$3,$4,100000,'fixture',now()+interval '1 day')",rid,digest(code),encrypt(code),'XP-***'+code[-4:])
        self.ok(user.post('/api/wallet/redeem',json={'code':code}))
        self.assertEqual(user.post('/api/wallet/redeem',json={'code':code}).status_code,400)
        wallet=self.ok(user.get('/api/wallet'));row=wallet['recharges'][0]
        self.assertEqual((row['source'],row['amount'],row['available_delta'],row['frozen_delta'],row['balance_after']),('redeem',100000,100000,0,100000))
        self.assertNotIn(code,json.dumps(wallet));self.assertEqual(db.scalar('SELECT count(*) FROM ledger WHERE user_id=$1',owner),1)
        self.assertFalse(self.ok(outsider.get('/api/wallet'))['recharges'])
        tid=self.ok(user.post('/api/topups',json={'amount':200,'proof':'fixture'}))['id']
        self.assertEqual(next(r for r in self.ok(user.get('/api/wallet'))['recharges'] if r['topup_id']==tid)['balance_after'],None)
        self.ok(self.client.post('/api/admin/topups/'+tid+'/review',json={'action':'approve','note':'已核实测试到账'}))
        r=next(r for r in self.ok(user.get('/api/wallet'))['recharges'] if r['topup_id']==tid)
        self.assertEqual((r['available_delta'],r['frozen_delta'],r['balance_after']),(200,0,100200))
        # Realistic freeze/settlement history: total decreases only at settlement.
        oid=uid();db.execute("INSERT INTO orders(id,user_id,product_id,product_name,country,email,fee,status,idempotency_key,request_hash) VALUES($1,$2,(SELECT id FROM products ORDER BY id LIMIT 1),'Plus','US','fixture@example.test',80,'succeeded',$1,'fixture')",oid,owner)
        for kind,amount,balance,frozen in [('freeze',0,100200,80),('charge',-80,100120,0),('freeze',0,100120,80),('unfreeze',0,100120,0)]:
            db.execute("INSERT INTO ledger(user_id,kind,amount,balance_after,frozen_after,order_id,reference,description) VALUES($1,$2,$3,$4,$5,$6,$7,'fixture')",owner,kind,amount,balance,frozen,oid,uid())
        rows=self.ok(user.get('/api/wallet'))['ledger'][:4]
        self.assertEqual([(r['available_delta'],r['frozen_delta']) for r in rows],[(80,-80),(-80,80),(0,-80),(-80,80)])
        db.execute('UPDATE orders SET deleted_at=now() WHERE id=$1',oid)
        self.assertTrue(all(r['related_order'] is None and not r['payment'] for r in self.ok(user.get('/api/wallet'))['ledger'][:4]))

    def test_every_admin_endpoint_rejects_members_and_api_keys(self):
        from backend.main import app
        _,_,member=self.new_user()
        token=self.ok(member.post('/api/keys',json={'name':'permission audit'}))['key']
        anonymous=TestClient(app);key_client=TestClient(app,headers={'Authorization':'Bearer '+token})
        # Newer FastAPI versions retain included routers instead of flattening
        # their routes at registration. Walk both representations without
        # silently omitting any management endpoint from the permission audit.
        def routes(router):
            for route in router.routes:
                if hasattr(route,'original_router'):yield from routes(route.original_router)
                else:yield route
        checked=0
        for route in routes(app):
            if not route.path.startswith('/api/admin/'):continue
            path=route.path
            for param in route.param_convertors:path=path.replace('{'+param+'}','1' if param=='id' and '/risk/' in path else 'fixture')
            for method in route.methods-{'HEAD','OPTIONS'}:
                with self.subTest(method=method,path=path):
                    self.assertEqual(anonymous.request(method,path,json={}).status_code,401)
                    self.assertEqual(member.request(method,path,json={}).status_code,403)
                    self.assertEqual(key_client.request(method,path,json={}).status_code,403)
                    checked+=1
        self.assertGreater(checked,40)
        self.assertEqual(key_client.get('/api/order-updates').status_code,403)

    def test_flow_switches_defaults_persistence_validation_and_scope(self):
        from backend import post_payment as gener,cslive_go_post_payment as cslive
        original=db.scalar("SELECT value FROM settings WHERE key='payment'")
        keys=('cslive_go_visit_return','cslive_go_refresh_before_renewal')
        legacy={k:v for k,v in original.items() if k not in keys}
        try:
            db.execute("UPDATE settings SET value=$1 WHERE key='payment'",legacy)
            cfg=self.ok(self.client.get('/api/admin/settings'))['payment']
            self.assertTrue(all(cfg[k] is True for k in keys))
            self.ok(self.client.put('/api/admin/settings/payment',json=dict(legacy,**{k:False for k in keys})))
            self.ok(self.client.put('/api/admin/settings/payment',json=legacy))
            self.assertTrue(all(self.ok(self.client.get('/api/admin/settings'))['payment'][k] is False for k in keys))
            for key in keys:
                for value in (None,0,'false'):
                    self.assertEqual(self.client.put('/api/admin/settings/payment',json=dict(legacy,**{key:value})).status_code,400)
            for module,worker,flow in [(gener,'backend.go_post_payment.execute','gener_direct_pay'),(cslive,'backend.cslive_go_post_payment.execute','cslive_go_direct_pay')]:
                oid,_=self.paid_followup_order();db.execute('UPDATE subscription_runs SET subscription_flow=$2 WHERE order_id=$1',oid,flow)
                def complete(cp,save):
                    if flow=='cslive_go_direct_pay':self.assertEqual((cp['visit_merchant_return'],cp['refresh_before_renewal']),(False,False))
                    else:self.assertNotIn('visit_merchant_return',cp);self.assertNotIn('refresh_before_renewal',cp)
                    cp.update(activation='active',renewal='off');save('completed')
                with patch(worker,side_effect=complete):self.assertEqual(module._run(oid,plan='pro5x')['state'],'completed')
        finally:db.execute("UPDATE settings SET value=$1 WHERE key='payment'",original)

    def test_announcement_visibility(self):
        _,_,user=self.new_user();ids=[]
        for name,published,start,end in [('active',True,None,None),('future',True,now()+timedelta(days=1),None),('expired',True,None,now()-timedelta(days=1)),('draft',False,None,None)]:
            aid=uid();ids.append(aid)
            db.execute("INSERT INTO announcements(id,title,body,tone,published,login_visible,starts_at,ends_at) VALUES($1,$2,'fixture','info',$3,false,$4,$5)",aid,name,published,start,end)
        try:
            visible={r['id'] for r in self.ok(user.get('/api/announcements'))['items']}
            self.assertEqual(visible.intersection(ids),{ids[0]})
        finally:
            for aid in ids:db.execute('DELETE FROM announcements WHERE id=$1',aid)

    def test_risk_grouping_search_pagination_and_resolution(self):
        from backend.common import event
        owner,email,_=self.new_user();other,_,_=self.new_user()
        request=SimpleNamespace(state=SimpleNamespace(user={'id':owner}),client=SimpleNamespace(host='203.0.113.93'))
        for _ in range(3):event(request,'fixture_test','medium','检索测试')
        request.state.user={'id':other};event(request,'fixture_test','medium','另一用户')
        result=self.ok(self.client.get('/api/admin/risk',params={'q':email,'category':'fixture_test','state':'pending','page_size':1}))
        self.assertEqual(result['total'],1);self.assertEqual(result['items'][0]['occurrences'],3)
        self.assertEqual(len(self.ok(self.client.get('/api/admin/risk',params={'category':'fixture_test','page_size':1,'page':2}))['items']),1)
        self.ok(self.client.post('/api/admin/risk/'+str(result['items'][0]['id'])+'/resolve',json={}))
        self.assertEqual(self.ok(self.client.get('/api/admin/risk',params={'q':email,'state':'pending'}))['total'],0)
        for params in [{'start':'invalid'},{'start':'2026-11-05','end':'2026-11-04'},{'severity':'invalid'}]:self.assertEqual(self.client.get('/api/admin/risk',params=params).status_code,400)

    def test_order_notifications_commit_only_and_session_isolation(self):
        from backend import order_updates as updates
        owner,_,user=self.new_user();oid=uid()
        with psycopg2.connect(os.environ['DATABASE_URL']) as conn:
            conn.autocommit=True
            with conn.cursor() as cur:cur.execute('LISTEN xufu_order_updates')
            conn.commit()
            try:
                with db.transaction() as q:
                    q.execute("INSERT INTO orders(id,user_id,product_id,product_name,country,email,fee,status,idempotency_key,request_hash) VALUES($1,$2,(SELECT id FROM products ORDER BY id LIMIT 1),'Plus','US','fixture@example.test',0,'draft',$1,'fixture')",oid,owner)
                    raise ValueError('rollback')
            except ValueError:pass
            self.assertFalse(select.select([conn],[],[],0.1)[0])
            db.execute("INSERT INTO orders(id,user_id,product_id,product_name,country,email,fee,status,idempotency_key,request_hash) VALUES($1,$2,(SELECT id FROM products ORDER BY id LIMIT 1),'Plus','US','fixture@example.test',0,'draft',$1,'fixture')",oid,owner)
            self.assertTrue(select.select([conn],[],[],2)[0]);conn.poll()
            self.assertEqual(json.loads(conn.notifies[-1].payload),{'id':oid,'user_id':owner})
        token=user.cookies.get('xupay_session')
        async def receive():return {'type':'http.request','body':b'','more_body':False}
        request=Request({'type':'http','method':'GET','path':'/api/order-updates','headers':[(b'cookie',('xupay_session='+token).encode())],'client':('testclient',1),'query_string':b''},receive)
        async def exercise():
            broker=updates.OrderUpdates()
            with patch.object(updates,'updates',broker):
                response=await updates.order_updates(request);stream=response.body_iterator
                self.assertIn('ready',await anext(stream))
                broker.publish({'id':'foreign','user_id':'other'});broker.publish({'id':oid,'user_id':owner})
                result=await asyncio.wait_for(anext(stream),2)
                self.assertIn(oid,result);self.assertNotIn('foreign',result);self.assertNotIn(owner,result)
                db.execute('DELETE FROM sessions WHERE hash=$1',digest(token));broker.publish({'resync':True})
                self.assertIn('revoked',await anext(stream));await stream.aclose();self.assertFalse(broker.clients)
        asyncio.run(exercise())
