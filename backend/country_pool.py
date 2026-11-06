"""IPRoyal country pools. Credentials and immutable task sessions stay server-side."""
import json
import re
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit, unquote

import httpx
from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool
from . import db
from .common import administrator, audit, body, config, decrypt, encrypt, integer, now, require, uid

router = APIRouter(prefix='/api/admin', dependencies=[Depends(administrator)])
PROXY_LOCK = 1886003
CLAIM_LOCK = 1886005
BASE = 'https://resi-api.iproyal.com/v1'
REFERENCE = json.loads((Path(__file__).parent/'data/country-currencies.json').read_text(encoding='utf-8'))
COUNTRY_REFERENCE = {r['code']: r for r in REFERENCE['countries']}
DEFAULTS = dict(username='', subuser_hash='', auth_type='credentials', lifetime_seconds=3600,
                max_parallel=20, renewal_country='PH', auto_sync=True,
                token_enc='', password_enc='', countries=[], synced_at='', checked_at='',
                last_error='', revision='', suppressed=[])
ERRORS = {
    'pool_not_configured':'国家代理池尚未配置完整', 'pool_auth_failed':'IPRoyal 认证失败，请检查 API 令牌及账户',
    'pool_rate_limited':'IPRoyal 接口请求受限，请稍后重试', 'pool_unavailable':'IPRoyal 接口暂时不可用',
    'pool_invalid_response':'IPRoyal 返回的代理或国家数据不符合要求',
    'pool_country_unavailable':'IPRoyal 当前不支持此国家，请检查国家同步结果',
    'pool_capacity':'国家代理池会话已占满，等待空闲资源',
    'proxy_mode_changed':'代理模式与本单绑定不一致，请核查原订单',
    'proxy_session_expired':'本单代理会话已过期',
}


class PoolError(RuntimeError):
    def __init__(self, code):
        self.post_payment_code = code
        super().__init__(code)


def settings(q=None):
    return {**DEFAULTS, **config('country_pool', q)}


def mode(q=None):
    return config('proxy', q).get('mode', 'manual')


def ready(cfg):
    return bool(cfg['token_enc'] and (cfg['subuser_hash'] if cfg['auth_type']=='subuser'
                                    else cfg['username'] and cfg['password_enc']))


def write(q, cfg):
    q.execute("INSERT INTO settings(key,value) VALUES('country_pool',$1) ON CONFLICT(key) DO UPDATE SET value=excluded.value", cfg)


def api(cfg, path, payload=None):
    if not cfg['token_enc']: raise PoolError('pool_not_configured')
    try:
        with httpx.Client(timeout=httpx.Timeout(20,connect=10), trust_env=False, follow_redirects=False) as client:
            with client.stream('POST' if payload is not None else 'GET', BASE+path,
                               headers={'Authorization':'Bearer '+decrypt(cfg['token_enc']), 'Accept':'application/json'},
                               **({'json':payload} if payload is not None else {})) as response:
                if response.status_code in (401,403): raise PoolError('pool_auth_failed')
                if response.status_code==429: raise PoolError('pool_rate_limited')
                if response.status_code>=500: raise PoolError('pool_unavailable')
                if response.status_code!=200: raise PoolError('pool_invalid_response')
                raw=bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw)>8_000_000: raise PoolError('pool_invalid_response')
                return json.loads(raw)
    except (httpx.HTTPError, OSError): raise PoolError('pool_unavailable') from None
    except (ValueError, TypeError): raise PoolError('pool_invalid_response') from None


def countries(cfg):
    value=api(cfg, '/access/countries')
    rows=value.get('countries') if isinstance(value,dict) else None
    if not isinstance(rows,list) or not 1<=len(rows)<=300: raise PoolError('pool_invalid_response')
    result=[]
    for row in rows:
        code=str(row.get('code','')).upper() if isinstance(row,dict) else ''
        if not re.fullmatch('[A-Z]{2}',code): raise PoolError('pool_invalid_response')
        ref=COUNTRY_REFERENCE.get(code,{})
        result.append(dict(code=code, name=ref.get('name',str(row.get('name',code))[:60]), currency=ref.get('currency','')))
    if len({r['code'] for r in result})!=len(result): raise PoolError('pool_invalid_response')
    return result


def active_sessions(q=None):
    q=q or db
    orders=q.scalar("SELECT count(*) FROM order_proxy_sessions s JOIN orders o ON o.id=s.order_id WHERE s.source_mode='country_pool' AND s.expires_at>now() AND (o.status IN ('queued','processing','review') OR (o.status='succeeded' AND o.workflow='subscription' AND (o.activation IN ('pending','review') OR o.renewal IN ('pending','review','retrying'))))")
    renewals=q.scalar("SELECT count(*) FROM renewal_proxy_sessions s JOIN renewal_tasks t ON t.id=s.task_id WHERE s.expires_at>now() AND t.status IN ('queued','processing','checking','review')")
    return orders+renewals


def public_settings(cfg):
    fields=('username','subuser_hash','auth_type','lifetime_seconds','max_parallel','renewal_country','auto_sync','synced_at','checked_at')
    return {**{k:cfg[k] for k in fields}, 'token_configured':bool(cfg['token_enc']),
            'password_configured':bool(cfg['password_enc']), 'ready':ready(cfg),
            'last_error':ERRORS.get(cfg['last_error'],''), 'country_count':len(cfg['countries'])}


@router.get('/country-pool')
def listing():
    cfg=settings()
    return dict(settings=public_settings(cfg), countries=cfg['countries'], mode=mode(), in_use=active_sessions())


def save_config(value, request=None):
    allowed={'username','password','token','subuser_hash','auth_type','lifetime_seconds','max_parallel','renewal_country','auto_sync','import_resource_id'}
    require(isinstance(value,dict) and not set(value)-allowed,'国家代理池配置字段无效')
    with db.transaction() as q:
        q.execute('SELECT pg_advisory_xact_lock($1)',PROXY_LOCK)
        cfg=settings(q)
        imported=value.get('import_resource_id')
        if imported:
            row=q.one('SELECT endpoint_enc,vendor FROM resources WHERE id=$1',imported)
            require(row is not None,'代理资源不存在',404)
            parsed=urlsplit(decrypt(row['endpoint_enc']))
            require(row['vendor']=='iproyal' or (parsed.hostname or '').endswith('.iproyal.com'),'请选择 IPRoyal 代理')
            password=unquote(parsed.password or '')
            # Supplier options belong to each order, never to the base credentials.
            base=re.split(r'_(?:country|city|state|isp|session|lifetime|killswitch|region|countryset|streaming|skipisp)-',password,flags=re.I)[0]
            require(parsed.username and base,'所选代理缺少账号或密码')
            cfg.update(username=unquote(parsed.username),password_enc=encrypt(base),auth_type='credentials')
            match=re.search(r'_lifetime-(\d+)([smh])(?:_|$)',password,re.I)
            if match:cfg['lifetime_seconds']=max(60,min(86400,int(match[1])*{'s':1,'m':60,'h':3600}[match[2].lower()]))
        for key in ('username','password','token','subuser_hash'):
            val=value.get(key,'')
            require(isinstance(val,str) and len(val)<=1000 and not any(c in val for c in '\r\n\x00'),'代理凭据格式无效')
            if key in ('password','token'):
                if val:cfg[key+'_enc']=encrypt(val.strip())
            elif key in value:cfg[key]=val.strip()
        for key in ('auth_type','lifetime_seconds','max_parallel','renewal_country','auto_sync'):
            if key in value:cfg[key]=value[key]
        require(cfg['auth_type'] in ('credentials','subuser'),'账户认证方式无效')
        integer(cfg['lifetime_seconds'],60,86400,'粘性会话有效期须为 60～86400 秒')
        integer(cfg['max_parallel'],1,100,'并发上限须为 1～100')
        require(type(cfg['auto_sync']) is bool and isinstance(cfg['renewal_country'],str) and re.fullmatch('[A-Z]{2}',cfg['renewal_country']),'国家代理池配置无效')
        if mode(q)=='country_pool': require(ready(cfg),'启用中的国家代理池必须保留完整凭据')
        old=settings(q)
        connection_changed=any(cfg[k]!=old[k] for k in ('token_enc','username','password_enc','subuser_hash','auth_type'))
        cfg['revision']=uid()
        if connection_changed:cfg.update(checked_at='',synced_at='',countries=[],last_error='')
        write(q,cfg)
        if request:audit(q,request,'country_pool.saved','iproyal','保存国家代理池配置（凭据不记录）')
    return {'ok':True}


@router.put('/country-pool')
async def save(request:Request):
    value=await body(request)
    return await run_in_threadpool(save_config,value,request)


def sync(request=None):
    cfg=settings()
    try:rows=countries(cfg)
    except PoolError as exc:
        with db.transaction() as q:
            q.execute('SELECT pg_advisory_xact_lock($1)',PROXY_LOCK)
            latest=settings(q)
            if latest['revision']==cfg['revision']:
                latest.update(last_error=str(exc),last_sync_attempt=now().isoformat());write(q,latest)
        raise
    from .regions import items
    with db.transaction() as q:
        q.execute('SELECT pg_advisory_xact_lock($1)',PROXY_LOCK)
        latest=settings(q)
        require(latest['revision']==cfg['revision'],'配置已经变化，请重新同步',409)
        q.execute("SELECT value FROM settings WHERE key='regions' FOR UPDATE")
        old=items(q);known={r['code'] for r in old};added=[]
        for row in rows:
            if row['code'] not in known and row['code'] not in latest['suppressed']:
                added.append({**row,'enabled':False,'sort':min(10000,len(old)+len(added))})
        require(len(old)+len(added)<=300,'地区数量超过上限')
        region_cfg=config('regions',q)
        q.execute("UPDATE settings SET value=$1 WHERE key='regions'",{**region_cfg,'items':old+added})
        latest.update(countries=rows,synced_at=now().isoformat(),last_sync_attempt=now().isoformat(),checked_at=now().isoformat(),last_error='')
        write(q,latest)
        if request:audit(q,request,'country_pool.synced','iproyal',f'同步 {len(rows)} 个国家，新增 {len(added)} 个待开放地区')
    return dict(ok=True,countries=len(rows),added=len(added))


@router.post('/country-pool/sync')
def sync_route(request:Request):
    try:return sync(request)
    except PoolError as exc:require(False,ERRORS[str(exc)],502)


@router.post('/country-pool/test')
def test_connection():
    cfg=settings()
    try:rows=countries(cfg)
    except PoolError as exc:require(False,ERRORS[str(exc)],502)
    return dict(ok=True,countries=len(rows),message='API 连接正常；代理账户可通过指定国家测试验证')


def auto_sync():
    from datetime import datetime
    cfg=settings()
    if not cfg['auto_sync'] or not cfg['token_enc']:return
    last=cfg.get('last_sync_attempt') or cfg['synced_at']
    if last and (now()-datetime.fromisoformat(last)).total_seconds()<86400:return
    try:sync()
    except PoolError:pass


def lifetime_value(seconds):
    if seconds%3600==0:return str(seconds//3600)+'h'
    if seconds%60==0:return str(seconds//60)+'m'
    return str(seconds)+'s'


def generate(cfg,country,q):
    if not ready(cfg):raise PoolError('pool_not_configured')
    if country not in {r['code'] for r in cfg['countries']}:raise PoolError('pool_country_unavailable')
    payload=dict(format='{hostname}:{port}:{username}:{password}',hostname='geo.iproyal.com',
                 port='http|https',rotation='sticky',location='_country-'+country.lower(),
                 proxy_count=1,lifetime=lifetime_value(cfg['lifetime_seconds']))
    if cfg['auth_type']=='subuser':payload['subuser_hash']=cfg['subuser_hash']
    else:payload.update(username=cfg['username'],password=decrypt(cfg['password_enc']))
    from .proxies import normalize_proxy
    for _ in range(3):
        data=api(cfg,'/access/generate-proxy-list',payload)
        if not isinstance(data,list) or len(data)!=1 or not isinstance(data[0],str):raise PoolError('pool_invalid_response')
        try:
            endpoint=normalize_proxy(data[0]);parsed=urlsplit(endpoint);password=unquote(parsed.password or '')
            options=dict(re.findall(r'_(country|session|lifetime)-([^_]+)',password))
            lifetime=re.fullmatch(r'(\d+)([smh])',options.get('lifetime',''))
            valid=(parsed.hostname=='geo.iproyal.com' and parsed.port==12321 and parsed.username
                   and options.get('country','').lower()==country.lower()
                   and re.fullmatch('[A-Za-z0-9]{8}',options.get('session','')) and lifetime
                   and int(lifetime[1])*{'s':1,'m':60,'h':3600}[lifetime[2]]==cfg['lifetime_seconds'])
            if not valid:raise PoolError('pool_invalid_response')
        except (ValueError,TypeError):raise PoolError('pool_invalid_response') from None
        # A distinct supplier session per task; this does not claim unique exit IPs.
        session=options['session']
        if q.execute('INSERT INTO country_pool_sessions(session_id) VALUES($1) ON CONFLICT DO NOTHING',session):
            return endpoint,session
    raise PoolError('pool_invalid_response')


def acquire_order(q,order_id,country):
    cfg=settings(q)
    if active_sessions(q)>=cfg['max_parallel']:raise PoolError('pool_capacity')
    endpoint,session=generate(cfg,country,q)
    q.execute("INSERT INTO order_proxy_sessions(order_id,resource_id,endpoint_enc,expires_at,source_mode,session_ref) VALUES($1,'iproyal-country-pool',$2,$3,'country_pool',$4)",
              order_id,encrypt(endpoint),now()+timedelta(seconds=cfg['lifetime_seconds']),session)
    return endpoint


def acquire_renewal(q,task_id):
    existing=q.one('SELECT endpoint_enc,expires_at FROM renewal_proxy_sessions WHERE task_id=$1',task_id)
    if existing:
        if existing['expires_at']<=now():raise PoolError('proxy_session_expired')
        return decrypt(existing['endpoint_enc'])
    cfg=settings(q)
    if active_sessions(q)>=cfg['max_parallel']:raise PoolError('pool_capacity')
    endpoint,session=generate(cfg,cfg['renewal_country'],q)
    q.execute('INSERT INTO renewal_proxy_sessions(task_id,endpoint_enc,expires_at,session_ref,country) VALUES($1,$2,$3,$4,$5)',
              task_id,encrypt(endpoint),now()+timedelta(seconds=cfg['lifetime_seconds']),session,cfg['renewal_country'])
    return endpoint


def blockers(q):
    # Review is a stopped outcome, not evidence that a worker still uses a proxy.
    # Protect the brief paid -> follow-up handoff, plus actual follow-up locks
    # (including explicit recovery), without trapping old or deleted records.
    order_rows=q.all("""SELECT o.id FROM orders o WHERE
        (o.deleted_at IS NULL AND (
            o.status='processing'
            OR (o.status='queued' AND EXISTS(SELECT 1 FROM order_proxy_sessions s WHERE s.order_id=o.id))
            OR (o.status='succeeded' AND o.workflow='subscription'
                AND (o.activation='pending' OR o.renewal IN ('pending','retrying'))
                AND EXISTS(SELECT 1 FROM order_proxy_sessions s WHERE s.order_id=o.id AND s.expires_at>now())
                AND NOT EXISTS(SELECT 1 FROM subscription_followups f WHERE f.order_id=o.id
                    AND (f.warning_code<>'' OR f.phase='completed')))))
        OR EXISTS(SELECT 1 FROM pg_locks l WHERE l.locktype='advisory' AND l.granted
            AND l.database=(SELECT oid FROM pg_database WHERE datname=current_database())
            AND l.classid IN (1886011,1886012) AND l.objsubid=2
            AND l.objid::bigint=(hashtext(o.id)::bigint & 4294967295))
        ORDER BY o.updated_at DESC LIMIT 20""")
    renewal_rows=q.all("SELECT id FROM renewal_tasks WHERE lease_until>now() OR status IN ('processing','checking') OR (status='queued' AND EXISTS(SELECT 1 FROM renewal_proxy_sessions s WHERE s.task_id=renewal_tasks.id)) LIMIT 20")
    checks=q.scalar("SELECT count(*) FROM proxy_checks WHERE state IN ('queued','running')")
    leases=q.scalar('SELECT count(*) FROM proxy_leases WHERE expires_at>now()')
    return dict(orders=[r['id'] for r in order_rows],renewals=[r['id'] for r in renewal_rows],checks=checks,leases=leases)


def change_mode(q,target):
    require(target in ('manual','country_pool'),'代理模式无效')
    if target==mode(q):return
    busy=blockers(q)
    require(not any(busy.values()),'存在未结束的办理任务或代理占用，请等待完成；订单：'+(', '.join(x[:12] for x in busy['orders']) or '无')+'；续费任务：'+str(len(busy['renewals']))+'；检测/租约：'+str(busy['checks']+busy['leases']),409)
    queued=q.all("SELECT DISTINCT country FROM orders WHERE deleted_at IS NULL AND status='queued'")
    if target=='country_pool':
        cfg=settings(q);require(ready(cfg) and cfg['countries'],'请先配置国家代理池并同步国家')
        supported={r['code'] for r in cfg['countries']}
        require(cfg['renewal_country'] in supported,'默认续费国家不可用')
    else:
        dynamic=q.scalar("SELECT EXISTS(SELECT 1 FROM resources WHERE enabled AND vendor='iproyal' AND ip_mode='dynamic')")
        supported={r['country'] for r in queued} if dynamic else {r['country'] for r in q.all('SELECT DISTINCT country FROM resources WHERE enabled')}
    require(all(r['country'] in supported for r in queued),'目标模式不支持部分排队订单的国家',409)


@router.put('/proxy-mode')
async def switch(request:Request):
    value=await body(request);target=value.get('mode')
    def apply():
        with db.transaction() as q:
            q.execute('SELECT pg_advisory_xact_lock($1)',CLAIM_LOCK)
            q.execute('SELECT pg_advisory_xact_lock(1886004)')
            q.execute('SELECT pg_advisory_xact_lock($1)',PROXY_LOCK)
            change_mode(q,target)
            cfg=config('proxy',q);cfg['mode']=target
            q.execute("UPDATE settings SET value=$1 WHERE key='proxy'",cfg)
            audit(q,request,'proxy.mode',target,'切换当前代理模式')
        return {'ok':True}
    return await run_in_threadpool(apply)


@router.post('/country-pool/test-country')
async def test_country(request:Request):
    value=await body(request);country=value.get('country')
    def test():
        from .proxies import proxy_client,probe_openai
        try:
            with db.transaction() as q:
                q.execute('SELECT pg_advisory_xact_lock($1)',PROXY_LOCK)
                endpoint,session=generate(settings(q),country,q)
            with proxy_client(endpoint,15) as client:result=probe_openai(client)
            return dict(result,session=session[:3]+'…',country=country)
        except PoolError as exc:require(False,ERRORS[str(exc)],502)
        except httpx.HTTPError:require(False,'代理连接失败，请检查账户和套餐',502)
    return await run_in_threadpool(test)


def order_metadata(order_id):
    row=db.one('SELECT source_mode,session_ref,created_at,expires_at FROM order_proxy_sessions WHERE order_id=$1',order_id)
    if row:
        row['session_ref']=row['session_ref'][:3]+'…' if row['session_ref'] else '原订单会话'
    return row
