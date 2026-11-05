"""Region CRUD and admission checks are offline; existing orders retain currency."""
from copy import deepcopy
from contextlib import contextmanager
from backend import db,regions
from backend.common import uid
from backend.seed import seed


class RegionTests:
    @contextmanager
    def region_fixture(self):
        original=db.scalar("SELECT value FROM settings WHERE key='regions'")
        try:yield
        finally:db.execute("UPDATE settings SET value=$1 WHERE key='regions'",original)

    def test_region_crud_permissions_defaults_and_no_seed_resurrection(self):
        with self.region_fixture(),self.api_fixture() as (_,client,_):
            self.assertEqual(client.get('/api/admin/regions').status_code,403)
            row={'code':'BR','name':'巴西','currency':'BRL','enabled':True,'sort':-5}
            self.ok(self.client.post('/api/admin/regions',json=row))
            self.assertEqual(self.client.post('/api/admin/regions',json=row).status_code,409)
            self.assertEqual(self.client.put('/api/admin/regions/BR',json=dict(row,code='CA')).status_code,400)
            for invalid in ({'code':'Brazil'},{'name':''},{'currency':'x'},{'enabled':'yes'},{'sort':True}):
                self.assertEqual(self.client.put('/api/admin/regions/BR',json=dict(row,**invalid)).status_code,400)
            self.ok(self.client.put('/api/admin/regions/BR',json=dict(row,name='巴西订阅',enabled=False)))
            self.assertEqual(self.ok(self.client.get('/api/admin/regions'))['items'][0]['name'],'巴西订阅')
            self.ok(self.client.delete('/api/admin/regions/BR'))
            self.ok(self.client.delete('/api/admin/regions/PH'))
            seed()
            codes={r['code'] for r in self.ok(self.client.get('/api/admin/regions'))['items']}
            self.assertNotIn('PH',codes);self.assertNotIn('BR',codes)
            self.assertEqual(regions.default_country(),'US')
            self.assertEqual(self.client.delete('/api/admin/regions/BR').status_code,404)

    def test_region_admission_snapshot_api_and_disable_delete(self):
        from backend import subscription_cslive_go as flow
        with self.region_fixture(),self.api_fixture() as (owner,client,_):
            cfg=db.scalar("SELECT value FROM settings WHERE key='payment'")
            db.execute("UPDATE settings SET value=$1 WHERE key='payment'",dict(cfg,subscription_flow=flow.FLOW))
            row={'code':'BR','name':'巴西','currency':'BRL','enabled':True,'sort':-5}
            self.ok(self.client.post('/api/admin/regions',json=row))
            for path in ('/api/public','/api/v1/catalog'):
                values=self.ok(client.get(path))['countries']
                self.assertIn({'code':'BR','name':'巴西','currency':'BRL'},values)
            proxy,values=self.proxy()
            self.ok(self.client.put('/api/admin/resources/'+proxy,json=dict(values,country='BR')))
            payload=dict(self.api_payload('subscription'),product_id='plus',country='BR');headers={'Idempotency-Key':uid()}
            oid=self.ok(client.post('/api/v1/orders',json=payload,headers=headers))['id']
            snapshot=db.scalar('SELECT region_snapshot FROM orders WHERE id=$1',oid)
            self.assertEqual(snapshot,{'code':'BR','name':'巴西','currency':'BRL'})
            self.ok(self.client.put('/api/admin/regions/BR',json=dict(row,currency='USD',enabled=False)))
            self.assertEqual(regions.order_region('BR',snapshot)['currency'],'BRL')
            self.assertTrue(self.ok(client.post('/api/v1/orders',json=payload,headers=headers))['replayed'])
            before=db.one('SELECT balance,frozen FROM users WHERE id=$1',owner)
            self.assertEqual(client.post('/api/v1/orders',json=payload,headers={'Idempotency-Key':uid()}).status_code,400)
            self.assertEqual(db.one('SELECT balance,frozen FROM users WHERE id=$1',owner),before)
            self.ok(self.client.delete('/api/admin/regions/BR'))
            self.assertNotIn('BR',{r['code'] for r in self.ok(client.get('/api/v1/catalog'))['countries']})
            self.assertEqual(db.scalar('SELECT region_snapshot FROM orders WHERE id=$1',oid),snapshot)
            self.assertEqual(db.scalar('SELECT country FROM resources WHERE id=$1',proxy),'BR')
            self.ok(self.client.put('/api/admin/resources/'+proxy,json=dict(values,country='BR',endpoint='')))
            # Claiming an existing order never reinterprets deleted region settings.
            db.execute("UPDATE orders SET created_at=now()-interval '30 days' WHERE id=$1",oid)
            job=flow.claim_subscription()
            self.assertEqual(job['id'],oid)
            self.assertEqual(regions.order_region(job['country'],job['region_snapshot'])['currency'],'BRL')

    def test_region_empty_catalog_disables_admission_and_legacy_flow_keeps_currency(self):
        with self.region_fixture(),self.api_fixture() as (_,client,_):
            rows=deepcopy(regions.items())
            rows[0]['currency']='USD'
            db.execute("UPDATE settings SET value=$1 WHERE key='regions'",{'items':rows})
            self.assertEqual(regions.selected('PH','gener_direct_pay')['currency'],'PHP')
            self.assertEqual(regions.selected('PH','cslive_go_direct_pay')['currency'],'USD')
            for row in rows:self.ok(self.client.put('/api/admin/regions/'+row['code'],json=dict(row,enabled=False)))
            self.assertEqual(self.ok(client.get('/api/v1/catalog'))['countries'],[])
            self.assertEqual(self.ok(client.get('/api/public'))['default_country'],'')
            self.assertEqual(client.post('/api/v1/orders',json=self.api_payload(),headers={'Idempotency-Key':uid()}).status_code,400)
            self.assertEqual(regions.order_region('PH',{})['currency'],'PHP')
