"""Editing resources preserves already acquired clients and order sessions."""
from backend import db,proxies
from backend.common import decrypt,uid


class ProxyEditingTests:
    def test_proxy_edit_with_unexpired_lease_keeps_old_and_new_order_snapshots(self):
        from backend import subscription_cslive_go as flow
        with self.api_fixture() as (owner,client,_):
            payment=db.scalar("SELECT value FROM settings WHERE key='payment'")
            db.execute("UPDATE settings SET value=$1 WHERE key='payment'",dict(payment,subscription_flow=flow.FLOW))
            proxy_id,values=self.proxy();values['capacity']=10
            self.ok(self.client.put('/api/admin/resources/'+proxy_id,json=values))
            original_cfg=db.scalar("SELECT value FROM settings WHERE key='proxy'")
            db.execute("UPDATE settings SET value=$1 WHERE key='proxy'",dict(original_cfg,default_id=proxy_id))
            try:
                ids=[]
                for _ in range(2):
                    ids.append(self.ok(client.post('/api/v1/orders',json=dict(self.api_payload(),country='US'),headers={'Idempotency-Key':uid()}))['id'])
                old_endpoint=flow._acquire_order_proxy(ids[0],'US',600)
                snapshot=db.one('SELECT * FROM order_proxy_sessions WHERE order_id=$1',ids[0])
                leases=db.all('SELECT * FROM proxy_leases WHERE resource_id=$1 ORDER BY id',proxy_id)
                self.assertTrue(leases)
                # A finished order can leave an unexpired capacity lease.
                db.execute("UPDATE orders SET status='failed' WHERE id=$1",ids[0])
                new_endpoint='8.8.4.4:8080:new-user:new-password'
                self.ok(self.client.put('/api/admin/resources/'+proxy_id,json=dict(values,endpoint=new_endpoint)))
                self.assertEqual(db.one('SELECT * FROM order_proxy_sessions WHERE order_id=$1',ids[0]),snapshot)
                self.assertEqual(db.all('SELECT * FROM proxy_leases WHERE resource_id=$1 ORDER BY id',proxy_id),leases)
                self.assertEqual(flow._acquire_order_proxy(ids[0],'US',600),old_endpoint)
                self.assertEqual(flow._acquire_order_proxy(ids[1],'US',600),proxies.normalize_proxy(new_endpoint))
                # A running client also owns its previously acquired endpoint.
                with db.transaction() as q:captured,lease=proxies.acquire(q,proxy_id,110)
                try:
                    newer='1.1.1.1:8080:newer-user:newer-password'
                    self.ok(self.client.put('/api/admin/resources/'+proxy_id,json=dict(values,endpoint=newer)))
                    self.assertEqual(decrypt(captured['endpoint_enc']),proxies.normalize_proxy(new_endpoint))
                    self.assertEqual(decrypt(db.scalar('SELECT endpoint_enc FROM resources WHERE id=$1',proxy_id)),proxies.normalize_proxy(newer))
                    self.assertEqual(flow._acquire_order_proxy(ids[1],'US',600),proxies.normalize_proxy(new_endpoint))
                    changed=db.execute("UPDATE resources SET last_status='ok' WHERE id=$1 AND endpoint_enc=$2",proxy_id,captured['endpoint_enc'])
                    self.assertEqual(changed,0)
                    self.assertEqual(self.client.delete('/api/admin/resources/'+proxy_id).status_code,409)
                finally:db.execute('DELETE FROM proxy_leases WHERE id=$1',lease)
            finally:
                db.execute("UPDATE settings SET value=$1 WHERE key='proxy'",original_cfg)
                db.execute('DELETE FROM proxy_leases WHERE resource_id=$1',proxy_id)
