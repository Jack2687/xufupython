import secrets
from fastapi import APIRouter,Depends,Request
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
 CASE WHEN l.previous_balance IS NOT NULL THEN l.balance_after-l.previous_balance
 WHEN l.kind='freeze' THEN -o.fee WHEN l.kind='unfreeze' THEN o.fee
 WHEN l.kind='charge' AND l.reference LIKE 'payment:%' THEN 0 ELSE l.amount END AS available_delta,
 CASE WHEN l.previous_frozen IS NOT NULL THEN l.frozen_after-l.previous_frozen
 WHEN l.kind='freeze' THEN o.fee WHEN l.kind='unfreeze' THEN -o.fee
 WHEN l.kind='charge' AND l.reference LIKE 'payment:%' THEN l.amount ELSE 0 END AS frozen_delta,
 CASE WHEN o.id IS NULL OR o.deleted_at IS NOT NULL THEN NULL ELSE jsonb_build_object('id',o.id,'product_name',o.product_name,'email',o.email,'country',o.country,'fee',o.fee,'price_snapshot',o.price_snapshot) END AS related_order,
 '{}'::jsonb AS payment FROM recent l LEFT JOIN orders o ON o.id=l.order_id AND o.user_id=$1 ORDER BY l.id DESC"""


def credit(q,request,user_id,kind,reference,description,amount):
    user=q.one('SELECT balance,frozen FROM users WHERE id=$1 FOR UPDATE',user_id)
    require(user is not None,'用户不存在',404)
    integer(amount,-100000000000,100000000000,'金额超出允许范围')
    require(amount!=0,'金额超出允许范围')
    require(user['balance']+amount>=0,'可用余额不足，无法扣减')
    q.execute('INSERT INTO ledger(user_id,kind,amount,balance_after,frozen_after,reference,description,actor_id) VALUES($1,$2,$3,$4,$5,$6,$7,$8)',user_id,kind,amount,user['balance']+amount,user['frozen'],reference,description,current(request)['id'])
    q.execute('UPDATE users SET balance=balance+$2,updated_at=now() WHERE id=$1',user_id,amount)


@router.get('/wallet')
def wallet(request: Request):
    user=current(request)
    ledger=db.all(LEDGER_QUERY,user['id'])
    return {'balance':user['balance'],'frozen':user['frozen'],'ledger':ledger,'topups':db.all('SELECT * FROM topups WHERE user_id=$1 ORDER BY created_at DESC LIMIT 200',user['id'])}


@router.get('/v1/wallet')
def api_wallet(request: Request):
    from .membership import membership_for
    u=current(request)
    return {'balance':u['balance'],'frozen':u['frozen'],'fee_override':u['fee_override'],'membership':membership_for(u['id']),'unit':'cent_R'}


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
    v=await body(request);u=current(request)
    require(limit('redeem:'+u['id'],10),'兑换过于频繁',429)
    code=str(v.get('code','')).strip().upper()
    with db.transaction() as q:
        row=q.one('SELECT id,amount,status,expires_at>now() AS valid FROM redeem_codes WHERE code_hash=$1 FOR UPDATE',digest(code))
        require(row is not None and row['status']=='active' and row['valid'],'兑换码无效、已过期或已使用')
        credit(q,request,u['id'],'redeem','redeem:'+row['id'],'兑换码入账',row['amount'])
        q.execute("UPDATE redeem_codes SET status='used',used_by=$2,used_at=now() WHERE id=$1",row['id'],u['id'])
    return {'ok':True,'amount':row['amount']}


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
    return {'items':db.all("SELECT substring(email from 1 for 2)||'***@'||split_part(email,'@',2) AS email,created_at FROM users WHERE invited_by=$1 ORDER BY created_at DESC",current(request)['id'])}
