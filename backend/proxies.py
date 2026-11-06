import ipaddress
import socket
import time
from contextlib import contextmanager
from urllib.parse import urlsplit, urlunsplit, quote
import httpx
import httpcore
from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool
from . import db
from .common import *
from .settings import COUNTRIES
from .egress import local_proxy, connect_via_local

router=APIRouter(prefix='/api/admin',dependencies=[Depends(administrator)])
FIELDS="""id,name,kind,country,vendor,ip_mode,enabled,weight,capacity,last_status,last_checked,last_ip,last_country,last_latency_ms,quality_result,openai_result,created_at,true AS has_credentials,
COALESCE(id=(SELECT value->>'global_id' FROM settings WHERE key='proxy'),false) AS is_global,
(SELECT id FROM proxy_checks c WHERE c.resource_id=resources.id AND c.state IN ('queued','running') ORDER BY c.created_at LIMIT 1) AS active_check,
(SELECT count(*) FROM proxy_leases l WHERE l.resource_id=resources.id AND l.expires_at>now()) AS in_use"""


def normalize_proxy(raw):
    require(isinstance(raw,str) and raw.strip(),'代理地址不能为空')
    raw=raw.strip()
    if '://' not in raw:
        parts=raw.split(':',3)
        raw=('http://'+quote(parts[2],safe='')+':'+quote(parts[3],safe='')+'@'+parts[0]+':'+parts[1]) if len(parts)==4 else 'http://'+raw
    try:
        u=urlsplit(raw)
        require(u.scheme in ('http','https','socks5','socks5h') and u.hostname and u.path in ('','/') and not u.query and not u.fragment and u.port and 1<=u.port<=65535,'代理格式或端口无效')
        require(u.hostname.lower()!='localhost' and not u.hostname.lower().endswith('.localhost'),'代理需为公网地址')
        try: addr=ipaddress.ip_address(u.hostname)
        except ValueError: addr=None
        require(addr is None or addr.is_global,'代理需为公网地址')
        return urlunsplit((u.scheme,u.netloc,'','',''))
    except ValueError:
        raise BusinessError('代理格式或端口无效')


class PublicNetworkBackend(httpcore.SyncBackend):
    def connect_tcp(self,host,port,timeout=None,local_address=None,socket_options=None):
        if local_proxy():
            return connect_via_local(host,port,timeout,local_address,socket_options)
        resolved=socket.getaddrinfo(host,port,type=socket.SOCK_STREAM)
        require(resolved and all(ipaddress.ip_address(r[4][0]).is_global for r in resolved),'代理需为公网地址')
        # Validate and pin every dial, preserving the original host for TLS/SNI.
        return super().connect_tcp(resolved[0][4][0],port,timeout,local_address,socket_options)


def proxy_client(endpoint,timeout=8):
    endpoint=normalize_proxy(endpoint)
    transport=httpx.HTTPTransport(proxy=endpoint,trust_env=False,retries=0)
    # httpx/httpcore versions are pinned in requirements.txt.
    transport._pool._network_backend=PublicNetworkBackend()
    return httpx.Client(transport=transport,timeout=timeout,trust_env=False,follow_redirects=False,headers={'Accept':'application/json','User-Agent':'Mozilla/5.0','Connection':'close'})


@router.get('/resources')
def resources():
    rows=db.all('SELECT '+FIELDS+',endpoint_enc FROM resources ORDER BY created_at')
    for row in rows:row['endpoint']=decrypt(row.pop('endpoint_enc'))
    return {'items':rows}


@router.post('/resources')
@router.put('/resources/{id}')
async def save_resource(request:Request,id:str=''):
    v=await body(request)
    return await run_in_threadpool(save_resource_value,v,request,id)


def save_resource_value(v,request,id=''):
    fresh=not id;id=id or uid()
    name=v.get('name','');country=v.get('country','')
    require(isinstance(name,str) and isinstance(country,str),'资源参数无效')
    name=name.strip();country=country.strip().upper()
    require(1<=length(name)<=80 and re.fullmatch('[A-Z]{2}',country) and type(v.get('enabled')) is bool,'资源参数无效')
    integer(v.get('weight'),1,100);integer(v.get('capacity'),1,100)
    if 'vendor' in v: require(v['vendor'] in ('iproyal','cliproxy','other'),'请选择代理厂商')
    if 'ip_mode' in v: require(v['ip_mode'] in ('dynamic','static'),'请选择动态或静态代理')
    if 'is_global' in v: require(type(v['is_global']) is bool,'全局代理必须为开关')
    endpoint=normalize_proxy(v.get('endpoint','')) if fresh or v.get('endpoint') else ''
    enc=encrypt(endpoint) if endpoint else ''
    with db.transaction() as q:
        q.execute('SELECT pg_advisory_xact_lock(1886003)')
        previous=q.one('SELECT country,vendor,ip_mode FROM resources WHERE id=$1 FOR UPDATE',id) if not fresh else {}
        require(fresh or previous is not None,'代理不存在',404)
        from .regions import items as region_items
        require(country in {r['code'] for r in region_items(q)} or (not fresh and previous['country']==country),'请选择已配置的订阅地区')
        vendor=v.get('vendor',previous.get('vendor','iproyal' if endpoint and urlsplit(endpoint).hostname=='geo.iproyal.com' else 'other'))
        mode=v.get('ip_mode',previous.get('ip_mode','dynamic'))
        changed=not fresh and (enc or previous['country']!=country or previous['vendor']!=vendor or previous['ip_mode']!=mode)
        # Leases reserve capacity and can outlive the request; they are not an
        # edit lock. Running clients and order_proxy_sessions already retain
        # their endpoint snapshot. Resource edits apply to future acquisitions.
        if fresh:
            q.execute('INSERT INTO resources(id,name,country,endpoint_enc,enabled,weight,capacity,vendor,ip_mode) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)',id,name,country,enc,v['enabled'],v['weight'],v['capacity'],vendor,mode)
        else:
            q.execute("UPDATE resources SET name=$2,country=$3,endpoint_enc=CASE WHEN $4='' THEN endpoint_enc ELSE $4 END,enabled=$5,weight=$6,capacity=$7,vendor=$8,ip_mode=$9 WHERE id=$1",id,name,country,enc,v['enabled'],v['weight'],v['capacity'],vendor,mode)
            if changed: q.execute("UPDATE resources SET last_status='untested',last_checked=NULL,last_ip='',last_country='',last_latency_ms=0,quality_result='{}',openai_result='{}' WHERE id=$1",id)
        if v.get('is_global'):
            require(v['enabled'],'请先启用代理，再设为全局代理')
            q.execute("UPDATE settings SET value=jsonb_set(value,'{global_id}',to_jsonb($1::text)) WHERE key='proxy'",id)
        elif 'is_global' in v:
            q.execute("UPDATE settings SET value=jsonb_set(value,'{global_id}','\"\"') WHERE key='proxy' AND value->>'global_id'=$1",id)
        audit(q,request,'resource.saved',id,'保存代理资源')
    return {'id':id}


@router.post('/resources/{id}/global')
async def set_global(id:str,request:Request):
    await body(request)
    return await run_in_threadpool(set_global_value,id,request)


def set_global_value(id,request):
    with db.transaction() as q:
        q.execute('SELECT pg_advisory_xact_lock(1886003)')
        require(q.scalar('SELECT enabled FROM resources WHERE id=$1',id),'全局代理不存在或未启用')
        q.execute("UPDATE settings SET value=jsonb_set(value,'{global_id}',to_jsonb($1::text)) WHERE key='proxy'",id)
        audit(q,request,'proxy.global',id,'设置续费全局代理')
    return {'ok':True}


@router.delete('/resources/{id}')
def delete_resource(id:str,request:Request):
    with db.transaction() as q:
        q.execute('SELECT pg_advisory_xact_lock(1886003)')
        require(not q.scalar('SELECT count(*) FROM proxy_leases WHERE resource_id=$1 AND expires_at>now()',id),'代理正在执行任务，请等待结束后删除',409)
        q.execute('DELETE FROM proxy_leases WHERE resource_id=$1',id)
        require(q.execute('DELETE FROM resources WHERE id=$1',id)==1,'资源不存在',404)
        for name in ('global_id','default_id'):
            q.execute("UPDATE settings SET value=jsonb_set(value,ARRAY[$2::text],'\"\"') WHERE key='proxy' AND value->>$2=$1",id,name)
        audit(q,request,'resource.deleted',id,'删除资源')
    return {'ok':True}


def bounded_get(client,url):
    with client.stream('GET',url) as r:
        data=bytearray()
        for block in r.iter_bytes():
            data.extend(block[:8193-len(data)])
            if len(data)>8192: break
        return r.status_code,r.headers,bytes(data)


def probe_exit(client):
    start=time.monotonic();result={'ok':False,'ip':'','country':'','latency_ms':0}
    try:
        status,_,raw=bounded_get(client,'https://ipinfo.io/json')
        if status==200 and len(raw)<=8192:
            v=json.loads(raw);address=str(ipaddress.ip_address(v.get('ip','')))
            country=v.get('country','');result.update(ok=True,ip=address,country=country if isinstance(country,str) and re.fullmatch('[A-Z]{2}',country) else '')
    except (httpx.HTTPError,ValueError,TypeError,KeyError): pass
    result['latency_ms']=round((time.monotonic()-start)*1000)
    return result


def probe_openai(client):
    start=time.monotonic();result={'status':'fail','message':'连接失败或请求超时','http_status':0,'latency_ms':0}
    try:
        status,headers,raw=bounded_get(client,'https://api.openai.com/v1/models');text=raw.decode('utf-8','replace').lower()
        result['http_status']=status
        challenge=headers.get('cf-mitigated','').lower()=='challenge' or any(m in text for m in ('window._cf_chl_opt','just a moment','enable javascript and cookies to continue','__cf_chl_','challenge-platform')) or 'text/html' in headers.get('content-type','') and ('cloudflare' in text or 'challenge' in text)
        if status in (403,429) and challenge: result.update(status='challenge',message='遇到 Cloudflare 验证')
        elif status==401: result.update(status='pass',message='目标可达（未携带密钥，返回 401）')
        elif status==429: result.update(status='warn',message='目标返回限流，请稍后再测')
        elif status==403: result['message']='目标拒绝访问'
        else: result['message']='目标返回非预期状态'
    except httpx.TimeoutException: result.update(stage='response',reason='timeout',message='连接代理或等待目标响应超时')
    except httpx.HTTPError: result.update(stage='proxy_connect',reason='network',message='连接代理或建立代理隧道失败')
    result['latency_ms']=round((time.monotonic()-start)*1000)
    return result


def measure(client,kind):
    samples=[probe_exit(client),probe_exit(client)] if kind=='quality' else []
    ai=probe_openai(client)
    result={'version':2,'kind':kind,'checked_at':now().isoformat(),'openai':ai,'status':ai['status'],'message':ai['message']}
    if kind=='quality':
        ok=[s for s in samples if s['ok']];latency=sum(s['latency_ms'] for s in ok)/len(ok) if ok else None
        parts={'connectivity':15*len(ok),'stability':20 if len(ok)==2 else 0,'latency':0 if latency is None else 20 if latency<=500 else 15 if latency<=1500 else 10 if latency<=3000 else 5,'openai':30 if ai['status']=='pass' else 10 if ai['status']=='warn' else 0}
        score=sum(parts.values());grade=next((g for n,g in [(90,'A'),(75,'B'),(60,'C'),(40,'D'),(0,'F')] if score>=n),'F')
        result.update(samples=samples,parts=parts,score=score,grade=grade,stability=('stable' if ok[0]['ip']==ok[1]['ip'] else 'changed') if len(ok)==2 else 'unknown',status='fail' if not ok else 'warn' if len(ok)<2 or ai['status']!='pass' else 'pass',message='出口探测失败，请检查代理连接' if not ok else '检测完成，评分反映本次连通质量')
    return result


def acquire(q,id,seconds):
    row=q.one('SELECT id,endpoint_enc,capacity FROM resources WHERE id=$1 FOR UPDATE',id)
    require(row,'资源不存在',404)
    require(q.scalar('SELECT count(*) FROM proxy_leases WHERE resource_id=$1 AND expires_at>now()',id)<row['capacity'],'代理资源忙，请稍后重试',409)
    lease=uid();q.execute("INSERT INTO proxy_leases(id,resource_id,expires_at) VALUES($1,$2,now()+$3*interval '1 second')",lease,id,seconds)
    return row,lease


@router.post('/resources/{id}/test')
def quick_test(id:str,request:Request):
    from .country_pool import mode
    require(limit('proxytest:'+current(request)['id'],10),'测试过于频繁',429)
    with db.transaction() as q:
        q.execute('SELECT pg_advisory_xact_lock(1886003)')
        require(mode(q)=='manual','当前启用国家代理池，请在国家代理池中测试',409)
        row,lease=acquire(q,id,35)
    sample={'ok':False,'ip':'','country':'','latency_ms':0}
    try:
        with proxy_client(decrypt(row['endpoint_enc']),15) as c: sample=probe_exit(c)
    except Exception: pass
    finally: db.execute('DELETE FROM proxy_leases WHERE id=$1',lease)
    db.execute('UPDATE resources SET last_status=$2,last_checked=now(),last_ip=$3,last_country=$4,last_latency_ms=$5 WHERE id=$1 AND endpoint_enc=$6',id,'ok' if sample['ok'] else 'failed',sample['ip'],sample['country'],sample['latency_ms'],row['endpoint_enc'])
    return dict(sample,message='连接成功' if sample['ok'] else '连接失败，请检查代理或凭据')


@router.post('/resources/{id}/checks')
async def start_check(id:str,request:Request):
    v=await body(request)
    return await run_in_threadpool(start_check_value,id,request,v)


def start_check_value(id,request,v):
    from .country_pool import mode
    kind=v.get('kind');require(kind in ('quality','openai'),'请选择评分或 OpenAI 测试')
    require(limit('proxytest:'+current(request)['id'],10),'测试过于频繁',429)
    with db.transaction() as q:
        q.execute('SELECT pg_advisory_xact_lock(1886003)')
        require(mode(q)=='manual','当前启用国家代理池，请在国家代理池中测试',409)
        require(q.scalar('SELECT id FROM resources WHERE id=$1',id),'资源不存在',404)
        existing=q.one("SELECT id,kind FROM proxy_checks WHERE resource_id=$1 AND state IN ('queued','running')",id)
        if existing:
            require(existing['kind']==kind,'此代理已有测试在进行，请等待结果',409);return {'id':existing['id'],'replayed':True}
        require(q.scalar("SELECT count(*) FROM proxy_checks WHERE state IN ('queued','running')")<100,'测试队列已满',429)
        check=uid();q.execute('INSERT INTO proxy_checks(id,resource_id,kind) VALUES($1,$2,$3)',check,id,kind)
        audit(q,request,'proxy.check',id,kind)
    return {'id':check,'state':'queued'}


@router.get('/proxy-checks/{id}')
def check_result(id:str):
    row=db.one('SELECT id,resource_id,kind,state,result,created_at,updated_at FROM proxy_checks WHERE id=$1',id)
    require(row,'检测记录不存在',404);return row


def run_check():
    from .country_pool import mode
    with db.transaction() as q:
        q.execute('SELECT pg_advisory_xact_lock(1886003)')
        if mode(q)!='manual':return False
        q.execute("UPDATE proxy_checks SET state='queued',lease_token=NULL,lease_until=NULL WHERE state='running' AND lease_until<now()")
        if q.scalar("SELECT count(*) FROM proxy_checks WHERE state='running'")>=2: return False
        job=q.one("SELECT c.id,c.resource_id,c.kind FROM proxy_checks c JOIN resources r ON r.id=c.resource_id WHERE c.state='queued' AND (SELECT count(*) FROM proxy_leases l WHERE l.resource_id=r.id AND l.expires_at>now())<r.capacity ORDER BY c.created_at LIMIT 1 FOR UPDATE OF c SKIP LOCKED")
        if not job:return False
        row,lease=acquire(q,job['resource_id'],110);token=uid()
        q.execute("UPDATE proxy_checks SET state='running',lease_token=$2,lease_until=now()+interval '110 seconds',proxy_lease_id=$3,updated_at=now() WHERE id=$1",job['id'],token,lease)
    result={'version':2,'kind':job['kind'],'status':'fail','message':'代理连接失败，请检查资源配置','checked_at':now().isoformat(),'openai':{'status':'fail','message':'连接失败','http_status':0,'latency_ms':0}}
    try:
        with proxy_client(decrypt(row['endpoint_enc'])) as c: result=measure(c,job['kind'])
    except Exception: pass
    with db.transaction() as q:
        q.execute('SELECT pg_advisory_xact_lock(1886003)')
        changed=q.execute("UPDATE proxy_checks SET state='completed',result=$3,lease_token=NULL,lease_until=NULL,updated_at=now() WHERE id=$1 AND lease_token=$2",job['id'],token,result)
        if changed:
            field='quality_result' if job['kind']=='quality' else 'openai_result'
            q.execute('UPDATE resources SET '+field+'=$2 WHERE id=$1 AND endpoint_enc=$3',job['resource_id'],result,row['endpoint_enc'])
        q.execute('DELETE FROM proxy_leases WHERE id=$1',lease)
    return True
