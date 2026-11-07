"""Real checkout adapter and retry transitions with a mocked Go process only."""
from contextlib import contextmanager
import json
import subprocess
from unittest.mock import Mock, patch

from backend import db, payment_trace, subscription_cslive_go as flow, subscription_retry
from backend.checkout_errors import CheckoutError


class CheckoutDiagnosticTests:
    def test_checkout_device_retry_recovers_without_changing_proxy(self):
        with self.retry_fixture(flow,workflow='link') as job:
            oid=job['id'];binding=db.one('SELECT * FROM order_proxy_sessions WHERE order_id=$1',oid)
            with self.checkout_process({'error_code':'device_id_missing'}):flow.run_subscription(job)
            job=self.ready_retry(flow,oid)
            with self.checkout_process({'error_code':'','checkout_id':'cs_test_device','checkout_url':'https://pay.openai.com/c/pay/cs_test_device'},exit_code=0) as (process,payment):
                flow.run_subscription(job)
                payment.assert_not_called()
            self.assertEqual(db.scalar('SELECT status FROM orders WHERE id=$1',oid),'awaiting_payment')
            self.assertEqual(db.scalar('SELECT network_retry_count FROM subscription_runs WHERE order_id=$1',oid),1)
            self.assertEqual(db.one('SELECT * FROM order_proxy_sessions WHERE order_id=$1',oid),binding)
            self.assertEqual(db.scalar("SELECT count(*) FROM order_events WHERE order_id=$1 AND stage='failed'",oid),0)

    def test_checkout_missing_device_retries_and_releases_once(self):
        for limit in (0, 1, 2):
            with self.subTest(limit=limit),self.retry_fixture(flow,limit=limit) as job,self.checkout_process({'error_code':'device_id_missing','diagnostics':{'homepage_status':200,'homepage_device_cookie_received':False}}) as (process,payment):
                oid=job['id'];user_id=job['user_id']
                binding=db.one('SELECT * FROM order_proxy_sessions WHERE order_id=$1',oid)
                scheduler=db.scalar("SELECT value FROM settings WHERE key='scheduler'")
                db.execute("UPDATE settings SET value=$1 WHERE key='scheduler'",dict(scheduler,retry_delay_seconds=17))
                for attempt in range(limit+1):
                    flow.run_subscription(job)
                    self.assertEqual(db.one('SELECT * FROM order_proxy_sessions WHERE order_id=$1',oid),binding)
                    if attempt<limit:
                        self.assertEqual(db.scalar('SELECT status FROM orders WHERE id=$1',oid),'queued')
                        self.assertIsNone(flow.claim_subscription())
                        self.assertEqual(db.scalar('SELECT frozen FROM users WHERE id=$1',user_id),job['fee'])
                        event=payment_trace.details(oid)[-1]
                        self.assertEqual(event['code'],'checkout_retry_scheduled')
                        self.assertEqual(event['diagnostics']['retry_delay_seconds'],17)
                        self.assertNotIn('network_reason',event['diagnostics'])
                        self.assertGreater(db.scalar('SELECT extract(epoch FROM network_retry_at-now()) FROM subscription_runs WHERE order_id=$1',oid),10)
                        job=self.ready_retry(flow,oid)
                self.assertEqual(process.call_count,limit+1)
                self.assertEqual(db.scalar('SELECT network_retry_count FROM subscription_runs WHERE order_id=$1',oid),limit)
                self.assertEqual(db.scalar('SELECT status FROM orders WHERE id=$1',oid),'failed')
                self.assertEqual(db.scalar("SELECT message FROM order_events WHERE order_id=$1 AND stage='failed' ORDER BY id DESC LIMIT 1",oid),'初始化失败：未获取到设备标识')
                self.assertEqual(db.scalar("SELECT count(*) FROM ledger WHERE order_id=$1 AND kind='unfreeze'",oid),1)
                self.assertEqual(db.scalar('SELECT frozen FROM users WHERE id=$1',user_id),0)
                payment.assert_not_called()

    def test_checkout_homepage_diagnostics_are_typed_and_redacted(self):
        private='COOKIE-AND-TOKEN-MUST-NOT-LEAK'
        allowed={'homepage_status':200,'homepage_redirected':False,'homepage_device_cookie_before':False,
                 'homepage_device_cookie_received':False,'homepage_device_id_present':False,
                 'homepage_challenge_detected':True,'homepage_page_kind':'html','homepage_response_bytes':123}
        with self.retry_fixture(flow,limit=0) as job,self.checkout_process({'error_code':'device_id_missing','diagnostics':dict(allowed,cookies=private,location=private,body=private)}):
            with self.assertLogs('xufu.payment_trace',level='WARNING') as logs:flow.run_subscription(job)
            failure=self.checkout_failure(job['id'])
            self.assertEqual(failure['code'],'device_id_missing')
            for k,v in allowed.items():self.assertEqual(failure['diagnostics'][k],v)
            self.assertNotIn(private,json.dumps(payment_trace.details(job['id']),default=str)+' '.join(logs.output))
        from backend.checkout_errors import homepage_diagnostics
        self.assertEqual(homepage_diagnostics({'homepage_status':True,'homepage_redirected':private,'homepage_page_kind':[private],'homepage_response_bytes':-1}),{})

    def test_checkout_missing_device_cannot_replay_a_saved_or_submitted_payment(self):
        with self.retry_fixture(flow) as job:
            oid=job['id']
            db.execute("UPDATE subscription_runs SET checkout_checkpoint_enc='saved' WHERE order_id=$1",oid)
            self.assertFalse(subscription_retry.handle_failure(oid,CheckoutError('device_id_missing')))
            subscription_retry.mark_payment_submission(oid)
            self.assertTrue(subscription_retry.handle_failure(oid,CheckoutError('device_id_missing')))
            self.assertEqual(db.scalar('SELECT status FROM orders WHERE id=$1',oid),'review')
            self.assertEqual(db.scalar('SELECT network_retry_count FROM subscription_runs WHERE order_id=$1',oid),0)

    @contextmanager
    def checkout_process(self, payload=None, *, stdout=None, exit_code=1, error=None):
        result=subprocess.CompletedProcess([],exit_code,
            stdout if stdout is not None else json.dumps(payload).encode(),b'private stderr cookie=secret')
        with patch('backend.cslive_go_checkout.binary_path',return_value=Mock(is_file=lambda: True)), \
             patch('backend.cslive_go_checkout.subprocess.run',return_value=result,side_effect=error) as process, \
             patch.object(flow,'StripeHostedClient') as payment, \
             patch('requests.sessions.Session.request',side_effect=AssertionError('External network forbidden')):
            yield process,payment

    def checkout_failure(self, order_id):
        return next(e for e in payment_trace.details(order_id)
                    if e['phase']=='checkout' and e['state']=='failed')

    def test_checkout_codes_preserved_and_non_network_failures_not_retried(self):
        cases=[('http_401','invalid_session',401),('http_403','access_denied',403),
               ('http_429','upstream_busy',429),('http_503','upstream_busy',503),
               ('session_expired','invalid_session',0),('existing_subscription','checkout_existing_subscription',0),
               ('sentinel_turnstile_error','checkout_verification_failed',0),
               ('protocol_error','invalid_response',0)]
        for raw,category,status in cases:
            with self.subTest(raw=raw),self.retry_fixture(flow) as job,self.checkout_process({'error_code':raw}) as (process,payment):
                with self.assertLogs('xufu.payment_trace',level='WARNING') as logs:flow.run_subscription(job)
                failure=self.checkout_failure(job['id'])
                self.assertEqual((failure['code'],failure['http_status']),(category,status))
                self.assertEqual(failure['diagnostics']['response_error_code'],raw)
                self.assertEqual(failure['diagnostics']['response_error_type'],'go_checkout')
                self.assertEqual(failure['diagnostics']['checkout_exit_code'],1)
                self.assertIn('code='+raw,' '.join(logs.output))
                self.assertEqual(db.scalar('SELECT network_retry_count FROM subscription_runs WHERE order_id=$1',job['id']),0)
                self.assertEqual(db.scalar('SELECT status FROM orders WHERE id=$1',job['id']),'failed')
                self.assertEqual(db.scalar('SELECT frozen FROM users WHERE id=$1',job['user_id']),0)
                self.assertEqual(db.scalar("SELECT count(*) FROM ledger WHERE order_id=$1 AND kind='unfreeze'",job['id']),1)
                payment.assert_not_called();self.assertEqual(process.call_count,1)

    def test_checkout_network_retries_use_config_and_original_order(self):
        for raw,category,limit in [('network_error','connection',2),('network_timeout','timeout',0),('http_408','timeout',1)]:
            with self.subTest(raw=raw),self.retry_fixture(flow,limit=limit) as job,self.checkout_process({'error_code':raw}) as (process,payment):
                order_id=job['id'];proxy=db.scalar('SELECT endpoint_enc FROM order_proxy_sessions WHERE order_id=$1',order_id)
                scheduler=db.scalar("SELECT value FROM settings WHERE key='scheduler'")
                db.execute("UPDATE settings SET value=$1 WHERE key='scheduler'",dict(scheduler,retry_delay_seconds=17))
                for attempt in range(limit+1):
                    flow.run_subscription(job)
                    if attempt<limit:
                        self.assertEqual(db.scalar('SELECT status FROM orders WHERE id=$1',order_id),'queued')
                        self.assertIsNone(flow.claim_subscription())
                        last=payment_trace.details(order_id)[-1]
                        self.assertEqual(last['code'],'network_retry_scheduled')
                        self.assertEqual(last['diagnostics']['retry_delay_seconds'],17)
                        self.assertEqual(last['diagnostics']['retry_number'],attempt+1)
                        self.assertGreater(db.scalar('SELECT extract(epoch FROM network_retry_at-now()) FROM subscription_runs WHERE order_id=$1',order_id),10)
                        self.assertEqual(db.scalar('SELECT frozen FROM users WHERE id=$1',job['user_id']),job['fee'])
                        job=self.ready_retry(flow,order_id)
                self.assertEqual(self.checkout_failure(order_id)['code'],category)
                self.assertEqual(process.call_count,limit+1)
                self.assertEqual(db.scalar('SELECT network_retry_count FROM subscription_runs WHERE order_id=$1',order_id),limit)
                self.assertEqual(db.scalar('SELECT trace_round FROM subscription_runs WHERE order_id=$1',order_id),limit+1)
                self.assertEqual(db.scalar('SELECT endpoint_enc FROM order_proxy_sessions WHERE order_id=$1',order_id),proxy)
                self.assertEqual(db.scalar("SELECT count(*) FROM ledger WHERE order_id=$1 AND kind='freeze'",order_id),1)
                self.assertEqual(db.scalar("SELECT count(*) FROM ledger WHERE order_id=$1 AND kind='unfreeze'",order_id),1)
                self.assertIsNone(db.scalar('SELECT payment_submit_started_at FROM subscription_runs WHERE order_id=$1',order_id))
                payment.assert_not_called()

    def test_checkout_unknown_output_never_leaks_credentials_or_triggers_retry(self):
        private='token_private_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
        for raw in [private,'network_error cookie='+private,{'message':private},['http_408'],123]:
            with self.subTest(raw_type=type(raw).__name__),self.retry_fixture(flow) as job,self.checkout_process({'error_code':raw,'secret':private}) as (process,payment):
                with self.assertLogs('xufu.payment_trace',level='WARNING') as logs:flow.run_subscription(job)
                rows=payment_trace.details(job['id']);failure=self.checkout_failure(job['id'])
                self.assertEqual(failure['code'],'checkout_failed')
                self.assertEqual(failure['diagnostics']['response_error_code'],'redacted_error')
                evidence=json.dumps(rows,default=str)+' '.join(logs.output)
                for secret in (private,'private stderr','cookie=secret'):self.assertNotIn(secret,evidence)
                self.assertEqual(db.scalar('SELECT network_retry_count FROM subscription_runs WHERE order_id=$1',job['id']),0)
                payment.assert_not_called()

    def test_checkout_process_failures_have_safe_diagnostics(self):
        cases=[(b'',None,'invalid_response',2),(b'<html>cookie=private</html>',None,'invalid_response',2),
               (b'[]',None,'invalid_response',2),(None,subprocess.TimeoutExpired(['secret-argument'],180),'timeout',None),
               (None,OSError('private process path'),'checkout_program_unavailable',None)]
        for stdout,error,raw,exit_code in cases:
            with self.subTest(raw=raw),self.retry_fixture(flow,limit=0) as job,self.checkout_process(stdout=stdout,exit_code=2,error=error) as (process,payment):
                flow.run_subscription(job)
                failure=self.checkout_failure(job['id'])
                self.assertEqual(failure['diagnostics']['response_error_code'],raw)
                self.assertEqual(failure['diagnostics'].get('checkout_exit_code'),exit_code)
                self.assertNotIn('private',json.dumps(failure,default=str))
                payment.assert_not_called()

    def test_checkout_classification_cannot_replay_submitted_payment(self):
        with self.retry_fixture(flow) as job,self.checkout_process({'error_code':'network_error'}) as (process,payment):
            subscription_retry.mark_payment_submission(job['id'])
            self.assertTrue(subscription_retry.handle_failure(job['id'],CheckoutError('network_error')))
            self.assertEqual(db.scalar('SELECT status FROM orders WHERE id=$1',job['id']),'review')
            self.assertEqual(db.scalar('SELECT network_retry_count FROM subscription_runs WHERE order_id=$1',job['id']),0)
            self.assertEqual(db.scalar('SELECT frozen FROM users WHERE id=$1',job['user_id']),job['fee'])
            flow.run_subscription(job)
            process.assert_not_called();payment.assert_not_called()
