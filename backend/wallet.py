import secrets
from fastapi import APIRouter,Depends,Request
from starlette.concurrency import run_in_threadpool
from . import db
from .common import *
from . import customer_contract

router=APIRouter(prefix='/api',dependencies=[Depends(member)])
LEDGER_QUERY="""WITH history AS (
 SELECT id,kind,amount,balance_after,frozen_after,description,created_at,order_id,reference,
 lag(balance_after) OVER (ORDER BY id) AS previous_balance,lag(frozen_after) OVER (ORDER BY id) AS previous_frozen
 FROM ledger WHERE user_id=$1
), recent AS (SELECT * FROM history ORDER BY id DESC LIMIT 300)
 SELECT l.id,l.kind,l.amount,l.balance_after,l.frozen_after,l.description,l.created_at,
 CASE WHEN l.previous_balance IS NOT NULL THEN (l.balance_after-l.frozen_after)-(l.previous_balance-l.previous_frozen)
 WHEN l.kind='freeze' THEN -o.fee WHEN l.kind='unfreeze' THEN o.fee
 WHEN l.kind='charge' AND l.reference LIKE 'payment:%' THEN 0 ELSE l.amount END AS available_delta,
 CASE WHEN l.previous_frozen IS NOT NULL THEN l.frozen_after-l.previous_frozen
 WHEN l.kind='freeze' THEN o.fee WHEN l.kind='unfreeze' THEN -o.fee
 WHEN l.kind='charge' AND l.reference LIKE 'payment:%' THEN l.amount ELSE 0 END AS frozen_delta,
 CASE WHEN o.id IS NULL OR o.deleted_at IS NOT NULL THEN NULL ELSE jsonb_build_object('id',o.id,'product_name',o.product_name,'email',o.email,'country',o.country,'fee',o.fee,'price_snapshot',o.price_snapshot) END AS related_order,
 CASE WHEN o.id IS NULL OR o.deleted_at IS NOT NULL OR s.order_id IS NULL THEN '{}'::jsonb
 ELSE jsonb_build_object('last4',s.payment_last4,'amount',s.payment_amount,'currency',s.payment_currency,
 'charged_amount',CASE WHEN s.paid_at IS NOT NULL THEN s.payment_amount ELSE NULL END,
 'charged_currency',CASE WHEN s.paid_at IS NOT NULL THEN s.payment_currency ELSE '' END) END AS payment
 FROM recent l LEFT JOIN orders o ON o.id=l.order_id AND o.user_id=$1
 LEFT JOIN subscription_runs s ON s.order_id=o.id ORDER BY l.id DESC"""


def credit(q,request,user_id,kind,reference,description,amount,require_active=False):
    user=q.one('SELECT balance,frozen,status,deleted_at FROM users WHERE id=$1 FOR UPDATE',user_id)
    require(user is not None,'用户不存在',404)
    if require_active:
        require(user['status']=='active' and user['deleted_at'] is None,'账户已停用或删除，无法兑换',403)
    integer(amount,-100000000000,100000000000,'金额超出允许范围')
    require(amount!=0,'金额超出允许范围')
    require(user['balance']+amount>=0,'可用余额不足，无法扣减')
    q.execute('INSERT INTO ledger(user_id,kind,amount,balance_after,frozen_after,reference,description,actor_id) VALUES($1,$2,$3,$4,$5,$6,$7,$8)',user_id,kind,amount,user['balance']+amount,user['frozen'],reference,description,current(request)['id'])
    q.execute('UPDATE users SET balance=balance+$2,updated_at=now() WHERE id=$1',user_id,amount)


@router.get('/wallet')
def wallet(request: Request):
    user=current(request)
    ledger=db.all(LEDGER_QUERY,user['id'])
    project_ledger(ledger,user)
    return {'balance':user['balance'],'frozen':user['frozen'],'ledger':ledger,'topups':db.all('SELECT * FROM topups WHERE user_id=$1 ORDER BY created_at DESC LIMIT 200',user['id']),
            'recharges':db.all(RECHARGE_QUERY,user['id'])}


# A read-only projection of existing credits; redeeming never creates a second
# accounting entry or an artificial manual topup. Keep /v1/topups unchanged.
RECHARGE_QUERY="""WITH credits AS (
 SELECT id,kind,amount,balance_after,frozen_after,reference,description,created_at,
 lag(balance_after-frozen_after) OVER (ORDER BY id) AS previous_available,
 lag(frozen_after) OVER (ORDER BY id) AS previous_frozen FROM ledger WHERE user_id=$1
), history AS (
 SELECT 'redeem:'||l.id AS id,'redeem' AS source,'approved' AS status,l.amount,
 COALESCE(l.balance_after-l.frozen_after-l.previous_available,l.amount) AS available_delta,
 COALESCE(l.frozen_after-l.previous_frozen,0) AS frozen_delta,l.balance_after,l.frozen_after,
 COALESCE(c.prefix,'已兑换（历史记录）') AS related,l.description,l.created_at,NULL::text AS topup_id
 FROM credits l LEFT JOIN redeem_codes c ON l.reference='redeem:'||c.id AND c.used_by=$1 WHERE l.kind='redeem'
 UNION ALL
 SELECT 'topup:'||t.id,'manual',t.status,t.amount,
 CASE WHEN l.id IS NOT NULL THEN COALESCE(l.balance_after-l.frozen_after-l.previous_available,l.amount) END,
 CASE WHEN l.id IS NOT NULL THEN COALESCE(l.frozen_after-l.previous_frozen,0) END,
 l.balance_after,l.frozen_after,t.id,COALESCE(NULLIF(t.note,''),l.description,''),
 COALESCE(l.created_at,t.created_at),t.id
 FROM topups t LEFT JOIN credits l ON l.reference='topup:'||t.id AND l.kind='topup' WHERE t.user_id=$1
) SELECT * FROM history ORDER BY created_at DESC,id DESC LIMIT 300"""


@router.get('/v1/wallet')
def api_wallet(request: Request):
    from .membership import membership_for
    from .features import allowed
    u=current(request)
    return {'balance':u['balance'],'frozen':u['frozen'],'fee_override':u['fee_override'] if allowed('membership',u) else None,'membership':membership_for(u['id']) if allowed('membership',u) else None,'unit':'cent_R'}


def project_ledger(rows,user):
    from .features import allowed
    if not allowed('orders',user):
        for row in rows: row.update(order_id=None,related_order=None,payment={})
    return rows


@router.post('/topups')
async def topup(request: Request):
    v=await body(request);amount=v.get('amount');proof=v.get('proof','')
    integer(amount,100,config('risk').get('max_topup',10000000),'充值金额或备注超出范围')
    require(0<=length(proof)<=500,'充值金额或备注超出范围')
    user_id=current(request)['id']
    with db.transaction() as q:
        api_request=hasattr(request.state,'customer_hash')
        if api_request:
            old=customer_contract.replay(q,request,'topup',lock=True)
            if old:return old
        q.one('SELECT id FROM users WHERE id=$1 FOR UPDATE',user_id)
        require(q.scalar("SELECT count(*) FROM topups WHERE user_id=$1 AND status='pending'",user_id)<3,'已有 3 笔待审核充值，请先等待审核')
        id=uid();q.execute('INSERT INTO topups(id,user_id,amount,proof) VALUES($1,$2,$3,$4)',id,user_id,amount,proof)
        if api_request:customer_contract.remember(q,request,'topup',{'id':id})
    return {'id':id}


@router.post('/topups/{id}/cancel')
def cancel_topup(id: str,request: Request):
    n=db.execute("UPDATE topups SET status='cancelled',updated_at=now() WHERE id=$1 AND user_id=$2 AND status='pending'",id,current(request)['id'])
    require(n==1,'充值已处理或不存在',409)
    return {'ok':True}


@router.post('/wallet/redeem')
@router.post('/v1/wallet/redeem')
async def redeem(request: Request):
    from .redeem_codes import redeem as redeem_transaction
    return await run_in_threadpool(redeem_transaction, request, await body(request))


@router.get('/keys')
def keys(request: Request):
    return {'items':db.all('SELECT id,name,prefix,revoked,last_used,created_at FROM api_keys WHERE user_id=$1 ORDER BY created_at DESC',current(request)['id'])}


@router.post('/keys')
async def create_key(request: Request):
    v=await body(request);name=v.get('name','');u=current(request)
    require(1<=length(name)<=80,'请填写密钥名称')
    with db.transaction() as q:
        q.one('SELECT id FROM users WHERE id=$1 FOR UPDATE',u['id'])
        require(q.scalar('SELECT count(*) FROM api_keys WHERE user_id=$1 AND NOT revoked',u['id'])<10,'最多保留 10 个有效密钥')
        key='xp_live_'+secrets.token_urlsafe(32);id=uid()
        q.execute('INSERT INTO api_keys(id,user_id,name,hash,prefix) VALUES($1,$2,$3,$4,$5)',id,u['id'],name,digest(key),key[:16]+'…'+key[-4:])
    return {'id':id,'key':key}


@router.delete('/keys/{id}')
def revoke_key(id: str,request: Request):
    require(db.execute('UPDATE api_keys SET revoked=true WHERE id=$1 AND user_id=$2',id,current(request)['id'])==1,'密钥不存在',404)
    return {'ok':True}


@router.get('/invites')
def invites(request: Request):
    from .invitations import listing
    return listing(request, current(request)['id'])
