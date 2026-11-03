"""Manage only this project's local application and PostgreSQL instance."""
import argparse
import base64
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1]
LOCAL=ROOT/'.local'
PG=Path(os.getenv('PG_BIN','C:/Program Files/PostgreSQL/18/bin'))
DATA=LOCAL/'postgres'
FLAGS=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
sys.path.insert(0,str(ROOT))


def port_open(port):
    try:
        with socket.create_connection(('127.0.0.1',port),1): return True
    except OSError: return False


def pg_command(*args):
    with (LOCAL/'postgres-control.log').open('ab') as log:
        result=subprocess.run([str(PG/'pg_ctl.exe'),'-D',str(DATA),*args],stdout=log,stderr=log,creationflags=FLAGS,timeout=60)
    if result.returncode: raise RuntimeError('PostgreSQL command failed; see .local/postgres-control.log')


def initialize():
    from dotenv import dotenv_values
    import psycopg2
    from psycopg2 import sql
    LOCAL.mkdir(exist_ok=True)
    env_file=ROOT/'.env'
    if not env_file.exists():
        password=secrets.token_urlsafe(32)
        env_file.write_text('DATABASE_URL=postgresql://xufu_python:'+password+'@127.0.0.1:18972/xufu_python\nMASTER_KEY='+base64.b64encode(secrets.token_bytes(32)).decode()+'\nPOSTGRES_PASSWORD='+password+'\nPORT=18970\nLISTEN_HOST=127.0.0.1\nCOOKIE_SECURE=false\nBOOTSTRAP_EMAIL=admin\nBOOTSTRAP_PASSWORD=admin\n',encoding='utf-8')
    env=dotenv_values(env_file)
    if not (DATA/'PG_VERSION').exists():
        if port_open(18972): raise RuntimeError('Port 18972 is occupied by another service')
        pw=LOCAL/'init-password.tmp'
        try:
            pw.write_text(env['POSTGRES_PASSWORD'],encoding='utf-8')
            with (LOCAL/'postgres-init.log').open('ab') as log:
                subprocess.run([str(PG/'initdb.exe'),'-D',str(DATA),'-U','xufu_python','--pwfile='+str(pw),'--auth=scram-sha-256','--encoding=UTF8','--locale=C'],stdout=log,stderr=log,check=True,creationflags=FLAGS,timeout=60)
        finally: pw.unlink(missing_ok=True)
        with (DATA/'postgresql.conf').open('a',encoding='utf-8') as cfg:
            cfg.write("\nlisten_addresses='127.0.0.1'\nport=18972\ntimezone='Asia/Shanghai'\nmax_connections=60\n")
    if not port_open(18972): pg_command('-l',str(LOCAL/'postgres.log'),'-w','start')
    url=urlsplit(env['DATABASE_URL'])
    conn=psycopg2.connect(host=url.hostname,port=url.port,user=url.username,password=url.password,dbname='postgres',connect_timeout=5)
    conn.autocommit=True
    with conn.cursor() as c:
        c.execute("SELECT datname FROM pg_database WHERE datname='xufu_python'")
        if not c.fetchone(): c.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier('xufu_python')))
    conn.close()
    from backend import db
    from backend.seed import seed
    db.initialize();seed();db.close()
    print('Independent PostgreSQL ready on 18972; schema and default admin initialized.')


def start():
    initialize()
    if port_open(18970):
        print('Application port 18970 already listening; no duplicate started.');return
    with (LOCAL/'app.log').open('ab') as log:
        process=subprocess.Popen([sys.executable,str(ROOT/'scripts/serve.py')],cwd=ROOT,stdout=log,stderr=log,creationflags=FLAGS)
    (LOCAL/'app-process.json').write_text(json.dumps({'pid':process.pid,'script':str(ROOT/'scripts/serve.py'),'port':18970}),encoding='utf-8')
    for _ in range(60):
        if port_open(18970): print('Application started: http://127.0.0.1:18970');return
        if process.poll() is not None: break
        time.sleep(.25)
    raise RuntimeError('Application did not start; see .local/app.log')


def stop(include_db=False):
    record=LOCAL/'app-process.json'
    if record.exists():
        pid=int(json.loads(record.read_text(encoding='utf-8'))['pid'])
        # Validate the recorded process command before terminating its process tree.
        command=f"[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); Get-CimInstance Win32_Process -Filter 'ProcessId={pid}' | Select-Object -ExpandProperty CommandLine"
        result=subprocess.run(['powershell','-NoProfile','-Command',command],capture_output=True,text=True,encoding='utf-8',creationflags=FLAGS,timeout=10,check=True)
        line=result.stdout.replace('\\','/').lower()
        if str(ROOT/'scripts/serve.py').replace('\\','/').lower() in line:
            subprocess.run(['taskkill','/PID',str(pid),'/T','/F'],capture_output=True,creationflags=FLAGS,check=True)
        elif port_open(18970): raise RuntimeError('Recorded process does not match; refusing to stop another service')
        record.unlink(missing_ok=True)
    if include_db and (DATA/'postmaster.pid').exists(): pg_command('-m','fast','-w','stop')
    print('Project local services stopped.' if include_db else 'Project application stopped.')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['init','start','stop','restart','status']);parser.add_argument('--database',action='store_true');args=parser.parse_args()
    if args.action=='init': initialize()
    elif args.action=='start': start()
    elif args.action=='stop': stop(args.database)
    elif args.action=='restart': stop();start()
    else: print(json.dumps({'app_18970':port_open(18970),'postgres_18972':port_open(18972)}))
