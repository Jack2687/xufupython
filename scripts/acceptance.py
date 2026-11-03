"""Read-only final resource verification, plus a temporary local login/logout."""
import base64
import hashlib
import json
import re
from pathlib import Path
import sys
import httpx
import psycopg2
from psycopg2.extras import RealDictCursor
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from dotenv import dotenv_values

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from backend import db
from backend.common import decrypt,now


def run():
    source=dotenv_values(ROOT.parent/'xupay/.env')
    conn=psycopg2.connect(source['DATABASE_URL'],connect_timeout=5)
    conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
    with conn,conn.cursor(cursor_factory=RealDictCursor) as c:
        c.execute("SELECT value FROM settings WHERE key='billing'");billing=c.fetchone()['value']
        c.execute('SELECT * FROM resources ORDER BY id');resources=[dict(r) for r in c.fetchall()]
    conn.close()
    assert db.scalar("SELECT value FROM settings WHERE key='billing'")==billing
    copied=db.all('SELECT * FROM resources ORDER BY id');assert len(copied)==len(resources)
    cipher=AESGCM(base64.b64decode(source['MASTER_KEY']))
    for original,copy in zip(resources,copied):
        raw=base64.b64decode(original['endpoint_enc']);plain=cipher.decrypt(raw[:12],raw[12:],None).decode()
        assert decrypt(copy['endpoint_enc'])==plain
        assert copy['endpoint_enc']!=original['endpoint_enc']
        for k in ('id','name','kind','country','vendor','ip_mode','enabled','weight','capacity','created_at'):
            assert copy[k]==original[k],k
    assert db.scalar('SELECT count(*) FROM users')==1
    assert db.scalar("SELECT count(*) FROM users WHERE email='admin' AND role='admin' AND balance=0 AND frozen=0")==1
    empty_tables=['orders','ledger','topups','redeem_codes','api_keys','renewal_tasks']
    for table in empty_tables:assert db.scalar('SELECT count(*) FROM '+table)==0,table
    with httpx.Client(base_url='http://127.0.0.1:18970',trust_env=False) as client:
        for path in ('/health/live','/health/ready'):assert client.get(path).json()['ok']
        assert client.post('/api/auth/login',json={'email':'admin','password':'admin'}).status_code==200
        assert len(client.get('/api/admin/billing-profiles').json()['items'])==len(billing['address_templates'])
        assert len(client.get('/api/admin/resources').json()['items'])==len(resources)
        for path in ('/api/orders','/api/v1/orders','/api/orders/example/payment/confirm'):
            assert client.post(path,json={}).status_code==404
        for asset in (ROOT/'webui/assets').iterdir():
            if asset.is_file():assert hashlib.sha256(client.get('/assets/'+asset.name).content).hexdigest()==hashlib.sha256(asset.read_bytes()).hexdigest()
        assert client.post('/api/auth/logout',json={}).status_code==200
    initial=json.loads((ROOT/'docs/frontend-manifest.json').read_text(encoding='utf-8-sig'))
    changed=[]
    for relative,expected in initial.items():
        if relative.startswith('frontend/') and hashlib.sha256((ROOT/relative).read_bytes()).hexdigest()!=expected:changed.append(relative)
    assert changed==['frontend/src/components/ConfigEditor.vue'],changed
    flow_select=re.search(r'<select v-model="form.subscription_flow"[^>]*>(.*?)</select>',(ROOT/'frontend/src/components/ConfigEditor.vue').read_text(encoding='utf-8'))
    flow_options=re.findall(r'<option value="([^"]+)"',flow_select[1]) if flow_select else []
    assert flow_options==['cslive','cslive_direct_pay']
    report={'checked_at':now().isoformat(),'url':'http://127.0.0.1:18970','health':True,'admin_login':True,'only_admin_user':True,'empty_business_tables':empty_tables,'billing_profiles':len(billing['address_templates']),'proxies':len(resources),'billing_exact':True,'proxy_plaintext_exact':True,'proxy_ciphertext_independent':True,'new_proxy_checks_preserved':db.scalar('SELECT count(*) FROM proxy_checks'),'frontend_source_changes':changed,'served_assets_match_build':True,'flow_selection_options':flow_options,'live_payment_or_renewal_performed':False}
    (ROOT/'.local/acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2));db.close()


if __name__=='__main__':run()
