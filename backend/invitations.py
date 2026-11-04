"""Direct invitation rewards, independent of payment execution.

Admission snapshots apply only to newly created orders. Settlement is retriable,
serialized and atomic with wallet entries; refunds never consume frozen funds.
"""
from fastapi import APIRouter, Depends, Request
from . import db
from .common import administrator, config, current, integer, require

router = APIRouter(prefix='/api/admin', dependencies=[Depends(administrator)])
LOCK = 1886020
NET_FEE = """SELECT GREATEST(0,COALESCE(-sum(amount),0))::bigint FROM ledger
 WHERE order_id=$1 AND user_id=$2 AND ((kind='charge' AND amount<0) OR (kind='refund' AND amount>0))"""


def _entry(q, user_id, amount, reference, description):
    user = q.one('SELECT balance,frozen FROM users WHERE id=$1 FOR UPDATE', user_id)
    require(user['balance'] + amount >= user['frozen'], '邀请奖励退回不能扣减冻结积分')
    q.execute('UPDATE users SET balance=balance+$2,updated_at=now() WHERE id=$1', user_id, amount)
    q.execute('INSERT INTO ledger(user_id,kind,amount,balance_after,frozen_after,reference,description) VALUES($1,$2,$3,$4,$5,$6,$7)',
              user_id, 'invite_reward' if amount > 0 else 'invite_reversal', amount,
              user['balance'] + amount, user['frozen'], reference, description)


def settle():
    """Run once per maintenance cycle; never calls an upstream payment API."""
    with db.transaction() as q:
        q.execute("SET LOCAL lock_timeout='2s'; SET LOCAL statement_timeout='5s'")
        if not q.scalar('SELECT pg_try_advisory_xact_lock($1)', LOCK):
            return
        # A refund adjusts entitlement even if rewards are disabled now. Recover
        # available points only and retain the remainder for a later cycle.
        changes = q.all("""SELECT r.*,n.fee FROM invitation_rewards r CROSS JOIN LATERAL
          (SELECT GREATEST(0,COALESCE(-sum(amount),0))::bigint AS fee FROM ledger
           WHERE order_id=r.order_id AND user_id=r.invitee_id
           AND ((kind='charge' AND amount<0) OR (kind='refund' AND amount>0))) n
          WHERE LEAST(r.granted,n.fee*r.rate_percent/100)<r.entitled OR r.granted-r.recovered>r.entitled
          ORDER BY r.created_at LIMIT 200 FOR UPDATE OF r""")
        for row in changes:
            target = min(row['entitled'], row['fee'] * row['rate_percent'] // 100)
            user = q.one('SELECT balance,frozen FROM users WHERE id=$1 FOR UPDATE', row['inviter_id'])
            reclaim = min(row['granted']-row['recovered']-target, max(0,user['balance']-user['frozen']))
            recovered = row['recovered'] + reclaim
            if reclaim:
                _entry(q,row['inviter_id'],-reclaim,f"invite-return:{row['invitee_id']}:{recovered}",'原订阅服务费退回，撤回对应邀请奖励')
            q.execute('UPDATE invitation_rewards SET entitled=$2,recovered=$3,updated_at=now() WHERE invitee_id=$1',row['invitee_id'],target,recovered)
        rows = q.all("""SELECT c.*, first_charge.id AS charge_id FROM invitation_candidates c
          JOIN orders o ON o.id=c.order_id
          JOIN LATERAL (SELECT min(id) AS id FROM ledger WHERE order_id=o.id AND user_id=o.user_id AND kind='charge') first_charge ON first_charge.id IS NOT NULL
          WHERE o.status='succeeded' AND o.activation='active'
          AND NOT EXISTS(SELECT 1 FROM invitation_rewards r WHERE r.invitee_id=c.invitee_id)
          AND NOT EXISTS(SELECT 1 FROM orders old JOIN ledger l ON l.order_id=old.id AND l.user_id=old.user_id AND l.kind='charge'
            WHERE old.user_id=c.invitee_id AND old.workflow='subscription' AND old.status='succeeded' AND old.activation='active' AND l.id<first_charge.id)
          ORDER BY first_charge.id LIMIT 200""")
        for row in rows:
            fee = q.scalar(NET_FEE, row['order_id'], row['invitee_id'])
            amount = fee * row['rate_percent'] // 100
            # Unique invitee constraint is the lifetime once-only boundary.
            inserted = q.execute('INSERT INTO invitation_rewards(invitee_id,inviter_id,order_id,rate_percent,fee,granted,entitled) VALUES($1,$2,$3,$4,$5,$6,$6) ON CONFLICT DO NOTHING',
                                 row['invitee_id'],row['inviter_id'],row['order_id'],row['rate_percent'],fee,amount)
            if inserted and amount:
                _entry(q,row['inviter_id'],amount,'invite:'+row['invitee_id'],'伙伴首笔成功订阅邀请奖励')


def listing(request, owner=None):
    params=request.query_params
    page=integer(int(params.get('page','1')) if params.get('page','1').isdigit() else 0,1,100000)
    search=params.get('q','').strip();require(len(search)<=100,'搜索内容过长')
    where="u.invited_by IS NOT NULL AND ($1::text IS NULL OR u.invited_by=$1) AND ($2='' OR u.email ILIKE '%'||$2||'%' OR i.email ILIKE '%'||$2||'%')"
    # Customer searches must not expose or probe another customer's full email.
    if owner:
        search=''
    args=(owner,search)
    totals=db.one("""SELECT count(*) AS invited,count(r.invitee_id) AS qualified,
      COALESCE(sum(r.entitled),0)::bigint AS earned,
      COALESCE(sum(r.recovered),0)::bigint AS recovered,
      COALESCE(sum(r.granted-r.recovered-r.entitled),0)::bigint AS pending_recovery
      FROM users u JOIN users i ON i.id=u.invited_by LEFT JOIN invitation_rewards r ON r.invitee_id=u.id WHERE """+where,*args)
    email="substring(u.email from 1 for 2)||'***@'||split_part(u.email,'@',2)" if owner else 'u.email'
    inviter="NULL::text" if owner else 'i.email'
    rows=db.all(f"""SELECT u.id,{email} AS email,{inviter} AS inviter,u.created_at,
      r.rate_percent,r.fee,r.granted,r.entitled,r.recovered,r.created_at AS rewarded_at,
      CASE WHEN r.invitee_id IS NULL THEN 'waiting' WHEN r.granted-r.recovered>r.entitled THEN 'recovering'
       WHEN r.entitled=0 AND r.granted>0 THEN 'reversed' WHEN r.recovered>0 THEN 'adjusted' ELSE 'rewarded' END AS reward_state
      FROM users u JOIN users i ON i.id=u.invited_by LEFT JOIN invitation_rewards r ON r.invitee_id=u.id
      WHERE {where} ORDER BY u.created_at DESC,u.id LIMIT 25 OFFSET $3""",*args,(page-1)*25)
    return {'items':rows,'stats':totals,'total':totals['invited'],'page':page,'has_more':page*25<totals['invited'],'rules':config('invites')}


@router.get('/invites')
def admin_invites(request: Request):
    return listing(request)
