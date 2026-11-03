"""One-time, transactional import of the two explicitly authorized resource pools."""
import argparse
import base64
import json
from pathlib import Path
import sys
import psycopg2
from psycopg2.extras import RealDictCursor
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from dotenv import dotenv_values

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from backend import db
from backend.common import encrypt,decrypt,digest


def run(source_env):
    source=dotenv_values(source_env)
    target=dotenv_values(ROOT/'.env')
    if source['DATABASE_URL']==target['DATABASE_URL'] or source['MASTER_KEY']==target['MASTER_KEY']:
        raise RuntimeError('Source and destination must be independent')
    source_conn=psycopg2.connect(source['DATABASE_URL'],connect_timeout=5)
    source_conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
    try:
        with source_conn,source_conn.cursor(cursor_factory=RealDictCursor) as c:
            c.execute("SELECT value FROM settings WHERE key='billing'");billing=c.fetchone()['value']
            c.execute("SELECT value FROM settings WHERE key='proxy'");proxy=c.fetchone()['value']
            c.execute('SELECT * FROM resources ORDER BY created_at,id');resources=[dict(r) for r in c.fetchall()]
    finally: source_conn.close()
    marker=ROOT/'.local/resource-import.json'
    with db.transaction() as q:
        q.execute('SELECT pg_advisory_xact_lock(1897201)')
        q.execute('CREATE TABLE IF NOT EXISTS local_imports(name text PRIMARY KEY,completed_at timestamptz NOT NULL DEFAULT now(),summary jsonb NOT NULL)')
        existing=q.scalar("SELECT summary FROM local_imports WHERE name='billing-proxies-v1'")
        if existing:
            print(json.dumps(dict(existing,already_imported=True)));return
        if q.scalar('SELECT count(*) FROM resources') or q.scalar("SELECT jsonb_array_length(value->'address_templates') FROM settings WHERE key='billing'"):
            raise RuntimeError('Destination already contains resources; refusing to overwrite')
        cipher=AESGCM(base64.b64decode(source['MASTER_KEY']))
        columns=[r['column_name'] for r in q.all("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name='resources' ORDER BY ordinal_position")]
        for row in resources:
            raw=base64.b64decode(row['endpoint_enc']);plain=cipher.decrypt(raw[:12],raw[12:],None).decode()
            row['endpoint_enc']=encrypt(plain)
            if decrypt(row['endpoint_enc'])!=plain: raise RuntimeError('Credential roundtrip failed')
            q.execute('INSERT INTO resources('+','.join(columns)+') VALUES('+','.join('$'+str(i+1) for i in range(len(columns)))+')',*[row[k] for k in columns])
        q.execute("UPDATE settings SET value=$1 WHERE key='billing'",billing)
        p=q.scalar("SELECT value FROM settings WHERE key='proxy' FOR UPDATE")
        ids={r['id'] for r in resources}
        for key in ('default_id','global_id'):
            p[key]=proxy.get(key,'') if proxy.get(key,'') in ids else ''
        q.execute("UPDATE settings SET value=$1 WHERE key='proxy'",p)
        if q.scalar("SELECT value FROM settings WHERE key='billing'")!=billing: raise RuntimeError('Billing verification failed')
        summary={'billing_profiles':len(billing.get('address_templates',[])),'proxies':len(resources),'credentials_verified':len(resources),'independent_encryption':True}
        q.execute("INSERT INTO local_imports(name,summary) VALUES('billing-proxies-v1',$1)",summary)
    marker.write_text(json.dumps(summary,indent=2),encoding='utf-8')
    print(json.dumps(summary))
    db.close()


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source-env',required=True);args=p.parse_args();run(Path(args.source_env))
