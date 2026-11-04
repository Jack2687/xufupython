"""Integration mixin for the disposable-database backend suite."""
from datetime import timedelta
from unittest.mock import patch
from backend import db
from backend.common import now


class PaymentDelaySettingsTests:
    def test_post_payment_delay_settings_defaults_validation_and_older_clients(self):
        original = db.scalar("SELECT value FROM settings WHERE key='payment'")
        legacy = {k:v for k,v in original.items() if k != 'post_payment_delay_seconds'}
        try:
            db.execute("UPDATE settings SET value=$1 WHERE key='payment'",legacy)
            self.assertEqual(self.ok(self.client.get('/api/admin/settings'))['payment']['post_payment_delay_seconds'],5)
            self.assertEqual(db.scalar("SELECT value FROM settings WHERE key='payment'"),legacy)
            for seconds in (0,5,17,300):
                self.ok(self.client.put('/api/admin/settings/payment',json=dict(legacy,post_payment_delay_seconds=seconds)))
                self.assertEqual(self.ok(self.client.get('/api/admin/settings'))['payment']['post_payment_delay_seconds'],seconds)
            self.ok(self.client.put('/api/admin/settings/payment',json=legacy))
            self.assertEqual(self.ok(self.client.get('/api/admin/settings'))['payment']['post_payment_delay_seconds'],300)
            for invalid in (-1,301,True,1.5,'5',None):
                self.assertEqual(self.client.put('/api/admin/settings/payment',json=dict(legacy,post_payment_delay_seconds=invalid)).status_code,400)
        finally:
            db.execute("UPDATE settings SET value=$1 WHERE key='payment'",original)

    def test_both_go_flows_wait_before_worker_without_changing_payment(self):
        from backend import post_payment as gener, cslive_go_post_payment as cslive, post_payment_delay as delay
        original = db.scalar("SELECT value FROM settings WHERE key='payment'")
        try:
            for flow, module, worker in [('gener_direct_pay',gener,'backend.go_post_payment.execute'),
                                         ('cslive_go_direct_pay',cslive,'backend.cslive_go_post_payment.execute')]:
                for seconds in (None,0,12):
                    with self.subTest(flow=flow,seconds=seconds):
                        cfg=dict(original,post_refresh_retries=4,post_refresh_interval_seconds=5)
                        cfg.pop('post_payment_delay_seconds',None)
                        if seconds is not None:cfg['post_payment_delay_seconds']=seconds
                        db.execute("UPDATE settings SET value=$1 WHERE key='payment'",cfg)
                        oid,owner=self.paid_followup_order()
                        paid=now()
                        db.execute('UPDATE subscription_runs SET subscription_flow=$2,paid_at=$3 WHERE order_id=$1',oid,flow,paid)
                        before=self.paid_snapshot(oid,owner)
                        sequence=[]
                        def slept(value):sequence.append(('wait',value))
                        def complete(cp,save):
                            sequence.append(('worker',None))
                            if flow=='cslive_go_direct_pay':
                                self.assertEqual(cp['refresh_retries'],4)
                                self.assertEqual(cp['refresh_interval_seconds'],5)
                            cp.update(activation='active',renewal='off');save('completed')
                        with patch.object(delay,'now',return_value=paid), \
                             patch.object(delay.time,'sleep',side_effect=slept),patch(worker,side_effect=complete):
                            self.assertEqual(module._run(oid,plan='pro5x')['state'],'completed')
                            expected=5 if seconds is None else seconds
                            self.assertEqual(sequence,([('wait',expected)] if expected else [])+[('worker',None)])
                            module.resume(oid)
                            self.assertEqual(sequence.count(('worker',None)),1)
                        self.assertEqual(self.paid_snapshot(oid,owner),before)
        finally:
            db.execute("UPDATE settings SET value=$1 WHERE key='payment'",original)

    def test_waiting_followup_keeps_order_lock_and_expired_proxy_stops_worker(self):
        from backend import post_payment as gener, cslive_go_post_payment as cslive, post_payment_delay as delay
        for flow,module,worker in [('gener_direct_pay',gener,'backend.go_post_payment.execute'),
                                   ('cslive_go_direct_pay',cslive,'backend.cslive_go_post_payment.execute')]:
            oid,owner=self.paid_followup_order()
            paid=now()
            db.execute('UPDATE subscription_runs SET subscription_flow=$2,paid_at=$3 WHERE order_id=$1',oid,flow,paid)
            def slept(_):
                result=self.ok(self.client.post('/api/admin/orders/batch-delete',json={'ids':[oid]}))
                self.assertEqual(result['deleted'],0)
                self.assertEqual(result['skipped'],1)
            def complete(cp,save):cp.update(activation='active',renewal='off');save('completed')
            with patch.object(delay,'now',return_value=paid),patch.object(delay.time,'sleep',side_effect=slept),patch(worker,side_effect=complete):
                self.assertEqual(module._run(oid,plan='pro5x')['state'],'completed')
            self.assertIsNone(db.scalar('SELECT deleted_at FROM orders WHERE id=$1',oid))
            expired,_=self.paid_followup_order()
            db.execute('UPDATE subscription_runs SET subscription_flow=$2,paid_at=$3 WHERE order_id=$1',expired,flow,paid)
            db.execute('UPDATE order_proxy_sessions SET expires_at=$2 WHERE order_id=$1',expired,paid+timedelta(seconds=2))
            with patch.object(delay,'now',return_value=paid),patch(worker) as execute:
                self.assertEqual(module._run(expired,plan='pro5x')['warning_code'],'proxy_session_expired')
                execute.assert_not_called()
