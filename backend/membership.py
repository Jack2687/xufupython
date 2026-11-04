from fastapi import APIRouter, Depends, Request
from . import db
from .common import *

router=APIRouter(prefix='/api',dependencies=[Depends(member)])
SPEND="""SELECT l.user_id,l.order_id,GREATEST(0,-sum(l.amount))::bigint AS spent FROM ledger l JOIN orders o ON o.id=l.order_id AND o.user_id=l.user_id WHERE o.status='succeeded' AND ((l.kind='charge' AND l.amount<0) OR (l.kind='refund' AND l.amount>0)) GROUP BY l.user_id,l.order_id"""


def membership_from(user,spent,q=None):
    tiers=sorted(config('tiers',q)['items'],key=lambda x:x['threshold'])
    tiers=[dict(t,code=t.get('code') or f'L{i+1}') for i,t in enumerate(tiers)]
    automatic=tiers[0]
    for t in tiers:
        if spent>=t['threshold']: automatic=t
    selected=automatic; source='automatic'; until=None
    if user.get('membership_code') and (not user.get('membership_until') or user['membership_until']>now()):
        selected=next((t for t in tiers if t['code']==user['membership_code']),automatic)
        if selected['code']==user['membership_code']:
            source='manual';until=user.get('membership_until')
    following=next((t for t in tiers if t['threshold']>automatic['threshold']),None)
    return {'current':selected,'automatic':automatic,'next':following,'spent':spent,'remaining':following['threshold']-spent if following else 0,'progress':(spent-automatic['threshold'])*100//(following['threshold']-automatic['threshold']) if following else 100,'source':source,'until':until,'fee_override':user.get('fee_override'),'tiers':tiers}


def membership_for(user_id,q=None):
    q=q or db
    user=q.one('SELECT membership_code,membership_until,fee_override FROM users WHERE id=$1',user_id)
    require(user is not None,'用户不存在',404)
    spent=q.scalar('SELECT COALESCE(sum(s.spent),0) FROM ('+SPEND+') s WHERE s.user_id=$1',user_id)
    return membership_from(user,int(spent),q)


def fee_for(m,standard):
    return m['fee_override'] if m['fee_override'] is not None else (standard*m['current']['discount']+50)//100


@router.get('/me')
def me(request: Request):
    return {**current(request),'membership':membership_for(current(request)['id'])}


@router.get('/membership')
@router.get('/v1/membership')
def membership(request: Request):
    m=membership_for(current(request)['id'])
    products=db.all('SELECT id,name,description,fee,enabled FROM products ORDER BY sort')
    for p in products:
        p['member_fee']=fee_for(m,p['fee']);p['saved']=p['fee']-p['member_fee']
    growth=db.all('SELECT o.id,o.product_name,o.updated_at,s.spent FROM ('+SPEND+') s JOIN orders o ON o.id=s.order_id WHERE s.user_id=$1 ORDER BY o.updated_at DESC LIMIT 100',current(request)['id'])
    return {'membership':m,'products':products,'growth':growth,'unit':'cent_R','basis':'settled_service_spend'}


@router.get('/quote')
@router.get('/v1/quote')
def quote(request: Request,product_id:str=''):
    with db.transaction() as q:
        product=q.one('SELECT fee FROM products WHERE id=$1 AND enabled',product_id)
        require(product is not None,'套餐不存在或已下架',404)
        m=membership_for(current(request)['id'],q)
        payment=config('payment',q)
        connected=payment.get('enabled') is True and payment.get('subscription_flow') in ('gener_direct_pay','cslive_go_direct_pay','cslive_full_go')
        return {'fee':fee_for(m,product['fee']),'standard':product['fee'],'membership':m,'payment_connected':connected}
