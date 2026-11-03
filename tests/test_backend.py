"""Behavioral integration tests use a disposable database on the NEW local cluster only."""
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
import unittest
from unittest.mock import patch, Mock
from urllib.parse import urlsplit,urlunsplit
import httpx
import psycopg2
from psycopg2 import sql
from dotenv import dotenv_values
from fastapi.testclient import TestClient

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
env=dotenv_values(ROOT/'.env')
url=urlsplit(env['DATABASE_URL'])
assert url.hostname=='127.0.0.1' and url.port==18972, 'Tests are restricted to the new local cluster'
TEST_DB='xufu_python_test_'+secrets.token_hex(6)
admin_conn=psycopg2.connect(host=url.hostname,port=url.port,user=url.username,password=url.password,dbname='postgres')
admin_conn.autocommit=True
with admin_conn.cursor() as cur:cur.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(TEST_DB)))
os.environ['DATABASE_URL']=urlunsplit((url.scheme,url.netloc,'/'+TEST_DB,'',''))
os.environ['MASTER_KEY']=base64.b64encode(secrets.token_bytes(32)).decode()
os.environ['APP_TEST_MODE']='1'
os.environ['LOCAL_EGRESS_PROXY']='';os.environ['LOCAL_EGRESS_SOCKS_PROXY']=''
os.environ['BOOTSTRAP_EMAIL']='admin';os.environ['BOOTSTRAP_PASSWORD']='admin'
from backend import db
from backend.main import app
from backend.common import uid,digest,encrypt,decrypt,now
from backend import renewals,proxies
from backend.seed import DEFAULTS


class BackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client=TestClient(app);cls.client.__enter__()
        result=cls.client.post('/api/auth/login',json={'email':'admin','password':'admin'})
        assert result.status_code==200,result.text

    @classmethod
    def tearDownClass(cls):cls.client.__exit__(None,None,None)

    def setUp(self):db.execute('DELETE FROM rate_buckets')

    def ok(self,response,status=200):
        self.assertEqual(response.status_code,status,response.text[:400]);return response.json()

    def new_user(self,name='测试用户'):
        email=uid()[:12]+'@example.test';pw='Test-password-123'
        id=self.ok(self.client.post('/api/admin/users',json={'email':email,'name':name,'password':pw,'role':'member'}))['id']
        client=TestClient(app);self.ok(client.post('/api/auth/login',json={'email':email,'password':pw}))
        return id,email,client

    def proxy(self):
        v={'name':'测试代理','country':'US','endpoint':'8.8.8.8:8080:fixture:secret','vendor':'other','ip_mode':'static','enabled':True,'weight':1,'capacity':2,'is_global':False}
        id=self.ok(self.client.post('/api/admin/resources',json=v))['id'];return id,v

    def fake_session(self,account=None,refresh=False):
        account=account or uid();email='fixture@example.test'
        claims={'exp':int((now()+timedelta(hours=2)).timestamp()),'https://api.openai.com/profile':{'email':email},'https://api.openai.com/auth':{'chatgpt_account_id':account}}
        part=base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
        s={'accessToken':'eyJhbGciOiJub25lIn0.'+part+'.fixture','user':{'email':email},'account':{'id':account,'planType':'plus'}}
        if refresh:s['sessionToken']='fixture-cookie'
        return s

    def task(self,client,session):
        return self.ok(client.post('/api/renewals',json={'session':session,'accepted':True},headers={'Idempotency-Key':uid()}))['id']

    def claim_specific(self,id):
        token=uid()
        db.execute("UPDATE renewal_tasks SET lease_token=$2,lease_until=now()+interval '110 seconds',checks=checks+1,status='processing' WHERE id=$1",id,token)
        return db.one('SELECT * FROM renewal_tasks WHERE id=$1',id)

    def test_01_auth_permissions_api_and_csrf(self):
        anonymous=TestClient(app)
        self.assertEqual(anonymous.get('/api/admin/resources').status_code,401)
        id,email,user=self.new_user()
        self.assertEqual(user.get('/api/admin/settings').status_code,403)
        self.assertEqual(user.post('/api/topups',json={'amount':100},headers={'Origin':'https://evil.example'}).status_code,403)
        self.assertEqual(user.post('/api/topups',json={'amount':100},headers={'Sec-Fetch-Site':'cross-site'}).status_code,403)
        key=self.ok(user.post('/api/keys',json={'name':'测试 API'}))
        token_client=TestClient(app,headers={'Authorization':'Bearer '+key['key']})
        self.ok(token_client.get('/api/v1/wallet'))
        self.assertEqual(token_client.get('/api/admin/users').status_code,403)
        self.assertEqual(token_client.get('/api/me').status_code,403)
        self.ok(user.delete('/api/keys/'+key['id']))
        self.assertEqual(token_client.get('/api/v1/wallet').status_code,401)
        admin_asset=next((ROOT/'webui/assets').glob('Admin-*.js'))
        self.assertEqual(anonymous.get('/assets/'+admin_asset.name).status_code,401)
        self.assertEqual(user.get('/assets/'+admin_asset.name).status_code,403)
        self.assertEqual(self.client.get('/assets/'+admin_asset.name).status_code,200)
        self.assertEqual(self.client.get('/').headers['x-frame-options'],'DENY')
        self.assertEqual(self.client.get('/api/not-present').status_code,404)

    def test_02_read_interfaces_and_settings(self):
        for path in ['/health/live','/health/ready','/api/public','/api/me','/api/overview','/api/wallet','/api/invites','/api/keys','/api/membership','/api/orders','/api/renewals','/api/v1/catalog','/api/v1/wallet','/api/v1/membership','/api/v1/orders','/api/openapi.json']+['/api/admin/'+p for p in ['users','overview','settings','membership','topups','codes','orders','resources','billing-profiles','products','announcements','risk','audit','finance']]:
            with self.subTest(path=path):self.ok(self.client.get(path))
        settings=self.ok(self.client.get('/api/admin/settings'))
        for key,value in settings.items():self.ok(self.client.put('/api/admin/settings/'+key,json=value))
        risk=dict(settings['risk'],session_hours=0)
        self.assertEqual(self.client.put('/api/admin/settings/risk',json=risk).status_code,400)
        self.assertEqual(self.client.put('/api/admin/settings/risk',json={'bad':'value'}).status_code,400)
        catalog=self.ok(self.client.get('/api/v1/catalog'))
        self.assertIn('items',catalog);self.assertEqual(catalog['countries'][0]['code'],'PH');self.assertFalse(catalog['payment_connected'])

    def test_03_topup_exact_once_and_wallet_delta(self):
        id,email,user=self.new_user();topup=self.ok(user.post('/api/topups',json={'amount':10000,'proof':'线下入账'}))['id']
        self.assertEqual(user.post('/api/admin/topups/'+topup+'/review',json={'action':'approve','note':'核实'}).status_code,403)
        for _ in range(2):self.ok(self.client.post('/api/admin/topups/'+topup+'/review',json={'action':'approve','note':'核实已到账'}))
        self.assertEqual(self.client.post('/api/admin/topups/'+topup+'/review',json={'action':'reject','note':'错误重试'}).status_code,409)
        wallet=self.ok(user.get('/api/wallet'));self.assertEqual(wallet['balance'],10000);self.assertEqual(len(wallet['ledger']),1)
        self.assertEqual(wallet['ledger'][0]['available_delta'],10000);self.assertEqual(wallet['ledger'][0]['frozen_delta'],0)
        self.assertEqual(self.ok(user.get('/api/membership'))['membership']['spent'],0)
        topup2=self.ok(user.post('/api/topups',json={'amount':100,'proof':''}))['id'];self.ok(user.post('/api/topups/'+topup2+'/cancel',json={}))
        self.assertEqual(user.post('/api/topups/'+topup2+'/cancel',json={}).status_code,409)
        for bad in [True,100.5,-100]:self.assertEqual(user.post('/api/topups',json={'amount':bad}).status_code,400)

    def test_04_concurrent_adjustment_and_replay_conflict(self):
        id,email,user=self.new_user();key=uid();path='/api/admin/users/'+id+'/balance'
        def adjust(_):return self.client.post(path,json={'amount':500,'reason':'并发验证'},headers={'Idempotency-Key':key})
        with ThreadPoolExecutor(max_workers=6) as pool:results=list(pool.map(adjust,range(6)))
        for r in results:self.ok(r)
        self.assertEqual(db.scalar('SELECT balance FROM users WHERE id=$1',id),500)
        self.assertEqual(db.scalar('SELECT count(*) FROM ledger WHERE user_id=$1',id),1)
        self.assertEqual(self.client.post(path,json={'amount':501,'reason':'并发验证'},headers={'Idempotency-Key':key}).status_code,409)
        self.assertEqual(self.client.post(path,json={'amount':-501,'reason':'余额不足'},headers={'Idempotency-Key':uid()}).status_code,400)
        self.assertEqual(db.scalar('SELECT balance FROM users WHERE id=$1',id),500)

    def test_05_concurrent_redemption_is_single_use(self):
        id1,_,u1=self.new_user();id2,_,u2=self.new_user()
        code=self.ok(self.client.post('/api/admin/codes',json={'count':1,'amount':1000,'days':1}))['codes'][0]
        def redeem(client):return client.post('/api/wallet/redeem',json={'code':code})
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(redeem,[u1,u2]))
        self.assertEqual(sorted(r.status_code for r in results),[200,400])
        self.assertEqual(db.scalar('SELECT sum(balance) FROM users WHERE id=ANY($1)',[id1,id2]),1000)
        self.assertEqual(db.scalar("SELECT count(*) FROM ledger WHERE kind='redeem' AND user_id=ANY($1)",[id1,id2]),1)
        used=db.scalar('SELECT id FROM redeem_codes WHERE code_hash=$1',digest(code))
        self.ok(self.client.post('/api/admin/codes/batch',json={'ids':[used],'action':'delete'}))
        self.assertEqual(db.scalar('SELECT status FROM redeem_codes WHERE id=$1',used),'used')
        created=self.ok(self.client.post('/api/admin/codes',json={'count':1,'amount':100,'days':1}))
        rid=db.scalar('SELECT id FROM redeem_codes WHERE batch=$1',created['batch'])
        self.assertEqual(self.ok(self.client.post('/api/admin/codes/batch',json={'ids':[rid],'action':'export'}))['codes'],created['codes'])
        self.ok(self.client.post('/api/admin/codes/batch',json={'ids':[rid],'action':'disable'}))
        self.ok(self.client.post('/api/admin/codes/batch',json={'ids':[rid],'action':'delete'}))
        self.assertFalse(db.scalar('SELECT id FROM redeem_codes WHERE id=$1',rid))

    def test_06_profile_account_and_session_revocation(self):
        id,email,user=self.new_user()
        self.ok(user.patch('/api/me',json={'name':'改名'}));self.assertEqual(self.ok(user.get('/api/me'))['name'],'改名')
        self.assertEqual(user.patch('/api/me',json={'name':'改名','old_password':'wrong','password':'Another-test-123'}).status_code,400)
        self.ok(user.patch('/api/me',json={'name':'改名','old_password':'Test-password-123','password':'Another-test-123'}))
        self.assertEqual(user.get('/api/me').status_code,401)
        self.ok(user.post('/api/auth/login',json={'email':email,'password':'Another-test-123'}))
        v={'email':email,'name':'改名','role':'member','status':'disabled','fee_override':None,'password':''}
        self.ok(self.client.patch('/api/admin/users/'+id,json=v));self.assertEqual(user.get('/api/me').status_code,401)
        admin=self.ok(self.client.get('/api/me'))
        v.update(email='admin',name='运营管理员',role='member',status='active')
        self.assertEqual(self.client.patch('/api/admin/users/'+admin['id'],json=v).status_code,400)

    def test_07_membership_growth_manual_and_refunds(self):
        id,_,user=self.new_user();order=uid()
        db.execute("INSERT INTO orders(id,user_id,product_id,product_name,email,status,fee,idempotency_key,request_hash) VALUES($1,$2,'pro','Pro','fixture@example.test','succeeded',300000,$1,'fixture')",order,id)
        for kind,amount,ref in [('charge',-300000,'fixture-charge'),('refund',50000,'fixture-refund')]:
            db.execute('INSERT INTO ledger(user_id,kind,amount,balance_after,frozen_after,reference,description,order_id) VALUES($1,$2,$3,0,0,$4,\'测试\',$5)',id,kind,amount,ref,order)
        m=self.ok(user.get('/api/membership'))['membership'];self.assertEqual(m['spent'],250000);self.assertEqual(m['current']['code'],'L2')
        self.ok(self.client.put('/api/admin/users/'+id+'/membership',json={'code':'L9','until':None,'reason':'合作测试'}))
        m=self.ok(user.get('/api/membership'))['membership'];self.assertEqual(m['current']['code'],'L9');self.assertEqual(m['automatic']['code'],'L2')
        bad={'items':DEFAULTS['tiers']['items'][:-1]}
        self.assertEqual(self.client.put('/api/admin/settings/tiers',json=bad).status_code,400)
        self.ok(self.client.put('/api/admin/users/'+id+'/membership',json={'code':'','until':None,'reason':'恢复自动'}))
        self.ok(self.client.post('/api/admin/users/pricing',json={'ids':[id],'fee_override':1234}))
        self.assertTrue(all(p['member_fee']==1234 for p in self.ok(user.get('/api/membership'))['products']))

    def test_08_billing_crud_sample_and_validation(self):
        p={'name':'测试地址','first_name':'Fixture','last_name':'Test','state':'CA','city':'Test City','line1':'123 Test Street','postal_code':'90001','enabled':True,'test_only':False}
        rows=self.ok(self.client.post('/api/admin/billing-profiles',json=p))['items'];id=rows[-1]['id']
        self.assertEqual(self.client.post('/api/admin/billing-profiles/sample',json={}).status_code,400)
        p['test_only']=True
        self.ok(self.client.put('/api/admin/billing-profiles/'+id,json=p))
        self.assertEqual(self.ok(self.client.post('/api/admin/billing-profiles/sample',json={}))['profile']['id'],id)
        p['state']='ZZ';self.assertEqual(self.client.put('/api/admin/billing-profiles/'+id,json=p).status_code,400)
        self.ok(self.client.delete('/api/admin/billing-profiles/'+id))
        self.assertEqual(self.ok(self.client.get('/api/admin/billing-profiles'))['items'],[])

    def test_09_proxy_secrets_leases_and_offline_checks(self):
        id,v=self.proxy();raw=db.scalar('SELECT endpoint_enc FROM resources WHERE id=$1',id)
        data=self.ok(self.client.get('/api/admin/resources'));self.assertNotIn('endpoint_enc',json.dumps(data));self.assertNotIn('fixture:secret',json.dumps(data))
        v['endpoint']='';v['name']='更新代理';self.ok(self.client.put('/api/admin/resources/'+id,json=v));self.assertEqual(raw,db.scalar('SELECT endpoint_enc FROM resources WHERE id=$1',id))
        lease=uid();db.execute("INSERT INTO proxy_leases(id,resource_id,expires_at) VALUES($1,$2,now()+interval '1 minute')",lease,id)
        v['country']='PH';self.assertEqual(self.client.put('/api/admin/resources/'+id,json=v).status_code,409)
        self.assertEqual(self.client.delete('/api/admin/resources/'+id).status_code,409)
        db.execute('DELETE FROM proxy_leases WHERE id=$1',lease)
        self.ok(self.client.post('/api/admin/resources/'+id+'/global',json={}))
        check=self.ok(self.client.post('/api/admin/resources/'+id+'/checks',json={'kind':'quality'}))['id']
        self.assertEqual(self.ok(self.client.post('/api/admin/resources/'+id+'/checks',json={'kind':'quality'}))['id'],check)
        self.assertEqual(self.client.post('/api/admin/resources/'+id+'/checks',json={'kind':'openai'}).status_code,409)
        def handle(req):
            if req.url.host=='ipinfo.io':return httpx.Response(200,json={'ip':'8.8.4.4','country':'US'})
            return httpx.Response(401,json={'error':'missing_key'})
        with patch('backend.proxies.proxy_client',side_effect=lambda *a:httpx.Client(transport=httpx.MockTransport(handle))):self.assertTrue(proxies.run_check())
        result=self.ok(self.client.get('/api/admin/proxy-checks/'+check));self.assertEqual(result['state'],'completed');self.assertEqual(result['result']['score'],100)
        self.ok(self.client.delete('/api/admin/resources/'+id));self.assertEqual(db.scalar("SELECT value->>'global_id' FROM settings WHERE key='proxy'"),'')
        for endpoint in ['127.0.0.1:8080','http://localhost:80','http://10.0.0.1:80','http://8.8.8.8:80/path']:
            with self.assertRaises(Exception):proxies.normalize_proxy(endpoint)
        with patch('backend.proxies.socket.getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',8080))]),patch('httpcore.SyncBackend.connect_tcp') as dial:
            with self.assertRaises(Exception):proxies.PublicNetworkBackend().connect_tcp('proxy.example.test',8080)
            dial.assert_not_called()
        with patch('backend.proxies.socket.getaddrinfo',return_value=[(2,1,6,'',('8.8.4.4',8080))]),patch('httpcore.SyncBackend.connect_tcp') as dial:
            proxies.PublicNetworkBackend().connect_tcp('proxy.example.test',8080)
            self.assertEqual(dial.call_args.args[0],'8.8.4.4')

    def test_10_renewal_cancellation_fence_and_resume(self):
        id,_,user=self.new_user();s=self.fake_session();self.ok(user.post('/api/session/inspect',json={'session':s}))
        task=self.task(user,s);resource,_=self.proxy();self.ok(self.client.post('/api/admin/resources/'+resource+'/global',json={}))
        writes=[]
        def upstream(req):
            if req.method=='POST':writes.append(req);return httpx.Response(502,json={'error':'ambiguous'})
            return httpx.Response(200,json={'is_active':True,'will_renew':True,'account_id':s['account']['id']})
        factory=lambda *a:httpx.Client(transport=httpx.MockTransport(upstream))
        for _ in range(2):renewals.run_task(self.claim_specific(task),factory)
        self.assertEqual(len(writes),1);self.assertTrue(db.scalar('SELECT cancel_started FROM renewal_tasks WHERE id=$1',task))
        db.execute("UPDATE renewal_tasks SET status='review',credentials_enc='',lease_token=NULL,lease_until=NULL WHERE id=$1",task)
        self.assertEqual(self.task(user,s),task)
        renewals.run_task(self.claim_specific(task),factory);self.assertEqual(len(writes),1)
        def done(req):
            self.assertEqual(req.method,'GET');return httpx.Response(200,json={'is_active':True,'will_renew':False,'account_id':s['account']['id']})
        renewals.run_task(self.claim_specific(task),lambda *a:httpx.Client(transport=httpx.MockTransport(done)))
        row=db.one('SELECT status,credentials_enc FROM renewal_tasks WHERE id=$1',task);self.assertEqual(row,{'status':'disabled','credentials_enc':''})
        detail=self.ok(user.get('/api/renewals/'+task));self.assertNotIn('credentials',json.dumps(detail));self.assertNotIn(s['accessToken'],json.dumps(detail))
        _,_,other=self.new_user();self.assertEqual(other.get('/api/renewals/'+task).status_code,404)
        self.ok(self.client.delete('/api/admin/resources/'+resource))

    def test_11_renewal_missing_proxy_and_refresh_mismatch(self):
        id,_,user=self.new_user();s=self.fake_session(refresh=True);task=self.task(user,s)
        with patch('backend.renewals.proxy_client',side_effect=AssertionError('Direct access forbidden')):
            renewals.run_task(self.claim_specific(task))
        self.assertEqual(db.scalar('SELECT status FROM renewal_tasks WHERE id=$1',task),'queued')
        resource,_=self.proxy();self.ok(self.client.post('/api/admin/resources/'+resource+'/global',json={}))
        calls=[]
        def upstream(req):calls.append(str(req.url));return httpx.Response(200,json=self.fake_session(account='different-account'))
        renewals.run_task(self.claim_specific(task),lambda *a:httpx.Client(transport=httpx.MockTransport(upstream)))
        self.assertEqual(len(calls),1);self.assertIn('/api/auth/session',calls[0]);self.assertEqual(db.scalar('SELECT status FROM renewal_tasks WHERE id=$1',task),'review')
        self.assertFalse(db.scalar('SELECT cancel_started FROM renewal_tasks WHERE id=$1',task))
        self.ok(self.client.delete('/api/admin/resources/'+resource))

    def test_12_content_finance_risk_and_validation(self):
        announcement=self.ok(self.client.post('/api/admin/announcements',json={'title':'公告测试','body':'测试内容','tone':'info','published':True,'login_visible':True,'sort':0,'starts_at':None,'ends_at':None}))['id']
        self.assertTrue(any(r['id']==announcement for r in self.ok(self.client.get('/api/public'))['announcements']))
        self.ok(self.client.delete('/api/admin/announcements/'+announcement))
        p=self.ok(self.client.get('/api/admin/products'))['items'][0];p['tag']='测试套餐';self.ok(self.client.put('/api/admin/products/'+p['id'],json=p))
        self.ok(self.client.post('/api/admin/blocks',json={'ip':'203.0.113.10','minutes':2,'reason':'测试限制'}));self.ok(self.client.delete('/api/admin/blocks/203.0.113.10'))
        self.assertEqual(self.client.post('/api/admin/blocks',json={'ip':'testclient','minutes':2,'reason':'测试'}).status_code,400)
        id,_,user=self.new_user('=FORMULA()')
        self.ok(self.client.post('/api/admin/users/'+id+'/balance',json={'amount':123,'reason':'财务测试'},headers={'Idempotency-Key':uid()}))
        for period in ('day','week','month','year'):self.ok(self.client.get('/api/admin/finance?period='+period))
        csv=self.client.get('/api/admin/finance/export');self.assertEqual(csv.status_code,200);self.assertTrue(csv.content.startswith(b'\xef\xbb\xbf'));self.assertIn("'=FORMULA()",csv.text)
        self.assertEqual(self.client.get('/api/admin/finance?start=invalid').status_code,400)
        self.assertEqual(self.client.post('/api/auth/register',json={'email':'bad','name':None,'password':{}}).status_code,400)

    def test_13_disabled_payment_and_frontend_authorized_changes(self):
        self.assertFalse(self.ok(self.client.get('/api/public'))['payment_connected'])
        excluded=[('POST','/api/v1/orders'),('POST','/api/orders/fake/cancel'),('POST','/api/admin/orders/fake/close'),('POST','/api/admin/orders/fake/payment/check'),('POST','/api/orders/fake/payment/confirm')]
        for method,path in excluded:self.assertEqual(self.client.request(method,path,json={}).status_code,404,path)
        tables={r['table_name'] for r in db.all("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")}
        self.assertFalse(tables & {'jobs','checkout_runs','payment_runs','subscription_protocol'})
        self.assertTrue({'subscription_runs','payment_trace_events'} <= tables)
        changes=json.loads((ROOT/'docs/frontend-authorized-changes.json').read_text(encoding='utf-8'))
        for relative,expected in json.loads((ROOT/'docs/frontend-manifest.json').read_text(encoding='utf-8-sig')).items():
            if relative in changes['source_files'] or relative.startswith(changes['generated_directory']+'/'):continue
            self.assertEqual(hashlib.sha256((ROOT/relative).read_bytes()).hexdigest(),expected,relative)


    def test_14_service_fee_quote(self):
        id,_,user=self.new_user()
        other_id,_,other=self.new_user()
        product=uid()
        db.execute('INSERT INTO products(id,name,description,fee,features,enabled) VALUES($1,\'报价测试\',\'测试\',101,\'[]\'::jsonb,true)',product)
        path='/api/quote?product_id='+product
        anonymous=TestClient(app)
        self.assertEqual(anonymous.get(path).status_code,401)
        before=db.one('SELECT balance,frozen FROM users WHERE id=$1',id)
        counts={t:db.scalar('SELECT count(*) FROM '+t) for t in ('orders','ledger','subscription_runs')}
        try:
            standard=self.ok(user.get(path))
            self.assertEqual((standard['fee'],standard['standard']),(101,101))
            self.assertEqual(standard['membership']['current']['code'],'L1')
            self.assertFalse(standard['payment_connected'])
            db.execute("UPDATE users SET membership_code='L3' WHERE id=$1",id)
            discounted=self.ok(user.get(path))
            self.assertEqual(discounted['fee'],91)
            self.assertEqual(discounted['membership']['current']['discount'],90)
            db.execute('UPDATE users SET fee_override=37 WHERE id=$1',id)
            self.assertEqual(self.ok(user.get(path))['fee'],37)
            self.assertEqual(self.ok(other.get(path))['fee'],101)
            db.execute('UPDATE users SET fee_override=0 WHERE id=$1',id)
            zero=self.ok(user.get(path))
            self.assertEqual(zero['fee'],0)
            self.assertEqual(zero['membership']['fee_override'],0)
            db.execute("UPDATE users SET fee_override=NULL,membership_until=now()-interval '1 day' WHERE id=$1",id)
            self.assertEqual(self.ok(user.get(path))['fee'],101)
            db.execute('UPDATE products SET fee=205 WHERE id=$1',product)
            self.assertEqual(self.ok(user.get(path))['fee'],205)
            key=self.ok(user.post('/api/keys',json={'name':'报价接口测试'}))
            api=TestClient(app,headers={'Authorization':'Bearer '+key['key']})
            self.assertEqual(self.ok(api.get('/api/v1/quote?product_id='+product))['fee'],205)
            self.assertEqual(api.get(path).status_code,403)
            self.ok(user.delete('/api/keys/'+key['id']))
            self.assertEqual(api.get('/api/v1/quote?product_id='+product).status_code,401)
            for query in ('','?product_id=missing','?product_id=%27%20OR%201%3D1--'):
                self.assertEqual(user.get('/api/quote'+query).status_code,404)
            db.execute('UPDATE products SET enabled=false WHERE id=$1',product)
            self.assertEqual(user.get(path).status_code,404)
            self.assertEqual(db.one('SELECT balance,frozen FROM users WHERE id=$1',id),before)
            self.assertEqual({t:db.scalar('SELECT count(*) FROM '+t) for t in counts},counts)
        finally:
            db.execute('DELETE FROM products WHERE id=$1',product)
            anonymous.close();user.close();other.close()


    def test_15_gener_config_requires_long_links(self):
        original=self.ok(self.client.get('/api/admin/settings'))['payment']
        short_links={plan:'oaics' for plan in original['checkout_types']}
        try:
            v=dict(original,subscription_flow='gener_direct_pay',checkout_type='oaics',checkout_types=short_links)
            self.ok(self.client.put('/api/admin/settings/payment',json=v))
            saved=self.ok(self.client.get('/api/admin/settings'))['payment']
            self.assertEqual(saved['subscription_flow'],'gener_direct_pay')
            self.assertEqual(saved['checkout_type'],'hosted')
            self.assertEqual(saved['checkout_types'],dict.fromkeys(short_links,'hosted'))
            v.pop('subscription_flow')
            self.ok(self.client.put('/api/admin/settings/payment',json=v))
            saved=self.ok(self.client.get('/api/admin/settings'))['payment']
            self.assertEqual(saved['subscription_flow'],'gener_direct_pay')
            self.assertEqual(saved['checkout_types'],dict.fromkeys(short_links,'hosted'))
            for flow in ('cslive','cslive_direct_pay'):
                v['subscription_flow']=flow
                self.ok(self.client.put('/api/admin/settings/payment',json=v))
                saved=self.ok(self.client.get('/api/admin/settings'))['payment']
                self.assertEqual(saved['checkout_types'],short_links)
            public=self.ok(self.client.get('/api/public'))
            self.assertFalse(public['payment_connected'])
            self.assertFalse(public['checkout_connected'])
        finally:
            self.ok(self.client.put('/api/admin/settings/payment',json=original))


    def test_16_queue_admission_billing_and_history(self):
        from backend import subscription
        payment=self.ok(self.client.get('/api/admin/settings'))['payment']
        billing=self.ok(self.client.get('/api/admin/settings'))['billing']
        user_id,_,user=self.new_user()
        _,_,other=self.new_user()
        profile={'id':'bp_regression','name':'测试资料','first_name':'Fixture','last_name':'User','email':'billing@example.test','state':'NH','city':'Concord','line1':'1 Example Street','postal_code':'03301','enabled':True,'test_only':False}
        try:
            db.execute("UPDATE settings SET value=$1 WHERE key='payment'",dict(payment,enabled=True,subscription_flow='gener_direct_pay'))
            db.execute("UPDATE settings SET value=$1 WHERE key='billing'",dict(billing,country='US',address_templates=[profile]))
            product=db.scalar('SELECT id FROM products WHERE enabled ORDER BY sort LIMIT 1')
            payload={'workflow':'link','product_id':product,'session':self.fake_session()}
            before=db.scalar('SELECT count(*) FROM orders')
            denied=user.post('/api/orders',json=payload,headers={'Idempotency-Key':uid()})
            self.assertEqual(denied.status_code,503)
            self.assertIn('后台任务未启动',denied.json()['error'])
            self.assertEqual(db.scalar('SELECT count(*) FROM orders'),before)
            with patch.dict(os.environ,{'APP_TEST_MODE':'0','APP_WORKER_MODE':'links'}):
                public=self.ok(user.get('/api/public'))
                self.assertTrue(public['checkout_connected']);self.assertFalse(public['payment_connected'])
                blocked=user.post('/api/orders',json=dict(payload,workflow='subscription'),headers={'Idempotency-Key':uid()})
                self.assertEqual(blocked.status_code,503)
                self.assertIn('自动付款尚未启用',blocked.json()['error'])
                first=self.ok(user.post('/api/orders',json=payload,headers={'Idempotency-Key':uid()}))['id']
                self.assertEqual(db.scalar("SELECT billing_snapshot->>'email' FROM subscription_runs WHERE order_id=$1",first),profile['email'])
                db.execute("UPDATE settings SET value=$1 WHERE key='billing'",dict(billing,country='US',address_templates=[dict(profile,email='  ')]))
                db.execute("UPDATE orders SET workflow='subscription' WHERE id=$1",first)
                second=self.ok(user.post('/api/orders',json=payload,headers={'Idempotency-Key':uid()}))['id']
                job=subscription.claim_subscription()
                self.assertEqual(job['id'],second)
                self.assertEqual(db.scalar('SELECT status FROM orders WHERE id=$1',first),'queued')
                self.assertEqual(job['billing_snapshot']['country'],'US')
                self.assertEqual(job['billing_snapshot']['state'],'NH')
                self.assertEqual(job['billing_snapshot']['profile_id'],profile['id'])
                self.assertEqual(job['billing_snapshot']['email'],payload['session']['user']['email'])
                self.assertEqual(job['billing_snapshot']['email_source'],'subscription_account')
                self.assertEqual(db.scalar("SELECT billing_snapshot->>'email' FROM subscription_runs WHERE order_id=$1",first),profile['email'])
                self.assertEqual(self.ok(self.client.get('/api/admin/orders/'+second))['billing']['region'],'NH')
                self.assertIn('自动付款尚未启用',self.ok(self.client.get('/api/admin/orders/'+first))['order']['queue_message'])
                db.execute("UPDATE settings SET value=jsonb_set(value,'{enabled}','false') WHERE key='payment'")
                self.assertIsNone(subscription.claim_subscription())
            subscription._add_event(second,'failed','fixture failure')
            detail=self.ok(self.client.get('/api/admin/orders/'+second))
            customer=self.ok(user.get('/api/orders/'+second))
            self.assertEqual(customer['trace'],[])
            business=[e for e in detail['trace'] if e['source']=='business']
            technical=[e for e in detail['trace'] if e['source']=='technical']
            self.assertEqual(len(business),3)
            self.assertEqual(business[-1]['state'],'failed')
            self.assertEqual([e['phase'] for e in technical],['accepted','billing','queue','queue'])
            self.assertEqual(technical[-1]['round'],1)
            self.assertTrue(all('http_status' not in e and 'elapsed_ms' not in e and 'round' not in e for e in business))
            self.assertEqual(user.get('/api/admin/orders/'+second).status_code,403)
            self.assertEqual(other.get('/api/orders/'+second).status_code,404)
            db.execute("UPDATE subscription_runs SET billing_snapshot=billing_snapshot-'country'-'profile_id' WHERE order_id=$1",second)
            legacy=self.ok(self.client.get('/api/admin/orders/'+second))['billing']
            self.assertEqual((legacy['country'],legacy['region'],legacy['state_code']),('US','NH','NH'))
            self.assertEqual(legacy['country_source'],'legacy_us_billing')
            self.assertIsNone(db.scalar("SELECT billing_snapshot->>'country' FROM subscription_runs WHERE order_id=$1",second))
        finally:
            db.execute("UPDATE orders SET status='failed' WHERE user_id=$1",user_id)
            db.execute("UPDATE settings SET value=$1 WHERE key='payment'",payment)
            db.execute("UPDATE settings SET value=$1 WHERE key='billing'",billing)
            user.close();other.close()

    def test_17_worker_links_mode(self):
        from backend import main, runtime
        with patch.dict(os.environ,{'APP_TEST_MODE':'0','APP_WORKER_MODE':'links'}):
            self.assertEqual(runtime.order_workflows(),('link',))
            stop=Mock();stop.wait.side_effect=[False,True]
            with patch.object(main,'ThreadPoolExecutor') as pool, patch.object(main,'maintenance'), patch.object(main.renewals,'claim') as renewal, patch.object(main.subscription,'claim_subscription',return_value={'workflow':'link'}) as claim:
                main.worker_loop(stop)
                renewal.assert_not_called();claim.assert_called_once()
                self.assertEqual(pool.return_value.submit.call_count,2)
                self.assertEqual(pool.return_value.submit.call_args_list[0].args[0],main.proxies.run_check)
                pool.return_value.shutdown.assert_called_once_with(wait=True,cancel_futures=True)
        with patch.dict(os.environ,{'APP_TEST_MODE':'0','APP_WORKER_MODE':'invalid'}):
            self.assertEqual(runtime.worker_mode(),'off')
        self.assertEqual(runtime.order_workflows(),())


    def test_18_local_proxy_chain_and_dns(self):
        from backend import egress
        response=httpx.Response(200,json={'Answer':[{'type':1,'data':'8.8.4.4'}]},request=httpx.Request('GET','https://dns.google/resolve'))
        headers=b'HTTP/1.1 200 Connection established\r\n\r\n'
        stream=Mock();stream.read.side_effect=[bytes([b]) for b in headers]
        with patch.dict(os.environ,{'LOCAL_EGRESS_PROXY':'http://127.0.0.1:7897'}), patch('backend.egress.httpx.Client') as client, patch('httpcore.SyncBackend.connect_tcp',return_value=stream) as dial:
            client.return_value.__enter__.return_value.get.return_value=response
            self.assertIs(proxies.PublicNetworkBackend().connect_tcp('proxy.example.test',8080,10),stream)
            self.assertEqual(dial.call_args.args[:2],('127.0.0.1',7897))
            self.assertIn(b'CONNECT 8.8.4.4:8080 HTTP/1.1',stream.write.call_args.args[0])
            self.assertEqual(client.call_args.kwargs['proxy'],'http://127.0.0.1:7897')
            self.assertFalse(client.call_args.kwargs['trust_env'])
            stream.close.assert_not_called()
            dial.reset_mock()
            response=httpx.Response(200,json={'Answer':[{'type':1,'data':'198.18.0.38'}]},request=httpx.Request('GET','https://dns.google/resolve'))
            client.return_value.__enter__.return_value.get.return_value=response
            with self.assertRaises(Exception):proxies.PublicNetworkBackend().connect_tcp('proxy.example.test',8080,10)
            dial.assert_not_called()
            stream.read.side_effect=[bytes([b]) for b in b'HTTP/1.1 502 Bad Gateway\r\n\r\n']
            with self.assertRaises(Exception):proxies.PublicNetworkBackend().connect_tcp('8.8.4.4',8080,10)
            stream.close.assert_called_once()
        with patch.dict(os.environ,{'LOCAL_EGRESS_PROXY':'http://8.8.8.8:7897'}):
            with self.assertRaises(ValueError):egress.local_proxy()

    def test_19_system_billing_reaches_payment_payloads(self):
        from backend.subscription import _payment_billing
        from backend.prolong.stripe_hosted import build_payment_method_form,StripeHostedClient
        snapshot={'country':'US','state':'NH','city':'Concord','line1':'1 Example Street','postal_code':'03301','first_name':'Fixture','last_name':'User','email':'fixture@example.test'}
        address,name,email=_payment_billing(snapshot)
        form=build_payment_method_form(card_number='4242424242424242',exp_month='12',exp_year='2030',cvc='123',name=name,email=email,pk='pk_test_fixture',guid='fixture',muid='fixture',sid='fixture',client_session_id='fixture',cs='cs_test_fixture',checkout_config_id='fixture',runtime_version='fixture',address=address)
        for key,value in address.items():
            self.assertEqual(form[f'billing_details[address][{key}]'],snapshot[key])
        self.assertEqual(form['billing_details[name]'],'Fixture User')
        self.assertEqual(form['billing_details[email]'],snapshot['email'])
        client=StripeHostedClient(pk='pk_test_fixture')
        try:
            response=Mock(status_code=200,headers={},text='{}');response.json.return_value={'id':'pm_fixture'}
            with patch.object(client.session,'post',return_value=response) as post:
                client.update_tax_region('cs_test_fixture',address)
                tax_payload=post.call_args.kwargs['data']
                client.create_payment_method(form)
                payment_payload=post.call_args.kwargs['data']
                for key in address:
                    self.assertEqual(tax_payload[f'tax_region[{key}]'],snapshot[key])
                    self.assertEqual(payment_payload[f'billing_details[address][{key}]'],tax_payload[f'tax_region[{key}]'])
                self.assertEqual(payment_payload['billing_details[email]'],snapshot['email'])
                self.assertEqual(post.call_count,2)
        finally:client.session.close()
        for key in snapshot:
            missing=dict(snapshot);missing.pop(key)
            with self.subTest(missing=key),self.assertRaises(Exception):_payment_billing(missing)

    def test_20_generator_uses_local_preproxy(self):
        from generator import build_curl_proxy_args
        proxy={'type':'http','host':'proxy.example.test','port':'8080','url':'http://proxy.example.test:8080'}
        with patch.dict(os.environ,{'LOCAL_EGRESS_PROXY':'http://127.0.0.1:7897','LOCAL_EGRESS_SOCKS_PROXY':'socks5h://127.0.0.1:7897'}):
            self.assertEqual(build_curl_proxy_args(proxy),['--preproxy','socks5h://127.0.0.1:7897','-x',proxy['url']])
            with self.assertRaises(ValueError):build_curl_proxy_args(dict(proxy,type='socks5'))
        with patch.dict(os.environ,{'LOCAL_EGRESS_PROXY':'http://127.0.0.1:7897','LOCAL_EGRESS_SOCKS_PROXY':''}):
            with self.assertRaises(ValueError):build_curl_proxy_args(proxy)
        self.assertEqual(build_curl_proxy_args(proxy),['-x',proxy['url']])

    def trace_order(self):
        order_id=uid()
        owner=db.scalar("SELECT id FROM users WHERE email='admin'")
        product=db.scalar('SELECT id FROM products WHERE enabled ORDER BY sort LIMIT 1')
        db.execute("INSERT INTO orders(id,user_id,product_id,product_name,email,status,fee,workflow,idempotency_key,request_hash) VALUES($1,$2,$3,'离线日志测试','trace@example.test','processing',0,'link',$1,'fixture')",order_id,owner,product)
        db.execute('INSERT INTO subscription_runs(order_id,user_id) VALUES($1,$2)',order_id,owner)
        return order_id,owner,product

    def test_21_request_diagnostics_and_failure_isolation(self):
        import subprocess
        from backend import payment_trace as trace
        from generator import curl_request
        from backend.prolong.stripe_hosted import StripeHostedClient
        order_id,_,_=self.trace_order()
        sensitive='secret-token-4242424242424242'
        response=Mock(status_code=402,headers={'Request-Id':'req_fixture123456'},text=json.dumps({'error':sensitive}))
        client=StripeHostedClient(pk='pk_test_fixture')
        try:
            with trace.scope(order_id,1), patch.object(client.session,'post',return_value=response):
                with self.assertRaises(RuntimeError):client.update_tax_region('cs_test_fixture',{'country':'US','state':'NH','city':'Concord','line1':'1 Fixture','postal_code':'03301'})
            with trace.scope(order_id,1),patch('generator.subprocess.run',return_value=Mock(returncode=0,stdout='{}\n__HTTP_STATUS__:200')),patch('generator._find_curl',return_value='curl'):
                self.assertEqual(curl_request('POST','https://chatgpt.com/backend-api/payments/checkout',proxy_host='proxy.example.test',proxy_port=8080,json_body={'token':sensitive}), (200,'{}'))
            with trace.scope(order_id,1),patch('generator.subprocess.run',side_effect=subprocess.TimeoutExpired(['curl',sensitive],65)),patch('generator._find_curl',return_value='curl'):
                with self.assertRaises(subprocess.TimeoutExpired):curl_request('GET','https://ipinfo.io/json',proxy_host='proxy.example.test',proxy_port=8080)
            rows=trace.details(order_id)
            self.assertEqual(len(rows),6)
            self.assertEqual((rows[1]['state'],rows[1]['http_status'],rows[1]['request_id']),('failed',402,'req_fixture123456'))
            self.assertEqual(rows[3]['code'],'response_received')
            self.assertEqual(rows[5]['code'],'timeout')
            self.assertEqual(rows[5]['http_status'],0)
            self.assertEqual(rows[5]['diagnostics']['network_reason'],'timeout')
            self.assertTrue(all(e['elapsed_ms']>=0 for e in rows))
            with trace.scope(order_id,1):
                trace.record('network','failed',sensitive,request_id=sensitive,diagnostics={'token':sensitive,'request_kind':sensitive,'request_attempt':sensitive})
            serialized=json.dumps(trace.details(order_id),default=str)
            self.assertNotIn(sensitive,serialized)
            self.assertNotIn('proxy.example.test',serialized)
            self.assertNotIn('cs_test_fixture',serialized)
            self.assertNotIn('1 Fixture',serialized)
            response.status_code=200;response.text='{}';response.json.return_value={}
            with trace.scope(order_id,1),patch.object(client.session,'post',return_value=response),patch.object(db,'transaction',side_effect=RuntimeError(sensitive)),self.assertLogs('xufu.payment_trace',level='WARNING') as captured:
                self.assertEqual(client.update_tax_region('cs_test_fixture'),{})
            self.assertNotIn(sensitive,str(captured.output))
            before=len(trace.details(order_id))
            with self.assertRaises(RuntimeError),db.transaction() as q:
                trace.record('funds','completed','hold',order_id=order_id,q=q)
                raise RuntimeError('rollback')
            self.assertEqual(len(trace.details(order_id)),before)
        finally:client.session.close()

    def test_22_link_worker_real_trace_hooks(self):
        from backend import subscription,payment_trace as trace
        import generator
        order_id,owner,product=self.trace_order()
        job={'id':order_id,'user_id':owner,'fee':0,'country':'PH','keep_renewal':False,'workflow':'link','product_id':product,'seats':1,
             'credentials_enc':encrypt(json.dumps({'access_token':self.fake_session()['accessToken'],'account_id':'fixture'})),
             'credentials_expires_at':now()+timedelta(hours=1),'billing_snapshot':{},'trace_round':1}
        ip_response=httpx.Response(200,json={'ip':'8.8.4.4'},request=httpx.Request('GET','https://ipinfo.io/json'))
        checkout={'checkout_session_id':'cs_test_fixture','tag':'hosted_checkout_session','checkout_ui_mode':'hosted','url':'https://pay.openai.com/c/pay/cs_test_fixture'}
        responses=[Mock(returncode=0,stdout=json.dumps({'ip':'8.8.4.4','country':'PH'})+'\n__HTTP_STATUS__:200'),Mock(returncode=0,stdout=json.dumps(checkout)+'\n__HTTP_STATUS__:200')]
        with patch.object(subscription,'_acquire_order_proxy',return_value='http://proxy.example.test:8080'),patch.object(subscription,'proxy_client') as proxy,patch.object(generator,'check_ip_direct',return_value={}),patch.object(generator.subprocess,'run',side_effect=responses),patch.object(generator,'_find_curl',return_value='curl'),patch.object(subscription,'StripeHostedClient') as payment:
            proxy.return_value.__enter__.return_value.get.return_value=ip_response
            subscription.run_subscription(job)
            payment.assert_not_called()
        self.assertEqual(db.scalar('SELECT status FROM orders WHERE id=$1',order_id),'awaiting_payment')
        rows=trace.details(order_id)
        self.assertEqual(rows[-1]['code'],'link_ready')
        self.assertTrue(any(e['diagnostics'].get('request_kind')=='checkout_create' and e['http_status']==200 for e in rows))
        self.assertTrue(any(e['diagnostics'].get('request_kind')=='exit_probe' and e['state']=='completed' for e in rows))
        self.assertFalse(any(e['phase'] in ('card_prepare','confirming','result') for e in rows))
        failed,owner,product=self.trace_order();job=dict(job,id=failed)
        with patch.object(subscription,'_acquire_order_proxy',return_value='http://proxy.example.test:8080'),patch.object(subscription,'proxy_client',side_effect=httpx.ConnectTimeout('secret-proxy-credential')),patch.object(generator,'generate_pro_long_link') as generate:
            subscription.run_subscription(job)
            generate.assert_not_called()
        self.assertEqual(db.scalar('SELECT status FROM orders WHERE id=$1',failed),'failed')
        self.assertEqual(trace.details(failed)[-1]['code'],'timeout')
        self.assertNotIn('secret-proxy-credential',json.dumps(self.ok(self.client.get('/api/admin/orders/'+failed))))
        # Historical business rows remain historical, without invented HTTP metadata.
        legacy,_,_=self.trace_order()
        db.execute("INSERT INTO order_events(order_id,stage,message) VALUES($1,'failed','历史失败记录')",legacy)
        history=self.ok(self.client.get('/api/admin/orders/'+legacy))['trace']
        self.assertEqual(len(history),1)
        self.assertEqual(history[0]['source'],'business')
        self.assertNotIn('round',history[0]);self.assertNotIn('http_status',history[0])

    def test_23_trace_scopes_do_not_cross_orders(self):
        from backend import payment_trace as trace
        from threading import Barrier
        first,_,_=self.trace_order();second,_,_=self.trace_order()
        barrier=Barrier(2)
        def perform(order_id,kind):
            with trace.scope(order_id,1),trace.request(kind) as diagnostic:
                barrier.wait(timeout=5)
                diagnostic.response(200)
        with ThreadPoolExecutor(max_workers=2) as executor:
            jobs=[executor.submit(perform,first,'exit_probe'),executor.submit(perform,second,'checkout_create')]
            for job in jobs:job.result(timeout=10)
        self.assertEqual({e['diagnostics']['request_kind'] for e in trace.details(first)},{'exit_probe'})
        self.assertEqual({e['diagnostics']['request_kind'] for e in trace.details(second)},{'checkout_create'})
        trace.record('network','running')
        self.assertEqual(len(trace.details(second)),2)

    def test_24_payment_proxy_chain_and_production_routing(self):
        import requests
        import socketserver
        import threading
        from backend.payment_transport import payment_session,LocalHopAdapter
        from backend.prolong.stripe_hosted import StripeHostedClient
        requests_seen=[]
        class Hop(socketserver.StreamRequestHandler):
            def handle(self):
                self.connection.settimeout(3)
                def headers():
                    value=bytearray()
                    while not value.endswith(b'\r\n\r\n'):
                        part=self.rfile.read(1)
                        if not part:break
                        value.extend(part)
                    return bytes(value)
                requests_seen.append(headers())
                self.wfile.write(b'HTTP/1.1 200 Connection established\r\n\r\n');self.wfile.flush()
                requests_seen.append(headers())
                self.wfile.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 11\r\nConnection: close\r\n\r\n{"ok":true}');self.wfile.flush()
        server=socketserver.TCPServer(('127.0.0.1',0),Hop)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        remote='http://fixture:password@proxy.example.test:8080'
        try:
            with patch.dict(os.environ,{'LOCAL_EGRESS_PROXY':f'http://127.0.0.1:{server.server_address[1]}','HTTP_PROXY':'http://127.0.0.1:1','NO_PROXY':'*'}),patch('backend.egress.resolve_public_proxy',return_value='8.8.8.8') as dns:
                client=StripeHostedClient(pk='pk_test_fixture',proxy=remote)
                try:
                    self.assertIsInstance(client.session.get_adapter('https://api.stripe.com'),LocalHopAdapter)
                    self.assertTrue(client.session.verify)
                    self.assertFalse(client.session.trust_env)
                    response=client.session.get('http://target.example.test/probe',timeout=3)
                    self.assertEqual(response.json(),{'ok':True})
                    self.assertEqual(dns.call_args.args[0],'proxy.example.test')
                    with patch('backend.payment_transport.connect_via_local',side_effect=TimeoutError('fixture')):
                        with self.assertRaises(requests.exceptions.ProxyError):client.session.get('http://target.example.test/probe',timeout=1)
                    client.session.proxies.clear()
                    with self.assertRaises(requests.exceptions.ProxyError):client.session.get('https://target.example.test',timeout=1)
                finally:client.session.close()
                with self.assertRaises(ValueError):payment_session('socks5://proxy.example.test:8080')
            self.assertIn(b'CONNECT 8.8.8.8:8080 HTTP/1.1',requests_seen[0])
            self.assertNotIn(b'Proxy-Authorization',requests_seen[0])
            self.assertIn(b'GET http://target.example.test/probe HTTP/1.1',requests_seen[1])
            self.assertIn(b'Proxy-Authorization: Basic ',requests_seen[1])
            with patch.dict(os.environ,{'LOCAL_EGRESS_PROXY':'','HTTPS_PROXY':'http://127.0.0.1:1'}),payment_session(remote) as production:
                self.assertNotIsInstance(production.get_adapter('https://api.stripe.com'),LocalHopAdapter)
                self.assertEqual(production.proxies,{'http':remote,'https':remote})
                self.assertFalse(production.trust_env)
                self.assertTrue(production.verify)
        finally:
            server.shutdown();server.server_close();thread.join(timeout=3)

    def test_25_subscription_worker_billing_and_trace_offline(self):
        from backend import subscription,payment_trace as trace
        from backend.prolong.stripe_hosted import StripeHostedClient
        order_id,owner,product=self.trace_order()
        db.execute("UPDATE orders SET workflow='subscription',keep_renewal=true WHERE id=$1",order_id)
        snapshot=subscription._billing_snapshot({'country':'US','state':'OR','city':'Portland','line1':'1 Fixture Street','postal_code':'97201','first_name':'Fixture','last_name':'User'},'account@example.test')
        job={'id':order_id,'user_id':owner,'fee':0,'country':'PH','keep_renewal':True,'workflow':'subscription','product_id':product,'seats':1,
             'credentials_enc':encrypt(json.dumps({'access_token':'fixture','account_id':'fixture'})),
             'credentials_expires_at':now()+timedelta(hours=1),'billing_snapshot':snapshot,'trace_round':1,
             'card_enc':encrypt(json.dumps({'number':'4242424242424242','month':'12','year':'2030','cvc':'123'}))}
        db.execute('UPDATE subscription_runs SET credentials_enc=$2 WHERE order_id=$1',order_id,job['credentials_enc'])
        db.execute('INSERT INTO order_proxy_sessions(order_id,resource_id,endpoint_enc,expected_ip,expires_at) VALUES($1,$2,$3,$4,$5)',
                   order_id,'fixture',encrypt('http://proxy.example.test:8080'),'8.8.4.4',now()+timedelta(minutes=10))
        def followup_http(request):
            if request.url.host=='ipinfo.io':return httpx.Response(200,json={'ip':'8.8.4.4'})
            if request.url.path=='/checkout/verify':return httpx.Response(200,text='callback')
            if request.url.path=='/backend-api/subscriptions':return httpx.Response(200,json={'plan_type':{'pro5x':'standard','pro':'premium','team':'team','team_premium':'prolite'}.get(product,'premium'),'will_renew':True})
            raise AssertionError('Unexpected post-payment request')
        sent=[]
        def post(url,**kwargs):
            sent.append((url,kwargs['data']))
            payload=({'status':'open','init_checksum':'offline','config_id':'offline'} if url.endswith('/init') else
                     {'id':'pm_fixture'} if url.endswith('/payment_methods') else
                     {'status':'complete'} if url.endswith('/confirm') else {'total_summary':{'due':100},'currency':'usd'})
            response=Mock(status_code=200,headers={'Request-Id':'req_offline123456'},text=json.dumps(payload));response.json.return_value=payload
            return response
        with patch('requests.sessions.Session.request',side_effect=AssertionError('External network forbidden')), \
             patch('requests.sessions.Session.post',side_effect=post), \
             patch('generator.generate_pro_long_link',return_value={'ok':True,'payable':True,'hostedUrl':'https://pay.openai.com/c/pay/cs_test_fixture','checkoutSessionId':'cs_test_fixture'}), \
             patch.object(subscription,'_acquire_order_proxy',return_value='http://proxy.example.test:8080'), \
             patch.object(subscription,'_check_exit_ip',return_value='8.8.4.4'),patch.object(subscription,'_verify_exit_ip'), \
             patch.object(StripeHostedClient,'register_device_fingerprint',return_value=('offline','offline','offline')), \
             patch('backend.prolong.stripe_checksum.fetch_stripe_build_constants',return_value=Mock(version='offline')), \
             patch('backend.prolong.stripe_checksum.build_runtime_checksums',return_value={'version':'offline','js_checksum':'offline','rv_timestamp':'offline'}), \
             patch('backend.post_payment.proxy_client',side_effect=lambda *a:httpx.Client(transport=httpx.MockTransport(followup_http))):
            subscription.run_subscription(job)
        self.assertEqual(db.scalar('SELECT status FROM orders WHERE id=$1',order_id),'succeeded')
        self.assertEqual(len(sent),4)
        tax=sent[1][1];payment=sent[2][1]
        for key in ('country','state','city','line1','postal_code'):
            self.assertEqual(tax[f'tax_region[{key}]'],snapshot[key])
            self.assertEqual(payment[f'billing_details[address][{key}]'],snapshot[key])
        self.assertEqual(payment['billing_details[name]'],'Fixture User')
        self.assertEqual(payment['billing_details[email]'],'account@example.test')
        rows=trace.details(order_id)
        kinds={e['diagnostics']['request_kind'] for e in rows if e['http_status']==200}
        self.assertEqual(kinds,{'checkout_initialize','billing_update','payment_method_create','payment_confirm',
                               'exit_probe','merchant_return','subscription_read'})
        self.assertEqual((rows[-1]['phase'],rows[-1]['state']),('complete','completed'))
        self.assertNotIn('4242424242424242',json.dumps(rows,default=str))


    def test_26_admin_saved_checkout_projection(self):
        user_id,_,user=self.new_user()
        order_id,_,_=self.trace_order()
        hosted='https://pay.openai.com/c/pay/cs_test_saved_link'
        db.execute("UPDATE orders SET user_id=$2,workflow='subscription',status='succeeded' WHERE id=$1",order_id,user_id)
        db.execute("UPDATE subscription_runs SET user_id=$2,checkout_url_enc=$3,trace_round=2 WHERE order_id=$1",order_id,user_id,encrypt(hosted))
        generated=db.scalar("INSERT INTO order_events(order_id,stage,message) VALUES($1,'checkout_done','链接已生成') RETURNING created_at",order_id)
        try:
            before=db.one('SELECT * FROM subscription_runs WHERE order_id=$1',order_id)
            detail=self.ok(self.client.get('/api/admin/orders/'+order_id))
            links=detail['checkout_links']
            self.assertEqual(len(links),1)
            self.assertTrue(links[0]['available'])
            self.assertEqual(links[0]['url'],hosted)
            self.assertEqual(links[0]['url'],detail['checkout']['url'])
            self.assertEqual(links[0]['source'],'current_snapshot')
            self.assertIsNone(links[0]['round'])
            self.assertEqual(datetime.fromisoformat(links[0]['generated_at']),generated)
            self.assertEqual(db.one('SELECT * FROM subscription_runs WHERE order_id=$1',order_id),before)
            self.assertNotIn('checkout_links',self.ok(user.get('/api/orders/'+order_id)))
            self.assertEqual(user.get('/api/admin/orders/'+order_id).status_code,403)
            db.execute("UPDATE orders SET workflow='link' WHERE id=$1",order_id)
            self.assertEqual(self.ok(self.client.get('/api/admin/orders/'+order_id))['checkout_links'],links)
            db.execute("DELETE FROM order_events WHERE order_id=$1",order_id)
            self.assertIsNone(self.ok(self.client.get('/api/admin/orders/'+order_id))['checkout_links'][0]['generated_at'])
            db.execute("UPDATE subscription_runs SET checkout_url_enc='unreadable' WHERE order_id=$1",order_id)
            unavailable=self.ok(self.client.get('/api/admin/orders/'+order_id))['checkout_links']
            self.assertEqual(len(unavailable),1)
            self.assertFalse(unavailable[0]['available']);self.assertEqual(unavailable[0]['url'],'')
            db.execute("UPDATE subscription_runs SET checkout_url_enc='' WHERE order_id=$1",order_id)
            self.assertEqual(self.ok(self.client.get('/api/admin/orders/'+order_id))['checkout_links'],[])
        finally:user.close()


    def paid_followup_order(self, keep=False):
        from backend import subscription
        order_id,owner,_=self.trace_order()
        creds={'access_token':'secret-access-fixture','session_token':'secret-session-fixture','account_id':'account-fixture','email':'fixture@example.test'}
        db.execute("UPDATE orders SET workflow='subscription',keep_renewal=$2 WHERE id=$1",order_id,keep)
        db.execute("UPDATE subscription_runs SET credentials_enc=$2,checkout_session_id='cs_test_followup',trace_round=1 WHERE order_id=$1",order_id,encrypt(json.dumps(creds)))
        db.execute('INSERT INTO order_proxy_sessions(order_id,resource_id,endpoint_enc,expected_ip,expires_at) VALUES($1,$2,$3,$4,$5)',
                   order_id,'fixture',encrypt('http://proxy.example.test:8080'),'8.8.4.4',now()+timedelta(minutes=10))
        subscription._succeed_order(order_id,owner,0,'4242',100,'USD')
        return order_id,owner

    def paid_snapshot(self, order_id, owner):
        return {'user':db.one('SELECT balance,frozen FROM users WHERE id=$1',owner),
                'ledger':db.all('SELECT * FROM ledger WHERE order_id=$1 ORDER BY id',order_id),
                'payment':db.one('SELECT paid_at,payment_last4,payment_amount,payment_currency,checkout_url_enc,checkout_session_id,card_enc FROM subscription_runs WHERE order_id=$1',order_id),
                'proxy':db.one('SELECT * FROM order_proxy_sessions WHERE order_id=$1',order_id),
                'status':db.scalar('SELECT status FROM orders WHERE id=$1',order_id)}

    def test_27_post_payment_refresh_failure_recovery_and_charge_invariants(self):
        from backend import post_payment as pp,payment_trace as trace
        order_id,owner=self.paid_followup_order()
        before=self.paid_snapshot(order_id,owner)
        mode={'fail_refresh':True};requests=[];cancelled=[]
        def respond(request):
            requests.append(request.url.path)
            if request.url.host=='ipinfo.io':return httpx.Response(200,json={'ip':'8.8.4.4'})
            if request.url.path=='/checkout/verify':return httpx.Response(200,text='callback')
            if request.url.path=='/api/auth/session':
                return httpx.Response(403,text='<html>challenge</html>') if mode['fail_refresh'] else httpx.Response(200,json={'accessToken':'fresh-fixture','account':{'id':'account-fixture'}})
            if request.url.path=='/backend-api/subscriptions':
                return httpx.Response(200,json={'plan_type':'standard','current_period_end':2000000000,'will_renew':not cancelled})
            if request.url.path=='/backend-api/subscriptions/cancel':
                cp=json.loads(decrypt(db.scalar('SELECT checkpoint_enc FROM subscription_followups WHERE order_id=$1',order_id)))
                self.assertEqual(cp['phase'],'cancellation_requested')
                cancelled.append(True)
                raise httpx.ReadTimeout('simulated response loss')
            raise AssertionError('Unexpected endpoint')
        with patch.object(pp,'proxy_client',side_effect=lambda *a:httpx.Client(transport=httpx.MockTransport(respond))) as factory:
            result=pp.finish_payment(order_id,plan='pro5x',proxy_url='http://proxy.example.test:8080')
            self.assertEqual(result,{'state':'review','warning_code':'http_403'})
            self.assertEqual(db.one('SELECT activation,renewal FROM orders WHERE id=$1',order_id),{'activation':'active','renewal':'review'})
            self.assertEqual(cancelled,[])
            self.assertEqual(self.paid_snapshot(order_id,owner),before)
            mode['fail_refresh']=False
            self.assertEqual(pp.resume(order_id)['state'],'completed')
            self.assertEqual(len(cancelled),1)
            self.assertEqual(db.one('SELECT activation,renewal FROM orders WHERE id=$1',order_id),{'activation':'active','renewal':'disabled'})
            self.assertEqual(self.paid_snapshot(order_id,owner),before)
            count=len(requests)
            self.assertEqual(pp.resume(order_id)['state'],'completed')
            self.assertEqual(len(requests),count)
            self.assertEqual(factory.call_args_list[0].args,('http://proxy.example.test:8080',60))
        diagnostics=json.dumps(trace.details(order_id),default=str)
        self.assertNotIn('secret-access-fixture',diagnostics)
        self.assertNotIn('secret-session-fixture',diagnostics)
        self.assertNotIn('secret-access-fixture',db.scalar('SELECT checkpoint_enc FROM subscription_followups WHERE order_id=$1',order_id))
        self.assertTrue(any(e['phase']=='session_refresh' and e['http_status']==403 for e in trace.details(order_id)))

    def test_28_followup_locks_and_rejects_unpaid_or_missing_checkpoints(self):
        from backend import post_payment as pp
        from threading import Event
        order_id,owner=self.paid_followup_order(keep=True)
        with self.assertRaisesRegex(pp.FollowupError,'checkpoint_missing'):pp.resume(order_id)
        unpaid,_,_=self.trace_order()
        with self.assertRaisesRegex(pp.FollowupError,'payment_evidence_missing'):pp.resume(unpaid)
        entered,release=Event(),Event()
        def respond(request):
            if request.url.host=='ipinfo.io':return httpx.Response(200,json={'ip':'8.8.4.4'})
            if request.url.path=='/checkout/verify':return httpx.Response(200)
            if request.url.path=='/backend-api/subscriptions':
                entered.set()
                if not release.wait(10):raise AssertionError('Concurrency fixture timed out')
                return httpx.Response(200,json={'plan_type':'standard','will_renew':True})
            raise AssertionError('Unexpected endpoint')
        with patch.object(pp,'proxy_client',side_effect=lambda *a:httpx.Client(transport=httpx.MockTransport(respond))),ThreadPoolExecutor(max_workers=1) as pool:
            first=pool.submit(pp.finish_payment,order_id,plan='pro5x')
            try:
                self.assertTrue(entered.wait(10))
                self.assertEqual(pp.resume(order_id)['state'],'busy')
            finally:release.set()
            self.assertEqual(first.result(timeout=10)['state'],'completed')

    def test_29_followup_uses_exact_order_proxy_and_freezes_on_ip_change(self):
        from backend import post_payment as pp
        order_id,owner=self.paid_followup_order()
        other,_=self.paid_followup_order()
        db.execute("UPDATE order_proxy_sessions SET expected_ip='1.1.1.1' WHERE order_id=$1",other)
        before=self.paid_snapshot(order_id,owner);business=[]
        def respond(request):
            if request.url.host!='ipinfo.io':business.append(request)
            return httpx.Response(200,json={'ip':'1.1.1.1'})
        with patch.object(pp,'proxy_client',side_effect=lambda *a:httpx.Client(transport=httpx.MockTransport(respond))):
            self.assertEqual(pp.finish_payment(order_id,plan='pro5x')['warning_code'],'proxy_ip_changed')
        self.assertEqual(business,[])
        self.assertEqual(self.paid_snapshot(order_id,owner),before)
        self.assertEqual(db.scalar('SELECT renewal FROM orders WHERE id=$1',order_id),'review')
        db.execute('UPDATE order_proxy_sessions SET expires_at=$2 WHERE order_id=$1',order_id,now()-timedelta(seconds=1))
        with patch.object(pp,'proxy_client') as factory:
            self.assertEqual(pp.resume(order_id)['warning_code'],'proxy_session_changed')
            factory.assert_not_called()

    def test_30_followup_storage_failures_cannot_change_payment_or_funds(self):
        from backend import post_payment as pp
        order_id,owner=self.paid_followup_order()
        before=self.paid_snapshot(order_id,owner)
        with patch.object(pp,'_save',side_effect=RuntimeError('secret-database-fixture')),patch.object(pp,'proxy_client') as factory:
            self.assertEqual(pp.finish_payment(order_id,plan='pro5x')['state'],'review')
            factory.assert_not_called()
        self.assertEqual(self.paid_snapshot(order_id,owner),before)
        self.assertEqual(db.scalar('SELECT renewal FROM orders WHERE id=$1',order_id),'review')


    def test_31_recover_uncertain_cancel_queries_state_before_any_new_write(self):
        from backend import post_payment as pp
        order_id,owner=self.paid_followup_order()
        before=self.paid_snapshot(order_id,owner);state={'off':False,'query_down':False};writes=[]
        def respond(request):
            if request.url.host=='ipinfo.io':return httpx.Response(200,json={'ip':'8.8.4.4'})
            if request.url.path=='/checkout/verify':return httpx.Response(200)
            if request.url.path=='/api/auth/session':return httpx.Response(200,json={'accessToken':'fresh-fixture'})
            if request.url.path=='/backend-api/subscriptions':
                if state['query_down']:return httpx.Response(503,json={})
                return httpx.Response(200,json={'plan_type':'standard','current_period_end':2000000000,'will_renew':not state['off']})
            if request.url.path=='/backend-api/subscriptions/cancel':
                writes.append(True);state.update(off=True,query_down=True)
                return httpx.Response(200,json={})
            raise AssertionError('Unexpected endpoint')
        with patch.object(pp,'proxy_client',side_effect=lambda *a:httpx.Client(transport=httpx.MockTransport(respond))):
            self.assertEqual(pp.finish_payment(order_id,plan='pro5x')['warning_code'],'http_503')
            self.assertEqual(db.scalar('SELECT phase FROM subscription_followups WHERE order_id=$1',order_id),'cancellation_requested')
            state['query_down']=False
            self.assertEqual(pp.resume(order_id)['state'],'completed')
        self.assertEqual(len(writes),1)
        self.assertEqual(self.paid_snapshot(order_id,owner),before)


    def test_32_public_api_docs_match_real_key_routes_and_responses(self):
        spec=json.loads((ROOT/'backend/openapi.json').read_text(encoding='utf-8'))
        actual={(path,method) for path,operations in app.openapi()['paths'].items() if path.startswith('/api/v1/') for method in operations}
        documented={(path,method) for path,operations in spec['paths'].items() for method in operations}
        self.assertEqual(documented,actual)
        self.assertEqual(len(documented),8)
        user_id,_,user=self.new_user()
        try:
            key=self.ok(user.post('/api/keys',json={'name':'contract test'}))['key']
            headers={'Authorization':'Bearer '+key}
            product=db.scalar('SELECT id FROM products WHERE enabled ORDER BY sort LIMIT 1')
            for path,method in documented:
                if method!='get' or '{id}' in path:continue
                query='?product_id='+product if path.endswith('/quote') else ''
                result=self.ok(self.client.get(path+query,headers=headers))
                response_schema=spec['paths'][path][method]['responses']['200']['content']['application/json']['schema']
                if '$ref' in response_schema:response_schema=spec['components']['schemas'][response_schema['$ref'].split('/')[-1]]
                self.assertLessEqual(set(response_schema['properties']),set(result),path)
            for obsolete in ('can_check','automatic_running','quote_expires_at','attempt_id'):
                self.assertNotIn(obsolete,spec['components']['schemas']['Payment']['properties'])
            self.assertEqual(set(spec['components']['schemas']['PriceSnapshot']['properties']),{'standard','member_fee','membership'})
            page=(ROOT/'frontend/src/components/ApiDocs.vue').read_text(encoding='utf-8')
            for obsolete in ('workflow=checkout','prepare=true','automatic_running','can_check','Idempotency-Key'):
                self.assertNotIn(obsolete,page)
            self.assertEqual(self.client.post('/api/v1/orders',headers=headers,json={}).status_code,404)
        finally:user.close()


if __name__=='__main__':
    try:
        suite=unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:],module=sys.modules[__name__]) if len(sys.argv)>1 else unittest.defaultTestLoader.loadTestsFromTestCase(BackendTests)
        result=unittest.TextTestRunner(verbosity=2).run(suite)
        (ROOT/('.local/targeted-test-result.json' if len(sys.argv)>1 else '.local/test-result.json')).write_text(json.dumps({'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'passed':result.wasSuccessful(),'database':'disposable on 18972','external_requests':'mock only'},indent=2),encoding='utf-8')
    finally:
        db.close()
        with admin_conn.cursor() as cur:
            cur.execute('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s AND pid<>pg_backend_pid()',(TEST_DB,))
            cur.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(TEST_DB)))
        admin_conn.close()
    sys.exit(0 if result.wasSuccessful() else 1)
