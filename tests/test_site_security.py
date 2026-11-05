import copy
import json
import re
import smtplib
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from backend import db, email_auth
from backend.common import config, decrypt, uid, digest


class SiteSecurityTests:
    @contextmanager
    def site_config(self, verify=False):
        saved={key:config(key) for key in ('features','security','mail','site')}
        try:
            mail={'enabled':True,'host':'smtp.example.test','port':465,'username':'sender@example.test',
                  'password':'smtp-secret-fixture','from_email':'sender@example.test','from_name':'测试站点','encryption':'ssl'}
            self.ok(self.client.put('/api/admin/settings/mail',json=mail))
            self.ok(self.client.put('/api/admin/settings/security',json={'registration_open':True,'email_verify_enabled':verify,'email_domain_whitelist':[]}))
            yield
        finally:
            for key,value in saved.items():db.execute('UPDATE settings SET value=$2 WHERE key=$1',key,value)

    def email_code(self, client, email, purpose='register'):
        with patch('backend.email_auth.smtp_send') as send:
            self.ok(client.post('/api/auth/email-code',json={'email':email,'purpose':purpose}))
            self.assertEqual(send.call_count,1)
            return re.search(r'\b(\d{6})\b',send.call_args.args[2])[1]

    def test_site_features_enforce_direct_web_api_and_alternate_flows(self):
        with self.site_config():
            _,_,user=self.new_user();key=self.ok(user.post('/api/keys',json={'name':'feature-test'}))['key']
            token=TestClient(self.client.app,headers={'Authorization':'Bearer '+key})
            flags={k:False for k in config('features')}
            self.ok(self.client.put('/api/admin/settings/features',json=flags))
            for path in ('overview','orders','orders/fake','wallet','v1/topups','keys','invites','membership','renewals','renewals/fake','quote','v1/catalog','v1/me','subscriptions/fake','cslive-go/subscriptions/fake','cslive-full/subscriptions/fake'):
                self.assertEqual(user.get('/api/'+path).status_code,403,path)
            for prefix in ('','/v1','/cslive-go','/cslive-full'):
                self.assertEqual(user.post('/api'+prefix+'/orders',json={}).status_code,403)
            self.assertEqual(user.post('/api/session/inspect',json={}).status_code,403)
            self.assertEqual(token.get('/api/v1/me').status_code,403)
            self.assertEqual(user.get('/docs').status_code,403)
            self.assertEqual(user.get('/api/openapi.json').status_code,403)
            me=self.ok(user.get('/api/me'))
            self.assertEqual([n['id'] for n in me['navigation']],['profile'])
            for field in ('balance','frozen','invite_code','fee_override'):self.assertNotIn(field,me)
            self.assertIsNone(me['membership'])
            pub=self.ok(user.get('/api/public'))
            self.assertEqual(pub['products'],[]);self.assertFalse(pub['payment_connected'])
            self.ok(self.client.get('/api/admin/settings'));self.ok(self.client.get('/api/orders'));self.ok(self.client.get('/api/openapi.json'))
            flags['links']=True;flags['keys']=True;flags['home']=True
            self.ok(self.client.put('/api/admin/settings/features',json=flags))
            for prefix in ('','/v1','/cslive-go','/cslive-full'):
                self.assertEqual(user.post('/api'+prefix+'/orders',json={'workflow':'subscription'}).status_code,403)
                self.assertNotEqual(user.post('/api'+prefix+'/orders',json={'workflow':'link'}).json().get('error'),'此功能暂未开放')
            overview=self.ok(user.get('/api/overview'))
            self.assertEqual(overview['stats'],{});self.assertEqual(overview['recent_orders'],[])
            self.assertNotIn('balance',overview['user'])
            self.assertIsNone(self.ok(user.get('/api/v1/catalog'))['membership'])
            flags['wallet']=True
            self.ok(self.client.put('/api/admin/settings/features',json=flags))
            self.assertIsNone(self.ok(user.get('/api/v1/wallet'))['membership'])
            self.assertIsNone(self.ok(user.get('/api/quote?product_id=pro'))['membership'])

    def test_site_admin_assets_and_menu_never_reach_customer(self):
        from backend.db import ROOT
        _,_,user=self.new_user();anonymous=TestClient(self.client.app)
        assets=list((ROOT/'webui/assets').glob('Admin*.js'))
        self.assertGreaterEqual(len(assets),2)
        for asset in assets:
            self.assertEqual(user.get('/assets/'+asset.name).status_code,403)
            self.assertEqual(anonymous.get('/assets/'+asset.name).status_code,401)
            self.assertEqual(self.client.get('/assets/'+asset.name).status_code,200)
        public_js=''.join(p.read_text(encoding='utf-8') for p in (ROOT/'webui/assets').glob('*.js') if not p.name.startswith('Admin'))
        for label in ('运营控制台','代理管理','安全与认证','邮件设置'):
            self.assertNotIn(label,public_js)
        for path in ('/api/admin/settings','/api/admin/mail/logs','/admin'):
            self.assertEqual(user.get(path).status_code,403)
        self.assertNotIn('admin',json.dumps(self.ok(user.get('/api/me'))['navigation']))

    def test_site_mail_settings_encrypted_roundtrip_and_validation(self):
        with self.site_config():
            raw=config('mail');self.assertEqual(decrypt(raw['password_enc']),'smtp-secret-fixture')
            settings=self.ok(self.client.get('/api/admin/settings'))
            self.assertNotIn('password_enc',settings['mail']);self.assertTrue(settings['mail']['password_configured'])
            self.assertNotIn('smtp-secret-fixture',json.dumps(settings))
            self.ok(self.client.put('/api/admin/settings/mail',json=settings['mail']))
            self.assertEqual(config('mail')['password_enc'],raw['password_enc'])
            for invalid in ({'password_enc':'injection'},{'host':'smtp.test\r\nBcc:x@y.test'},{'encryption':'none'},{'port':True},{'from_name':'\nspoof'}):
                payload=dict(settings['mail'],**invalid)
                self.assertEqual(self.client.put('/api/admin/settings/mail',json=payload).status_code,400)
            public=self.ok(TestClient(self.client.app).get('/api/public'))
            self.assertNotIn('smtp.example.test',json.dumps(public));self.assertNotIn('mail',public)

    def test_site_registration_domains_match_reference(self):
        rules=email_auth.normalize_domains('@EXAMPLE.com， *.school.test\nexample.com')
        self.assertEqual(rules,['example.com','*.school.test'])
        for email,valid in [('a@example.com',True),('a@sub.example.com',False),('a@school.test',True),('a@sub.school.test',True),('a@evil-school.test',False),('a@school.test.evil',False)]:
            self.assertEqual(email_auth.email_allowed(email,{'email_domain_whitelist':rules}),valid)
        for value in ('*','*.','test','a..test','a.test/path','a*.test','-a.test'):
            with self.assertRaises(Exception):email_auth.normalize_domains(value)
        with self.site_config():
            cfg=dict(config('security'),email_domain_whitelist=['example.com'])
            self.ok(self.client.put('/api/admin/settings/security',json=cfg))
            self.assertEqual(TestClient(self.client.app).post('/api/auth/register',json={'email':'a@sub.example.com','name':'test','password':'Password-12345'}).status_code,400)

    def test_site_registration_verification_required_consumed_once(self):
        with self.site_config(True):
            user=TestClient(self.client.app);email=uid()+'@example.test'
            payload={'email':email,'name':'验证码注册','password':'Password-12345'}
            self.assertEqual(user.post('/api/auth/register',json=payload).status_code,400)
            code=self.email_code(user,email)
            stored=db.one("SELECT * FROM email_challenges WHERE email=$1 AND purpose='register'",email)
            self.assertNotEqual(stored['secret_hash'],code)
            self.assertNotIn(code,json.dumps(self.ok(self.client.get('/api/admin/mail/logs'))))
            self.assertEqual(user.post('/api/auth/register',json=dict(payload,code='999999' if code!='999999' else '111111')).status_code,400)
            self.assertEqual(db.scalar("SELECT attempts FROM email_challenges WHERE email=$1 AND purpose='register'",email),1)
            self.ok(user.post('/api/auth/register',json=dict(payload,code=code)))
            self.assertEqual(self.ok(user.get('/api/me'))['email'],email)
            db.execute('DELETE FROM rate_buckets')
            self.assertEqual(user.post('/api/auth/register',json=dict(payload,code=code)).status_code,400)
            self.assertFalse(db.scalar('SELECT count(*) FROM email_challenges WHERE email=$1',email))

    def test_site_email_cooldown_expiry_attempt_limit_and_purpose(self):
        with self.site_config(True):
            user=TestClient(self.client.app);email=uid()+'@example.test';code=self.email_code(user,email)
            self.assertEqual(user.post('/api/auth/email-code',json={'email':email,'purpose':'register'}).status_code,429)
            payload={'email':email,'name':'test','password':'Password-12345'}
            self.assertEqual(user.post('/api/auth/reset-password',json=dict(payload,code=code)).status_code,400)
            for _ in range(5):
                db.execute('DELETE FROM rate_buckets')
                self.assertEqual(user.post('/api/auth/register',json=dict(payload,code='bad')).status_code,400)
            db.execute('DELETE FROM rate_buckets')
            self.assertEqual(user.post('/api/auth/register',json=dict(payload,code=code)).status_code,400)
            db.execute("UPDATE email_challenges SET created_at=now()-interval '61 seconds' WHERE email=$1",email)
            fresh=self.email_code(user,email)
            db.execute("UPDATE email_challenges SET expires_at=now()-interval '1 second' WHERE email=$1",email)
            self.assertEqual(user.post('/api/auth/register',json=dict(payload,code=fresh)).status_code,400)

    def test_site_email_failed_send_cannot_be_consumed(self):
        with self.site_config(True):
            user=TestClient(self.client.app);email=uid()+'@example.test'
            with patch('backend.email_auth.smtp_send',side_effect=smtplib.SMTPAuthenticationError(535,b'secret private upstream')):
                self.ok(user.post('/api/auth/email-code',json={'email':email,'purpose':'register'}))
            row=db.one('SELECT delivered FROM email_challenges WHERE email=$1',email);self.assertFalse(row['delivered'])
            record=db.one('SELECT status,detail FROM mail_deliveries WHERE recipient=$1',email)
            self.assertEqual(record['status'],'failed');self.assertNotIn('secret',record['detail'])

    def test_site_reset_private_response_revoke_sessions_keys_and_one_time(self):
        with self.site_config():
            id,email,user=self.new_user();another=TestClient(self.client.app)
            self.ok(another.post('/api/auth/login',json={'email':email,'password':'Test-password-123'}))
            key=self.ok(user.post('/api/keys',json={'name':'reset-test'}))['key']
            token=TestClient(self.client.app,headers={'Authorization':'Bearer '+key})
            anonymous=TestClient(self.client.app)
            with patch('backend.email_auth.smtp_send') as send:
                missing=self.ok(anonymous.post('/api/auth/email-code',json={'email':uid()+'@example.test','purpose':'reset'}))
                existing=self.ok(anonymous.post('/api/auth/email-code',json={'email':email,'purpose':'reset'}))
                self.assertEqual(existing,missing);self.assertEqual(send.call_count,1)
                code=re.search(r'\b(\d{6})\b',send.call_args.args[2])[1]
            payload={'email':email,'code':code,'password':'New-password-123'}
            self.ok(anonymous.post('/api/auth/reset-password',json=payload))
            for client,path in ((user,'/api/me'),(another,'/api/me'),(token,'/api/v1/me')):self.assertEqual(client.get(path).status_code,401)
            self.assertEqual(anonymous.post('/api/auth/reset-password',json=payload).status_code,400)
            self.assertEqual(anonymous.post('/api/auth/login',json={'email':email,'password':'Test-password-123'}).status_code,401)
            self.ok(anonymous.post('/api/auth/login',json={'email':email,'password':payload['password']}))

    def test_site_reset_concurrent_consumption_only_once(self):
        with self.site_config():
            _,email,_=self.new_user();client=TestClient(self.client.app);code=self.email_code(client,email,'reset')
            def reset(_):return TestClient(self.client.app).post('/api/auth/reset-password',json={'email':email,'code':code,'password':'New-password-123'}).status_code
            with ThreadPoolExecutor(max_workers=2) as pool:statuses=list(pool.map(reset,range(2)))
            self.assertEqual(sorted(statuses),[200,400])

    def test_site_registration_switch_preserved_and_reset_independent(self):
        with self.site_config(True):
            _,email,user=self.new_user()
            self.ok(self.client.put('/api/admin/settings/security',json=dict(config('security'),registration_open=False)))
            self.assertFalse(config('site')['registration_open'])
            self.assertFalse(self.ok(user.get('/api/public'))['security']['registration_open'])
            for path in ('/api/auth/register','/api/auth/email-code'):
                self.assertEqual(user.post(path,json={'email':uid()+'@example.test','purpose':'register'}).status_code,403)
            self.email_code(user,email,'reset')
            self.assertEqual(self.client.put('/api/admin/settings/mail',json=dict(self.ok(self.client.get('/api/admin/settings'))['mail'],enabled=False)).status_code,400)

    def test_site_smtp_tls_and_test_delivery_real_transport_contract(self):
        with self.site_config():
            smtp=MagicMock();smtp.__enter__.return_value=smtp;smtp.send_message.return_value={}
            with patch('backend.email_auth.smtplib.SMTP_SSL',return_value=smtp) as transport:
                self.ok(self.client.post('/api/admin/mail/test',json={'email':'receiver@example.test'}))
                self.assertEqual(transport.call_args.kwargs['context'].verify_mode,2)
                smtp.login.assert_called_once_with('sender@example.test','smtp-secret-fixture')
                message=smtp.send_message.call_args.args[0]
                self.assertEqual(message['To'],'receiver@example.test')
            cfg=self.ok(self.client.get('/api/admin/settings'))['mail'];cfg.update(encryption='starttls',port=587)
            self.ok(self.client.put('/api/admin/settings/mail',json=cfg))
            smtp.reset_mock()
            with patch('backend.email_auth.smtplib.SMTP',return_value=smtp):
                self.ok(self.client.post('/api/admin/mail/test',json={'email':'receiver@example.test'}))
                smtp.starttls.assert_called_once();self.assertEqual(smtp.starttls.call_args.kwargs['context'].verify_mode,2)
