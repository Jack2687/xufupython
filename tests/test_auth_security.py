import asyncio
import os
import threading
from unittest.mock import patch
import httpx
from fastapi.testclient import TestClient
from backend import auth,db
from backend.common import password_hash,uid,BusinessError


class AuthSecurityTests:
    def test_auth_login_rechecks_password_before_session(self):
        user_id,email,_=self.new_user()
        create=auth.create_session
        def interleave(request,*args):
            with db.transaction() as q:
                q.execute('UPDATE users SET password_hash=$2 WHERE id=$1',user_id,password_hash('Changed-password-123'))
                q.execute('DELETE FROM sessions WHERE user_id=$1',user_id)
            return create(request,*args)
        client=TestClient(self.client.app)
        with patch.object(auth,'create_session',side_effect=interleave):
            self.assertEqual(client.post('/api/auth/login',json={'email':email,'password':'Test-password-123'}).status_code,401)
        self.assertEqual(db.scalar('SELECT count(*) FROM sessions WHERE user_id=$1',user_id),0)
        self.ok(client.post('/api/auth/login',json={'email':email,'password':'Changed-password-123'}))

    def test_auth_registration_session_is_atomic(self):
        with self.site_config(False):
            email=uid()+'@example.test';v={'email':email,'name':'atomic','password':'Register-password-123'}
            with patch.object(auth,'insert_session',side_effect=BusinessError('fixture',503)):
                self.assertEqual(TestClient(self.client.app).post('/api/auth/register',json=v).status_code,503)
            self.assertFalse(db.scalar('SELECT count(*) FROM users WHERE email=$1',email))
            self.ok(TestClient(self.client.app).post('/api/auth/register',json=v))

    def test_auth_password_work_keeps_event_loop_available(self):
        _,email,_=self.new_user();original=auth.password_matches
        entered=threading.Event();release=threading.Event()
        def slow(*args):
            entered.set();release.wait(3);return original(*args)
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.client.app),base_url='http://testserver') as client:
                with patch.object(auth,'password_matches',side_effect=slow):
                    pending=asyncio.create_task(client.post('/api/auth/login',json={'email':email,'password':'Test-password-123'}))
                    try:
                        self.assertTrue(await asyncio.to_thread(entered.wait,2))
                        self.assertFalse(pending.done(),'password computation blocked the event loop')
                        r=await asyncio.wait_for(client.get('/health/live'),1)
                        self.assertEqual(r.status_code,200)
                        self.assertFalse(release.is_set())
                    finally:release.set()
                    self.assertEqual((await pending).status_code,200)
        asyncio.run(run())

    def test_auth_proxy_boundaries_and_cookie(self):
        from backend.request_source import client_ip,cookie_secure
        from starlette.requests import Request
        def request(peer,forwarded,proto='http'):
            return Request({'type':'http','method':'GET','scheme':'http','server':('site.test',80),'path':'/','query_string':b'','client':(peer,1),'headers':[(b'host',b'site.test'),(b'x-forwarded-for',forwarded.encode()),(b'x-forwarded-proto',proto.encode())]})
        with patch.dict(os.environ,{'TRUSTED_PROXIES':'172.30.0.1,127.0.0.1,::1','PUBLIC_ORIGIN':'','COOKIE_SECURE':'false'}):
            self.assertEqual(client_ip(request('172.30.0.1','198.51.100.1')), '198.51.100.1')
            self.assertEqual(client_ip(request('198.51.100.2','198.51.100.1')), '198.51.100.2')
            self.assertEqual(client_ip(request('172.30.0.1','198.51.100.1, 198.51.100.2')), '198.51.100.2')
            self.assertEqual(client_ip(request('172.30.0.1','invalid')), '172.30.0.1')
            self.assertTrue(cookie_secure(request('172.30.0.1','198.51.100.1','https')))
            self.assertFalse(cookie_secure(request('198.51.100.2','198.51.100.1','https')))
            _,email,_=self.new_user()
            async def run():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.client.app,client=('172.30.0.1',1)),base_url='http://testserver') as client:
                    for i in range(11):
                        result=await client.post('/api/auth/login',json={'email':email,'password':'Test-password-123'},headers={'x-forwarded-for':f'198.51.100.{i+1}'})
                        self.assertEqual(result.status_code,200,result.text)
            asyncio.run(run())

    def test_auth_old_reset_code_rejected_after_profile_change(self):
        with self.site_config(False):
            _,email,user=self.new_user();code=self.email_code(TestClient(self.client.app),email,'reset')
            self.ok(user.patch('/api/me',json={'name':'changed','old_password':'Test-password-123','password':'Changed-password-123'}))
            self.assertEqual(TestClient(self.client.app).post('/api/auth/reset-password',json={'email':email,'code':code,'password':'Old-code-password-123'}).status_code,400)
