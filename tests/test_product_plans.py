"""New products use only the chosen Go/HTTP flow; no live checkout or payment."""
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

from backend import db
from backend.common import uid
from backend.seed import seed


class ProductPlanTests:
    def test_new_product_seed_is_repeatable_and_preserves_admin_pricing(self):
        rows=db.all("SELECT * FROM products WHERE id IN ('plus','promax') ORDER BY id")
        self.assertEqual([r['id'] for r in rows],['plus','promax'])
        self.assertTrue(all(r['fee']==10000 and r['enabled'] for r in rows))
        try:
            db.execute("UPDATE products SET fee=1234,enabled=false,name='Custom Plus' WHERE id='plus'")
            before=db.all('SELECT * FROM products ORDER BY id')
            settings=db.all('SELECT * FROM settings ORDER BY key')
            seed();seed()
            self.assertEqual(db.all('SELECT * FROM products ORDER BY id'),before)
            self.assertEqual(db.all('SELECT * FROM settings ORDER BY key'),settings)
        finally:
            row=rows[0]
            db.execute('UPDATE products SET fee=$2,enabled=$3,name=$4 WHERE id=$1',row['id'],row['fee'],row['enabled'],row['name'])

    def test_new_product_catalog_quote_and_flow_boundary(self):
        with self.api_fixture() as (owner,client,_):
            cfg=db.scalar("SELECT value FROM settings WHERE key='payment'")
            for flow in ('gener_direct_pay','cslive_full_go','cslive_go_direct_pay'):
                db.execute("UPDATE settings SET value=$1 WHERE key='payment'",dict(cfg,subscription_flow=flow))
                allowed=flow=='cslive_go_direct_pay'
                for path,field in [('/api/public','products'),('/api/v1/catalog','items'),('/api/v1/membership','products')]:
                    ids={p['id'] for p in self.ok(client.get(path))[field]}
                    for product in ('plus','promax'):self.assertEqual(product in ids,allowed)
                for product in ('plus','promax'):
                    quote=client.get('/api/v1/quote',params={'product_id':product})
                    if allowed:self.assertEqual(self.ok(quote)['fee'],10000)
                    else:self.assertEqual(quote.status_code,400)
                    if not allowed:
                        before=db.one('SELECT balance,frozen FROM users WHERE id=$1',owner)
                        count=db.scalar('SELECT count(*) FROM orders WHERE user_id=$1',owner)
                        for workflow in ('subscription','link'):
                            payload=dict(self.api_payload(workflow),product_id=product)
                            self.assertEqual(client.post('/api/v1/orders',json=payload,headers={'Idempotency-Key':uid()}).status_code,400)
                        self.assertEqual(db.one('SELECT balance,frozen FROM users WHERE id=$1',owner),before)
                        self.assertEqual(db.scalar('SELECT count(*) FROM orders WHERE user_id=$1',owner),count)
                # Direct web routes cannot bypass the flow boundary, including administrators.
                for route in ('/api/orders','/api/cslive-full/orders'):
                    for product in ('plus','promax'):
                        payload=dict(self.api_payload(),product_id=product)
                        self.assertEqual(self.client.post(route,json=payload,headers={'Idempotency-Key':uid()}).status_code,400)

    def test_new_products_api_web_order_and_replay_use_go_http(self):
        with self.api_fixture() as (owner,client,_):
            cfg=db.scalar("SELECT value FROM settings WHERE key='payment'")
            target=dict(cfg,subscription_flow='cslive_go_direct_pay')
            db.execute("UPDATE settings SET value=$1 WHERE key='payment'",target)
            # Web routes require a cookie, not an API key. Use a disposable session
            # for the same fixture owner so wallet assertions cover both entries.
            from backend.common import digest
            from fastapi.testclient import TestClient
            token=uid();session_hash=digest(token)
            db.execute("INSERT INTO sessions(hash,user_id,expires_at) VALUES($1,$2,now()+interval '5 minutes')",session_hash,owner)
            self.addCleanup(db.execute,'DELETE FROM sessions WHERE hash=$1',session_hash)
            web=TestClient(self.client.app,cookies={'xupay_session':token});self.addCleanup(web.close)
            for product in ('plus','promax'):
                for route in ('/api/v1/orders','/api/cslive-go/orders'):
                    caller=client if route=='/api/v1/orders' else web
                    for workflow in ('link','subscription'):
                        payload=dict(self.api_payload(workflow),product_id=product)
                        headers={'Idempotency-Key':uid()}
                        first=self.ok(caller.post(route,json=payload,headers=headers))
                        oid=first['id']
                        row=db.one('SELECT o.product_id,o.fee,o.keep_renewal,s.subscription_flow FROM orders o JOIN subscription_runs s ON s.order_id=o.id WHERE o.id=$1',oid)
                        self.assertEqual(row,dict(product_id=product,fee=10000 if workflow=='subscription' else 0,keep_renewal=False,subscription_flow='cslive_go_direct_pay'))
                        frozen=db.scalar('SELECT frozen FROM users WHERE id=$1',owner)
                        self.assertEqual(self.ok(caller.post(route,json=payload,headers=headers))['id'],oid)
                        self.assertEqual(db.scalar('SELECT frozen FROM users WHERE id=$1',owner),frozen)
                        self.assertEqual(self.ok(client.get('/api/v1/orders/'+oid))['order']['product_id'],product)
                        if route=='/api/v1/orders':
                            db.execute("UPDATE settings SET value=$1 WHERE key='payment'",dict(cfg,subscription_flow='gener_direct_pay'))
                            self.assertTrue(self.ok(client.post(route,json=payload,headers=headers))['replayed'])
                            db.execute("UPDATE settings SET value=$1 WHERE key='payment'",target)
                        db.execute("UPDATE orders SET status='failed' WHERE id=$1",oid)
            db.execute("UPDATE products SET enabled=false WHERE id='promax'")
            try:
                self.assertNotIn('promax',{p['id'] for p in self.ok(client.get('/api/v1/catalog'))['items']})
                self.assertEqual(client.get('/api/v1/quote?product_id=promax').status_code,404)
                self.assertEqual(client.post('/api/v1/orders',json=dict(self.api_payload(),product_id='promax'),headers={'Idempotency-Key':uid()}).status_code,404)
            finally:db.execute("UPDATE products SET enabled=true WHERE id='promax'")

    def test_new_product_checkout_setting_keeps_legacy_and_custom_values(self):
        original=db.scalar("SELECT value FROM settings WHERE key='payment'")
        try:
            legacy=deepcopy(original);legacy['subscription_flow']='cslive_go_direct_pay'
            legacy['checkout_types'].pop('promax',None)
            legacy['checkout_types']['plus']='oaics'
            db.execute("UPDATE settings SET value=$1 WHERE key='payment'",legacy)
            shown=self.ok(self.client.get('/api/admin/settings'))['payment']
            self.assertEqual(shown['checkout_types']['promax'],'hosted')
            self.assertEqual(shown['checkout_types']['plus'],'oaics')
            self.assertEqual(db.scalar("SELECT value FROM settings WHERE key='payment'"),legacy)
            self.ok(self.client.put('/api/admin/settings/payment',json=legacy))
            shown=self.ok(self.client.get('/api/admin/settings'))['payment']
            shown['checkout_types']['promax']='oaics'
            self.ok(self.client.put('/api/admin/settings/payment',json=shown))
            self.ok(self.client.put('/api/admin/settings/payment',json=legacy))
            self.assertEqual(self.ok(self.client.get('/api/admin/settings'))['payment']['checkout_types']['promax'],'oaics')
            invalid=deepcopy(shown);invalid['checkout_types']['promax']='invalid'
            self.assertEqual(self.client.put('/api/admin/settings/payment',json=invalid).status_code,400)
        finally:db.execute("UPDATE settings SET value=$1 WHERE key='payment'",original)

    def test_new_product_go_checkout_arguments_and_followup_plan(self):
        from backend import cslive_go_checkout as checkout,cslive_go_post_payment as followup
        from backend.subscription_cslive_go import _resolve_checkout_type
        for product,upstream in [('plus','chatgptplusplan'),('promax','chatgptpromax')]:
            self.assertEqual(checkout._normalize_plan(product),product)
            self.assertEqual(followup.canonical_plan(product),product)
            self.assertIn(upstream,followup.PLAN_TYPES[product])
            self.assertNotIn('chatgptpro',followup.PLAN_TYPES[product])
            for mode in ('hosted','oaics'):
                cfg={'checkout_type':'hosted','checkout_types':{product:mode}}
                self.assertEqual(_resolve_checkout_type(cfg,product),mode)
                result=Mock(returncode=0,stdout=b'{"checkout_url":"https://pay.openai.com/c/pay/cs_fixture","checkout_id":"cs_fixture"}',stderr=b'')
                with patch.object(Path,'is_file',return_value=True),patch.object(checkout.subprocess,'run',return_value=result) as run:
                    checkout.create_checkout(session={'access_token':'fixture'},plan=product,country='US',proxy_url='http://fixture-proxy:8080',checkout_type=mode)
                    args=run.call_args.args[0]
                    self.assertEqual(args[args.index('--plan')+1],product)
                    self.assertEqual(args[args.index('--checkout-type')+1],mode)
                    self.assertEqual(args[args.index('--proxy')+1],'http://fixture-proxy:8080')
                    self.assertNotIn('--seats',args)
