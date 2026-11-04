"""Independent renewal cancellation only. No subscription purchase or checkout code."""
import base64
from datetime import datetime,timedelta,timezone
from http.cookies import SimpleCookie
from urllib.parse import quote
import httpx
from fastapi import APIRouter,Depends,Request
from . import db
from .common import *
from . import customer_contract
from .proxies import proxy_client,acquire

router=APIRouter(prefix='/api',dependencies=[Depends(member)])
FIELDS='id,email,status,message,created_at,updated_at'
COOKIE_NAME=re.compile(r'^__Secure-(?:next-auth|platform)\.session-token(?:\.[0-9]{1,2})?$')
BASE='https://chatgpt.com'


def inspect(raw):
    try:
        require(len(json.dumps(raw).encode())<=262144,'Session 最多 256 KB')
        s=json.loads(raw) if isinstance(raw,str) else raw
        require(isinstance(s,dict),'Session JSON 格式不正确')
        email=s.get('user',{}).get('email','').strip().lower();account=s.get('account',{}).get('id','');plan=s.get('account',{}).get('planType','');token=s.get('accessToken','')
        require(email_valid(email) and 1<=length(account)<=200 and 0<=length(plan)<=80 and 20<=length(token)<=32768,'Session 缺少有效账号邮箱、空间标识或 accessToken')
        parts=token.split('.');require(len(parts)==3,'accessToken 格式不正确')
        claims=json.loads(base64.urlsafe_b64decode(parts[1]+'='*(-len(parts[1])%4)))
        exp=claims.get('exp');require(type(exp) in (int,float) and 0<exp<=1e11 and int(exp)==exp,'Session 缺少有效的 Token 到期时间')
        expiry=datetime.fromtimestamp(exp,timezone.utc)
        if s.get('expires'): expiry=min(expiry,parsedate(s['expires']))
        require(expiry>now(),'Session 已过期，请重新获取')
        require(not claims.get('nbf') or claims['nbf']<=now().timestamp()+60,'Session 尚未生效')
        require(s.get('error') in (None,''),'Session 包含登录错误')
        profile=claims.get('https://api.openai.com/profile',{});auth=claims.get('https://api.openai.com/auth',{})
        require(not profile.get('email') or profile['email'].lower()==email,'Session 账号信息不一致')
        require(not auth.get('chatgpt_account_id') or auth['chatgpt_account_id']==account,'Session 空间信息不一致')
        return s,{'email':email,'account_id':account,'plan':plan,'expires_at':expiry,'status':'format_valid','online_verified':False}
    except (ValueError,TypeError,AttributeError,OverflowError,KeyError):
        raise BusinessError('Session JSON 或 accessToken 格式不正确')


def credentials(s,info):
    token=s.get('sessionToken','');cookies=s.get('cookies','')
    require(0<=length(token)<=32768 and 0<=length(cookies)<=65536 and not any(c in cookies for c in '\r\n'),'Session Cookie 格式不正确')
    require(not token or re.fullmatch(r'[\x21-\x7e]+',token) and not any(c in token for c in ';,"\\'),'Session Token 格式不正确')
    jar=SimpleCookie()
    try: jar.load(cookies)
    except Exception: raise BusinessError('Session Cookie 格式不正确')
    selected={k:m.value for k,m in jar.items() if COOKIE_NAME.fullmatch(k)}
    expiry=info['expires_at']
    if token or selected:
        expiry=now()+timedelta(hours=24)
        if s.get('expires'): expiry=min(expiry,parsedate(s['expires']))
    c={'access_token':s['accessToken'],'account_id':info['account_id'],'email':info['email'],'token_expires_at':info['expires_at'].isoformat(),'session_token':token,'cookies':selected}
    return c,min(expiry,now()+timedelta(hours=24))


@router.post('/session/inspect')
@router.post('/v1/session/inspect')
async def inspect_session(request:Request):
    v=await body(request)
    require(limit('session-inspect:'+current(request)['id'],20),'检查过于频繁',429)
    _,info=inspect(v.get('session'));return info


@router.get('/renewals')
def tasks(request:Request):
    return {'items':db.all('SELECT '+FIELDS+' FROM renewal_tasks WHERE user_id=$1 ORDER BY created_at DESC LIMIT 200',current(request)['id'])}


@router.get('/renewals/{id}')
def task(id:str,request:Request):
    row=db.one('SELECT '+FIELDS+' FROM renewal_tasks WHERE id=$1 AND user_id=$2',id,current(request)['id'])
    require(row,'记录不存在',404)
    return {'task':row,'events':db.all('SELECT status,message,created_at FROM renewal_events WHERE task_id=$1 ORDER BY id',id)}


@router.post('/renewals')
async def create_task(request:Request):
    v=await body(request);require(v.get('accepted') is True,'请确认关闭自动续费')
    s,info=inspect(v.get('session'));key=request.headers.get('Idempotency-Key','');require(8<=len(key)<=120,'请提供 8～120 位 Idempotency-Key')
    user=current(request);cfg=config('risk');require(limit('renewal-create:'+user['id'],cfg['order_per_minute']),'提交过于频繁',429)
    c,expiry=credentials(s,info);enc=encrypt(json.dumps(c));ah=digest(info['account_id'])
    with db.transaction() as q:
        q.execute('SELECT pg_advisory_xact_lock(1886002)')
        api_request=hasattr(request.state,'customer_hash')
        if api_request:
            saved=customer_contract.replay(q,request,'renewal')
            if saved:
                saved['status']=q.scalar('SELECT status FROM renewal_tasks WHERE id=$1',saved['id'])
                return saved
        old=q.one('SELECT k.task_id,k.account_hash,t.status FROM renewal_request_keys k JOIN renewal_tasks t ON t.id=k.task_id WHERE k.user_id=$1 AND k.key=$2',user['id'],key)
        if old:
            require(not api_request,'该请求标识已用于网页申请，请使用新的 Idempotency-Key',409)
            require(old['account_hash']==ah,'同一请求标识不能用于不同账号',409)
            return {'id':old['task_id'],'status':old['status'],'replayed':True}
        previous=q.one("SELECT id,user_id,status FROM renewal_tasks WHERE account_hash=$1 AND status IN ('queued','processing','checking','review') FOR UPDATE",ah)
        if previous:
            require(previous['user_id']==user['id'],'该账号已有未结束的续费申请',409)
            id=previous['id'];status=previous['status']
            if status=='review':
                # cancel_started is a permanent write fence and must survive fresh authorization.
                q.execute("UPDATE renewal_tasks SET credentials_enc=$2,credentials_expires_at=$3,status='queued',checks=0,next_at=now(),updated_at=now(),message='已更新授权，等待继续核实。' WHERE id=$1",id,enc,expiry)
                status='queued';q.execute("INSERT INTO renewal_events(task_id,status,message) VALUES($1,'queued','已更新授权，等待继续核实。')",id)
        else:
            require(q.scalar("SELECT count(*) FROM renewal_tasks WHERE user_id=$1 AND status IN ('queued','processing','checking','review')",user['id'])<cfg['max_open_orders'],'未结束的续费申请已达上限')
            id=uid();status='queued'
            q.execute('INSERT INTO renewal_tasks(id,user_id,email,account_hash,credentials_enc,credentials_expires_at) VALUES($1,$2,$3,$4,$5,$6)',id,user['id'],info['email'],ah,enc,expiry)
            q.execute("INSERT INTO renewal_events(task_id,status,message) VALUES($1,'queued','申请已受理，等待处理；不收取服务费。')",id)
        q.execute('INSERT INTO renewal_request_keys(user_id,key,account_hash,task_id) VALUES($1,$2,$3,$4)',user['id'],key,ah,id)
        audit(q,request,'renewal_request',id,'提交独立关闭续费申请')
        if api_request:customer_contract.remember(q,request,'renewal',{'id':id,'status':status,'replayed':bool(previous)})
    return {'id':id,'status':status,'replayed':bool(previous)}


class ProviderError(Exception):
    pass


class RenewalSession:
    def __init__(self,client,c,persist):
        self.client,self.c,self.persist=client,c,persist;self.attempted=False

    def headers(self):
        c=self.c
        h={'Authorization':'Bearer '+c['access_token'],'Chatgpt-Account-Id':c['account_id'],'Accept':'application/json','Origin':BASE,'Referer':BASE+'/','User-Agent':'Mozilla/5.0'}
        if c.get('cookies'):h['Cookie']='; '.join(k+'='+v for k,v in sorted(c['cookies'].items()))
        return h

    def request(self,method,path,headers=None,payload=None):
        from . import payment_trace as trace
        with trace.request(trace.request_kind(BASE+path)) as diagnostic, self.client.stream(method,BASE+path,headers=headers or self.headers(),json=payload) as r:
            diagnostic.response(r.status_code, r.headers)
            raw=bytearray()
            for part in r.iter_bytes():
                raw.extend(part[:262145-len(raw)])
                if len(raw)>262144:raise ProviderError('invalid_response')
            try: v=json.loads(raw)
            except ValueError: raise ProviderError('invalid_response')
            if r.status_code==401:raise ProviderError('invalid_session')
            if r.status_code==404 and isinstance(v,dict) and 'subscription_not_found' in json.dumps(v):
                diagnostic.code='subscription_absent'
                return {'is_active':False,'will_renew':False}
            if r.status_code<200 or r.status_code>=300 or not isinstance(v,dict):raise ProviderError('provider_rejected')
            return v

    def refresh(self):
        self.attempted=True;c=self.c;cookies=dict(c.get('cookies',{}))
        if not cookies and c.get('session_token'):
            cookies={k:c['session_token'] for k in ('__Secure-next-auth.session-token','__Secure-platform.session-token')}
        if not cookies:raise ProviderError('invalid_session')
        self.client.cookies.clear()
        for k,v in cookies.items():self.client.cookies.set(k,v,domain='chatgpt.com',path='/')
        h=self.headers();h.pop('Authorization',None);h.pop('Cookie',None)
        h.update({'X-Openai-Target-Path':'/api/auth/session','X-Openai-Target-Route':'/api/auth/session'})
        v=self.request('GET','/api/auth/session?refresh=true&reason=token_expired',headers=h)
        try: _,info=inspect(v)
        except BusinessError:raise ProviderError('invalid_session')
        if info['account_id']!=c['account_id'] or info['email']!=c['email']:raise ProviderError('account_mismatch')
        fresh={x.name:x.value for x in self.client.cookies.jar if COOKIE_NAME.fullmatch(x.name) and x.domain.lstrip('.')=='chatgpt.com'}
        # A replacement unchunked cookie supersedes chunks of that family.
        for k in list(fresh):
            if re.search(r'\.\d+$',k) and k.rsplit('.',1)[0] in fresh:del fresh[k]
        c.update(access_token=v['accessToken'],token_expires_at=info['expires_at'].isoformat(),cookies=fresh,session_token='',refreshed_at=now().isoformat())
        self.persist(c)

    def prepare(self):
        if self.c.get('cookies') or self.c.get('session_token'):
            if not self.c.get('refreshed_at') or parsedate(self.c['refreshed_at'])<now()-timedelta(minutes=1) or parsedate(self.c['token_expires_at'])<=now()+timedelta(minutes=1):self.refresh()
        elif parsedate(self.c['token_expires_at'])<=now():raise ProviderError('invalid_session')

    def status(self):
        path='/backend-api/subscriptions?account_id='+quote(self.c['account_id'],safe='')
        try:v=self.request('GET',path)
        except ProviderError as e:
            if str(e)!='invalid_session' or self.attempted or not (self.c.get('cookies') or self.c.get('session_token')):raise
            self.refresh();v=self.request('GET',path)
        if v.get('account_id') and v['account_id']!=self.c['account_id']:raise ProviderError('account_mismatch')
        active=v.get('is_active',v.get('has_active_subscription'));renew=v.get('will_renew')
        if type(active) is not bool or active and type(renew) is not bool:raise ProviderError('invalid_response')
        return active,renew is True


def claim():
    with db.transaction() as q:
        q.execute('SELECT pg_advisory_xact_lock(1886004)');cfg=config('payment',q)
        if q.scalar('SELECT count(*) FROM renewal_tasks WHERE lease_until>now()')>=cfg['max_parallel']:return None
        row=q.one("SELECT * FROM renewal_tasks t WHERE t.status IN ('queued','processing','checking') AND t.next_at<=now() AND (t.lease_until IS NULL OR t.lease_until<now()) AND (SELECT count(*) FROM renewal_tasks x WHERE x.user_id=t.user_id AND x.lease_until>now())<$1 ORDER BY t.next_at,t.created_at LIMIT 1 FOR UPDATE OF t SKIP LOCKED",cfg['per_user_parallel'])
        if not row:return None
        token=uid();q.execute("UPDATE renewal_tasks SET status='processing',message='正在检查续费设置。',lease_token=$2,lease_until=now()+interval '110 seconds',checks=checks+1,updated_at=now() WHERE id=$1",row['id'],token)
        q.execute("INSERT INTO renewal_events(task_id,status,message) VALUES($1,'processing','正在检查续费设置。')",row['id'])
        row.update(lease_token=token,checks=row['checks']+1);return row


def run_task(job,client_factory=proxy_client):
    id=job['id'];token=job['lease_token'];cfg=config('payment');state='checking';message='正在核实续费设置，稍后自动更新。';lease=None
    try:
        if not job['credentials_enc'] or job['credentials_expires_at']<=now():raise ProviderError('invalid_session')
        c=json.loads(decrypt(job['credentials_enc']))
        try:
            with db.transaction() as q:
                q.execute('SELECT pg_advisory_xact_lock(1886003)')
                resource=q.scalar("SELECT r.id FROM resources r JOIN settings s ON s.key='proxy' AND r.id=s.value->>'global_id' WHERE r.enabled")
                require(resource,'全局代理不可用',409);row,lease=acquire(q,resource,110)
            client=client_factory(decrypt(row['endpoint_enc']),20)
        except Exception:
            if lease:db.execute('DELETE FROM proxy_leases WHERE id=$1',lease)
            db.execute("UPDATE renewal_tasks SET status='queued',message='等待办理资源，稍后自动重试。',checks=GREATEST(checks-1,0),next_at=now()+$3*interval '1 second',lease_token=NULL,lease_until=NULL,updated_at=now() WHERE id=$1 AND lease_token=$2",id,token,cfg['renewal_check_interval_seconds'])
            return
        def persist(value):
            require(db.execute('UPDATE renewal_tasks SET credentials_enc=$3 WHERE id=$1 AND lease_token=$2 AND lease_until>now()',id,token,encrypt(json.dumps(value)))==1,'任务租约已过期',409)
        with client:
            auth=RenewalSession(client,c,persist);auth.prepare();active,renew=auth.status()
            if active and renew and not job['cancel_started']:
                # Commit before the upstream write. Retries after an ambiguous response are GET-only.
                changed=db.execute("UPDATE renewal_tasks SET cancel_started=true,status='checking',message='正在确认关闭结果。',updated_at=now() WHERE id=$1 AND lease_token=$2 AND NOT cancel_started AND lease_until>now()",id,token)
                if not changed:return
                try:auth.request('POST','/backend-api/subscriptions/cancel',payload={'account_id':c['account_id']})
                except (httpx.HTTPError,ProviderError):pass
                active,renew=auth.status()
            if not renew:
                state='disabled' if active else 'no_subscription'
                message='已确认自动续费关闭，当前订阅有效期保留。' if active else '未发现需要续费的有效订阅，无需关闭。'
    except ProviderError as e:
        if str(e) in ('invalid_session','account_mismatch'):
            state='review';message='账号登录授权已失效或账号不一致，已暂停核实。请粘贴新的 Session 继续处理。'
    except (ValueError,KeyError,BusinessError):
        state='review';message='账号授权不可用，请粘贴新的 Session 继续核实。'
    except Exception:pass
    finally:
        if lease:db.execute('DELETE FROM proxy_leases WHERE id=$1',lease)
    if state=='checking' and job['checks']>=cfg['renewal_check_limit']:
        state='review';message='暂时无法确认结果，已暂停自动核实。可粘贴新的 Session 继续核实。'
    with db.transaction() as q:
        changed=q.execute("UPDATE renewal_tasks SET status=$3,message=$4,credentials_enc=CASE WHEN $3 IN ('disabled','no_subscription','review') THEN '' ELSE credentials_enc END,next_at=now()+$5*interval '1 second',lease_token=NULL,lease_until=NULL,updated_at=now() WHERE id=$1 AND lease_token=$2 AND lease_until>now()",id,token,state,message,cfg['renewal_check_interval_seconds'])
        if changed:q.execute('INSERT INTO renewal_events(task_id,status,message) VALUES($1,$2,$3)',id,state,message)
