"""Read-only customer and administrative records; no order execution routes."""
from fastapi import APIRouter, Depends, Query, Request
from . import db
from .common import *
from .membership import membership_for,fee_for
from .runtime import order_workflows, queue_message, worker_mode
from . import payment_trace

public_router=APIRouter(prefix='/api')
router=APIRouter(prefix='/api',dependencies=[Depends(member)])
admin_router=APIRouter(prefix='/api/admin',dependencies=[Depends(administrator)])
ORDER_FIELDS='o.id,o.product_id,o.product_name,o.email,o.country,o.seats,o.status,o.fee,o.source,o.workflow,o.keep_renewal,o.renewal,o.activation,o.created_at,o.updated_at,o.price_snapshot'
ANN="SELECT id,title,body,tone FROM announcements WHERE published AND (starts_at IS NULL OR starts_at<=now()) AND (ends_at IS NULL OR ends_at>now())"


def _order_history(id, technical=False):
    events=db.all('SELECT id,stage,message,created_at FROM order_events WHERE order_id=$1 ORDER BY id',id)
    labels={'network_retry':'等待网络重试','checkout_reused':'复用原结账链接','payment_review':'付款结果待核实','queued':'排队受理','processing':'开始办理','network_check':'检查代理出口',
            'network_verified':'记录代理出口','checkout':'生成结账链接','checkout_done':'结账链接已生成',
            'awaiting_payment':'等待用户付款','payment':'开始付款','confirming':'提交付款确认',
            'approving':'商户批准','failed':'办理失败','payment_succeeded':'付款成功',
            'activation':'核实订阅生效','activation_confirmed':'订阅生效','activation_pending':'等待订阅生效',
            'post_payment_review':'付款后核实待处理',
            'renewal_pending':'等待续费处理','renewal_disabled':'续费已关闭','renewal_kept':'保留续费'}
    trace=[{'id':'business:'+str(e['id']),'source':'business','phase':'network' if e['stage'].startswith('network_') else 'business',
            'state':'failed' if e['stage']=='failed' else 'review' if e['stage'].endswith('_review') else 'recorded',
            'code':e['stage'],'label':labels.get(e['stage'],e['stage']),
            'description':e['message'],'created_at':e['created_at']} for e in events]
    if technical:
        trace += payment_trace.details(id)
        trace.sort(key=lambda e: e['created_at'])
    return {'events':events,'trace':trace if technical else []}


def _billing_details(snapshot):
    if not snapshot:
        return {'state':'not_selected'}
    result={k:snapshot.get(k,'') for k in ('country','city','line1','postal_code','first_name','last_name','email','email_source','profile_id')}
    result.update(state='selected',region=snapshot.get('state') or snapshot.get('region',''))
    result['state_code']=result['region']
    if not result['country']:
        result.update(country='US',country_source='legacy_us_billing')
    return result


def _saved_checkout_links(id, run, checkout):
    """Project the retained URL for admins; this is not a per-round archive."""
    if not run or not run.get('checkout_url_enc'):
        return []
    generated_at = db.scalar("SELECT created_at FROM order_events WHERE order_id=$1 AND stage='checkout_done' ORDER BY id DESC LIMIT 1", id)
    return [{'source': 'current_snapshot', 'round': None, 'generated_at': generated_at,
             'available': bool(checkout.get('ready') and checkout.get('url')),
             'url': checkout.get('url', '')}]


@public_router.get('/public')
def public():
    cfg=config('payment')
    flow=cfg.get('subscription_flow') or ''
    connected=cfg.get('enabled') is True and flow in ('gener_direct_pay','cslive_go_direct_pay','cslive_full_go')
    return {'site':config('site'),'products':db.all('SELECT * FROM products WHERE enabled ORDER BY sort'),'announcements':db.all(ANN+' AND login_visible ORDER BY sort,created_at DESC'),'checkout_connected':connected,'payment_connected':connected,'subscription_flow':flow if connected else '','default_country':config('scheduler')['default_country'],'tiers':config('tiers')}


@router.get('/overview')
def overview(request:Request):
    user=current(request)
    stats=db.one("SELECT count(*) AS orders,count(*) FILTER(WHERE status='succeeded') AS succeeded,count(*) FILTER(WHERE status IN ('draft','queued','processing','awaiting_payment') AND NOT(workflow='link' AND status='awaiting_payment')) AS pending FROM orders WHERE user_id=$1",user['id'])
    stats.update(db.one("SELECT COALESCE(sum(-amount) FILTER(WHERE kind='charge'),0) AS used,COALESCE(sum(amount) FILTER(WHERE kind IN ('topup','redeem')),0) AS credited FROM ledger WHERE user_id=$1",user['id']))
    return {'user':user,'stats':stats,'recent_orders':db.all('SELECT '+ORDER_FIELDS+' FROM orders o WHERE user_id=$1 ORDER BY created_at DESC LIMIT 5',user['id']),'announcements':db.all(ANN+' ORDER BY sort,created_at DESC')}


@router.get('/orders')
@router.get('/v1/orders')
def orders(request:Request,q:str='',status:str='',workflow:str='',page:int=Query(1,ge=1,le=100000),page_size:int=Query(200,ge=1,le=200)):
    require(status in ('','draft','queued','processing','awaiting_payment','succeeded','failed','cancelled','review'),'订单状态无效')
    require(workflow in ('','link','subscription') and len(q)<=200,'订单筛选参数无效')
    where=" FROM orders o WHERE user_id=$1 AND ($2='' OR email ILIKE '%'||$2||'%' OR id ILIKE '%'||$2||'%') AND ($3='' OR status=$3) AND ($4='' OR workflow=$4)"
    args=(current(request)['id'],q,status,workflow)
    rows=db.all('SELECT '+ORDER_FIELDS+where+' ORDER BY created_at DESC,id DESC LIMIT $5 OFFSET $6',*args,page_size,(page-1)*page_size)
    for r in rows:
        r['payment']=_order_list_payment(r)
    total=db.scalar('SELECT count(*)'+where,*args)
    return {'items':rows,'page':page,'page_size':page_size,'total':total,'has_more':page*page_size<total}


@router.get('/orders/{id}')
@router.get('/v1/orders/{id}')
def order_detail(id:str,request:Request):
    row=db.one('SELECT '+ORDER_FIELDS+' FROM orders o WHERE id=$1 AND user_id=$2',id,current(request)['id'])
    require(row,'订单不存在',404)
    if row['status']=='queued':row['queue_message']=queue_message(row.get('workflow'))
    payment={};checkout={}
    if row.get('workflow') in ('subscription','link'):
        sr=db.one('SELECT payment_last4,payment_amount,payment_currency,paid_at,checkout_session_id,checkout_url_enc FROM subscription_runs WHERE order_id=$1',id)
        if sr:
            if sr.get('checkout_session_id'):
                checkout={'checkout_session_id':sr['checkout_session_id']}
            if sr.get('checkout_url_enc'):
                try:checkout['url']=decrypt(sr['checkout_url_enc']);checkout['ready']=True
                except Exception:pass
            # Build payment object for all subscription states
            if row['workflow']=='subscription':
                fee_state='released'
                if row['status']=='succeeded':fee_state='charged'
                elif row['status'] in ('queued','processing','review'):fee_state='held'
                payment={'state':_payment_state(row),'last4':sr.get('payment_last4') or '','amount':sr.get('payment_amount'),'currency':sr.get('payment_currency') or '','fee_state':fee_state,'charged_amount':sr.get('payment_amount') if sr.get('payment_last4') else None,'charged_currency':sr.get('payment_currency') or '','message':_payment_message(row),'enabled':False}
    return {'order':row,**_order_history(id),'checkout':checkout,'payment':payment}


def _payment_state(row):
    s=row['status']
    if s=='succeeded' and row.get('activation') in ('active','review','pending'):return 'succeeded'
    if s=='processing':return 'preparing'
    if s=='failed':return 'failed'
    if s=='cancelled':return 'cancelled'
    if s=='review':return 'review'
    if s=='queued':return 'preparing'
    return 'preparing'


def _payment_message(row):
    s=row['status']
    if s=='succeeded':return '付款成功，服务费已结算。'
    if s=='processing':return '正在办理中，请稍候。'
    if s=='failed':return '办理未完成，服务费已解冻。'
    if s=='queued':return '已排队，等待处理。'
    if s=='review':return '付款结果尚未确认，已停止重复提交，请核实结果。'
    return ''


def _order_list_payment(row):
    if row.get('workflow') not in ('subscription','link'):
        return {}
    if row['workflow']=='subscription':
        fee_state='released'
        if row['status']=='succeeded':fee_state='charged'
        elif row['status'] in ('queued','processing','review'):fee_state='held'
        sr=db.one('SELECT payment_last4,payment_amount,payment_currency FROM subscription_runs WHERE order_id=$1',row['id'])
        return {'state':_payment_state(row),'last4':(sr or {}).get('payment_last4') or '','amount':(sr or {}).get('payment_amount'),'currency':(sr or {}).get('payment_currency') or '','fee_state':fee_state,'message':_payment_message(row)}
    return {}


@router.get('/v1/catalog')
def catalog(request:Request):
    m=membership_for(current(request)['id']);products=db.all('SELECT id,name,description,fee,features,tag FROM products WHERE enabled ORDER BY sort')
    for p in products: p['member_fee']=fee_for(m,p['fee'])
    countries=[{'code':code,'name':name,'currency':currency} for code,name,currency in [('PH','菲律宾','PHP'),('US','美国','USD'),('JP','日本','JPY'),('SG','新加坡','SGD'),('GB','英国','GBP'),('EG','埃及','EGP'),('TR','土耳其','TRY')]]
    cfg=config('payment')
    return {'items':products,'membership':m,'unit':'cent_R','countries':countries,'payment_connected':cfg.get('enabled') is True and cfg.get('subscription_flow') in ('gener_direct_pay','cslive_go_direct_pay','cslive_full_go')}


@admin_router.get('/overview')
def admin_overview():
    stats=db.one("SELECT (SELECT count(*) FROM users WHERE role='member') AS customers,(SELECT COALESCE(sum(balance),0) FROM users WHERE role='member') AS balance,(SELECT COALESCE(sum(frozen),0) FROM users WHERE role='member') AS frozen,(SELECT count(*) FROM orders) AS orders,(SELECT count(*) FROM topups WHERE status='pending') AS pending_topups,(SELECT count(*) FROM risk_events WHERE NOT resolved) AS risks,(SELECT count(*) FROM resources WHERE enabled) AS resources,(SELECT count(*) FROM orders WHERE status IN ('queued','processing','awaiting_payment') AND NOT(workflow='link' AND status='awaiting_payment')) AS active_orders")
    scheduler=dict(config('scheduler'),worker_mode=worker_mode())
    scheduler['paused']=scheduler['paused'] or worker_mode()=='off'
    cfg=config('payment')
    return {'stats':stats,'orders':db.all('SELECT '+ORDER_FIELDS+',o.user_id,o.note,u.email AS owner FROM orders o JOIN users u ON u.id=o.user_id ORDER BY o.created_at DESC LIMIT 8'),'risks':db.all('SELECT * FROM risk_events ORDER BY id DESC LIMIT 5'),'scheduler':scheduler,'payment_connected':cfg.get('enabled') is True and cfg.get('subscription_flow') in ('gener_direct_pay','cslive_go_direct_pay','cslive_full_go')}


@admin_router.get('/orders')
def admin_orders(q:str='',status:str=''):
    rows=db.all("SELECT "+ORDER_FIELDS+",o.user_id,o.note,u.email AS owner FROM orders o JOIN users u ON u.id=o.user_id WHERE ($1='' OR o.email ILIKE '%'||$1||'%' OR o.id ILIKE '%'||$1||'%' OR u.email ILIKE '%'||$1||'%') AND ($2='' OR o.status=$2) ORDER BY o.created_at DESC LIMIT 500",q,status)
    for r in rows:
        r['payment']=_order_list_payment(r)
    return {'items':rows}


@admin_router.get('/orders/{id}')
def admin_detail(id:str):
    row=db.one('SELECT '+ORDER_FIELDS+',o.user_id,o.note,u.email AS owner FROM orders o JOIN users u ON u.id=o.user_id WHERE o.id=$1',id)
    require(row,'订单不存在',404)
    if row['status']=='queued':row['queue_message']=queue_message(row.get('workflow'))
    payment={};checkout={};billing={'state':'not_selected'};checkout_links=[]
    if row.get('workflow') in ('subscription','link'):
        sr=db.one('SELECT payment_last4,payment_amount,payment_currency,paid_at,checkout_session_id,checkout_url_enc,billing_snapshot FROM subscription_runs WHERE order_id=$1',id)
        if sr:
            if sr.get('checkout_session_id'):
                checkout={'checkout_session_id':sr['checkout_session_id']}
            if sr.get('checkout_url_enc'):
                try:checkout['url']=decrypt(sr['checkout_url_enc']);checkout['ready']=True
                except Exception:pass
            if row['workflow']=='subscription':
                fee_state='released'
                if row['status']=='succeeded':fee_state='charged'
                elif row['status'] in ('queued','processing','review'):fee_state='held'
                payment={'state':_payment_state(row),'last4':sr.get('payment_last4') or '','amount':sr.get('payment_amount'),'currency':sr.get('payment_currency') or '','fee_state':fee_state,'charged_amount':sr.get('payment_amount') if sr.get('payment_last4') else None,'charged_currency':sr.get('payment_currency') or '','message':_payment_message(row),'enabled':False}
            if sr.get('billing_snapshot'):
                billing=_billing_details(sr['billing_snapshot'])
            checkout_links=_saved_checkout_links(id,sr,checkout)
    return {'order':row,**_order_history(id,technical=True),'checkout':checkout,'checkout_links':checkout_links,'payment':payment,'billing':billing,'ledger':db.all('SELECT kind,amount,balance_after,frozen_after,description,created_at FROM ledger WHERE order_id=$1 ORDER BY id',id)}


@admin_router.patch('/orders/{id}')
async def note(id:str,request:Request):
    v=await body(request);require(0<=length(v.get('note'))<=1000,'备注内容过长')
    with db.transaction() as q:
        require(q.execute('UPDATE orders SET note=$2,updated_at=now() WHERE id=$1',id,v['note'])==1,'订单不存在',404)
        audit(q,request,'order.note',id,'更新内部备注')
    return {'ok':True}
