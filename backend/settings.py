import secrets
from urllib.parse import urlsplit
from fastapi import APIRouter, Depends, Request
from . import db
from .common import *
from .seed import DEFAULTS

router = APIRouter(prefix='/api/admin', dependencies=[Depends(administrator)])
COUNTRIES = {'PH','US','JP','SG','GB','EG','TR'}
STATES = set('AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY'.split())
RANGES = {
 'risk': {'login_per_minute':(1,100),'api_per_minute':(10,10000),'order_per_minute':(1,100),'max_open_orders':(1,500),'max_topup':(100,100000000),'session_hours':(1,168),'event_retention_days':(7,365)},
 'scheduler': {'max_parallel':(1,100),'per_user_parallel':(1,20),'retry_limit':(0,10),'retry_delay_seconds':(5,3600),'task_timeout_seconds':(30,1800),'queue_limit':(1,10000)},
 'payment': {'max_parallel':(1,20),'per_user_parallel':(1,5),'prepare_timeout_seconds':(30,300),'quote_timeout_seconds':(30,600),'result_timeout_seconds':(30,600),'requests_per_minute':(1,20),'subscription_wait_seconds':(30,1800),'renewal_check_interval_seconds':(5,300),'renewal_check_limit':(1,60),'network_restart_limit':(0,5),'activation_check_interval_seconds':(2,300),'activation_check_limit':(1,120)},
 'proxy': {'request_timeout_seconds':(10,120)}, 'site': {'invite_rate':(0,0)}
}
OPTIONAL = {'proxy': {'global_id'}, 'payment': {'checkout_flow','subscription_flow','checkout_type','checkout_types','network_restart_limit'}}


def validate_config(key, v):
    require(key in DEFAULTS, '不支持的配置组')
    allowed = set(DEFAULTS[key]) | OPTIONAL.get(key,set())
    require(not set(v)-allowed, '未知配置字段')
    require(not allowed-set(v)-OPTIONAL.get(key,set()), '缺少配置字段')
    for name, (lo,hi) in RANGES.get(key,{}).items():
        if name in v: integer(v[name],lo,hi,name+' 数值超出范围')
    for name in {'site':['registration_open'],'scheduler':['paused'],'payment':['enabled'],'billing':['allow_customer_edit'],'proxy':['match_country']}.get(key,[]):
        require(type(v.get(name)) is bool, name+' 必须为开关')
    if key in ('scheduler','payment'):
        require(v['per_user_parallel']<=v['max_parallel'],'用户并发不能大于总并发')
    if key=='scheduler':
        require(v['default_country'] in COUNTRIES and v['link_mode']=='long' and v['engine'] in ('not_connected','checkout'),'调度配置无效')
    if key=='payment':
        # Retired flow identifiers must not be accepted from older clients.
        for name in ('subscription_flow','checkout_flow'):
            if name in v: require(isinstance(v[name],str) and length(v[name])<=80,'流程配置无效')
        if 'subscription_flow' in v:
            require(v['subscription_flow'] in ('','gener_direct_pay','cslive_go_direct_pay'),
                    '此订阅流程不受支持，请重新选择流程')
        if 'checkout_type' in v: require(v['checkout_type'] in ('hosted','oaics'),'结账链接类型无效')
        if 'checkout_types' in v:
            require(isinstance(v['checkout_types'],dict) and set(v['checkout_types'])==set(DEFAULTS['payment']['checkout_types']) and all(t in ('hosted','oaics') for t in v['checkout_types'].values()),'请为每个套餐选择链接类型')
    if key=='proxy':
        require(v['match_country'] is True,'代理必须按订阅地区自动匹配')
        for name in ('default_id','global_id'):
            if name in v: require(0<=length(v[name])<=64,'代理标识无效')
    if key=='site':
        for name,lo,hi in [('name',1,50),('english_name',1,50),('tagline',1,200),('support_url',0,500),('terms',10,5000)]:
            require(lo<=length(v[name])<=hi,name+' 内容长度无效')
        require(v['billing_country']=='US' and v['billing_mode']=='customer_review','账单配置无效')
        if v['support_url']:
            u=urlsplit(v['support_url']);require(u.scheme in ('http','https') and u.hostname,'客服链接须为 http(s) 地址')
    if key=='billing':
        require(v['country']=='US','账单国家固定 US')
        arr=v['address_templates'];require(isinstance(arr,list) and len(arr)<=100,'地址模版应为最多 100 项的列表')
        ids=set();emails=set()
        for p in arr:
            require(isinstance(p,dict),'地址模版格式无效')
            for k,x in p.items():
                if k in ('enabled','test_only'): require(type(x) is bool,'账单标记必须为开关')
                else: require(k in ('id','name','email','first_name','last_name','state','city','line1','postal_code') and 0<=length(x)<=200,'地址模版内容无效')
            require(p.get('state') in STATES and re.fullmatch(r'\d{5}(-\d{4})?',p.get('postal_code','')) and all(isinstance(p.get(k),str) and p[k].strip() for k in ('first_name','last_name','city','line1')),'请填写完整、真实的 US 持卡人账单资料')
            email=p.get('email','').strip().lower();id=p.get('id','')
            require(not email or email_valid(email) and email not in emails,'邮箱无效或重复')
            require(not id or re.fullmatch(r'bp_[A-Za-z0-9_-]{1,80}',id) and id not in ids,'账单资料标识无效或重复')
            require(id or email,'账单资料缺少标识')
            ids.add(id);emails.add(email)
    if key=='tiers':
        arr=v['items'];require(isinstance(arr,list) and 1<=len(arr)<=10,'等级数量需为 1～10')
        codes=set();names=set();threshold=-1;discount=100
        for i,p in enumerate(arr):
            require(isinstance(p,dict) and not set(p)-{'code','name','threshold','discount','description'},'等级字段无效')
            require(1<=length(p.get('name'))<=80 and p['name'].strip() and 0<=length(p.get('description',''))<=500,'等级名称无效')
            require(isinstance(p.get('code'),str) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,15}',p['code']) and p['code'] not in codes and p['name'].strip() not in names,'等级标识或名称重复、无效')
            integer(p.get('threshold'),0,10000000000);integer(p.get('discount'),1,100)
            require((i>0 or p['threshold']==0) and p['threshold']>threshold and p['discount']<=discount,'第一档门槛须为 0，门槛递增且折扣不能变差')
            threshold,discount=p['threshold'],p['discount'];codes.add(p['code']);names.add(p['name'].strip())


@router.get('/settings')
def settings():
    return {r['key']:r['value'] for r in db.all('SELECT key,value FROM settings ORDER BY key')}


@router.put('/settings/{key}')
async def save_settings(key:str,request:Request):
    v=await body(request);validate_config(key,v)
    with db.transaction() as q:
        if key=='proxy': q.execute('SELECT pg_advisory_xact_lock(1886003)')
        if key=='tiers': q.execute('SELECT pg_advisory_xact_lock(782518860)')
        previous=q.scalar('SELECT value FROM settings WHERE key=$1 FOR UPDATE',key)
        for name in OPTIONAL.get(key,set()):
            if name not in v and name in previous: v[name]=previous[name]
        if key=='payment' and v.get('subscription_flow')=='gener_direct_pay':
            v['checkout_type']='hosted'
            v['checkout_types']={plan:'hosted' for plan in DEFAULTS['payment']['checkout_types']}
        if key=='proxy':
            for name in ('default_id','global_id'):
                require(not v.get(name) or q.scalar('SELECT enabled FROM resources WHERE id=$1',v[name]),'代理不存在或未启用')
        if key=='tiers':
            require(not q.scalar("SELECT count(*) FROM users WHERE membership_code<>'' AND (membership_until IS NULL OR membership_until>now()) AND NOT(membership_code=ANY($1::text[]))",[p['code'] for p in v['items']]),'有账户正在使用被移除的指定等级')
        q.execute('UPDATE settings SET value=$2 WHERE key=$1',key,v)
        audit(q,request,'settings.updated',key,'保存配置组')
    return {'ok':True}


@router.get('/billing-profiles')
def profiles():
    return {'items':config('billing').get('address_templates',[])}


@router.post('/billing-profiles/sample')
def sample_profile():
    rows=[p for p in config('billing').get('address_templates',[]) if p.get('enabled') is True and p.get('test_only') is True]
    require(rows,'暂无已启用的测试资料')
    return {'profile':secrets.choice(rows),'test_only':True}


@router.post('/billing-profiles')
@router.put('/billing-profiles/{email}')
@router.delete('/billing-profiles/{email}')
async def change_profile(request:Request,email:str=''):
    p={}
    if request.method!='DELETE':
        p=await body(request)
        p={k:x.strip() if isinstance(x,str) else x for k,x in p.items()}
        if isinstance(p.get('email'),str): p['email']=p['email'].lower()
        require(all(type(p.get(k)) is bool for k in ('enabled','test_only')),'请指定启用状态及是否仅供测试')
    with db.transaction() as q:
        cfg=q.scalar("SELECT value FROM settings WHERE key='billing' FOR UPDATE")
        rows=cfg['address_templates']
        if request.method=='POST':
            p['id']='bp_'+uid();rows.append(p)
        else:
            index=next((i for i,r in enumerate(rows) if r.get('id')==email or r.get('email','').lower()==email.lower() and email),None)
            require(index is not None,'账单资料不存在，请刷新列表',404)
            if request.method=='DELETE': rows.pop(index)
            else:
                p['id']=rows[index].get('id') or 'bp_'+uid();rows[index]=p
        validate_config('billing',cfg)
        q.execute("UPDATE settings SET value=$1 WHERE key='billing'",cfg)
        audit(q,request,'billing.'+request.method.lower(),'billing','更新账单资料列表')
    return {'items':rows}
