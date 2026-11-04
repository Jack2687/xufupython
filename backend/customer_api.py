"""Stable, account-scoped API for customer integrations."""
from fastapi import APIRouter, Depends, Query, Request

from . import db, membership, records, renewals, wallet
from .common import body, config, current, member, require
from .customer_contract import OrderRequest, RenewalRequest, TopupRequest, prepare, replay

router = APIRouter(prefix='/api/v1', dependencies=[Depends(member)])


@router.post('/orders')
async def create_order(request: Request):
    prepare(request, OrderRequest, await body(request))
    old = db.one('SELECT id,status,request_hash FROM orders WHERE user_id=$1 AND idempotency_key=$2',
                 current(request)['id'], request.headers['Idempotency-Key'])
    if old:
        require(old['request_hash'] == request.state.customer_hash,
                '同一 Idempotency-Key 不能用于不同请求', 409)
        return {'id': old['id'], 'status': old['status'], 'replayed': True}
    from . import subscription, subscription_cslive_go, subscription_cslive_full
    flows = {module.FLOW: module for module in (subscription, subscription_cslive_go, subscription_cslive_full)}
    selected = flows.get(config('payment').get('subscription_flow'))
    require(selected is not None, '当前订阅办理不可用，请联系服务支持', 503)
    return await selected.create_order(request)


@router.post('/renewals')
async def create_renewal(request: Request):
    prepare(request, RenewalRequest, await body(request))
    old = replay(db, request, 'renewal')
    if old:
        old['status'] = db.scalar('SELECT status FROM renewal_tasks WHERE id=$1 AND user_id=$2',
                                  old['id'], current(request)['id'])
        return old
    require(config('payment').get('enabled') is True, '续费办理暂未开放', 403)
    return await renewals.create_task(request)


def page_result(items, total, page, page_size):
    return {'items': items, 'page': page, 'page_size': page_size,
            'total': total, 'has_more': page * page_size < total}


@router.get('/renewals')
def renewal_list(request: Request, status: str = '', page: int = Query(1, ge=1, le=100000),
                 page_size: int = Query(50, ge=1, le=200)):
    require(status in ('', 'queued', 'processing', 'checking', 'disabled', 'no_subscription', 'review'),
            '续费任务状态无效')
    user_id = current(request)['id']
    where = " FROM renewal_tasks WHERE user_id=$1 AND ($2='' OR status=$2)"
    items = db.all('SELECT ' + renewals.FIELDS + where +
                   ' ORDER BY created_at DESC,id DESC LIMIT $3 OFFSET $4',
                   user_id, status, page_size, (page - 1) * page_size)
    return page_result(items, db.scalar('SELECT count(*)' + where, user_id, status), page, page_size)


@router.get('/renewals/{id}')
def renewal_detail(id: str, request: Request):
    return renewals.task(id, request)


@router.get('/me')
def me(request: Request):
    return membership.me(request)


@router.get('/overview')
def overview(request: Request):
    return records.overview(request)


@router.get('/wallet/ledger')
def ledger(request: Request, page: int = Query(1, ge=1, le=100000),
           page_size: int = Query(50, ge=1, le=200)):
    query = wallet.LEDGER_QUERY.replace('LIMIT 300', 'LIMIT $2 OFFSET $3')
    items = db.all(query, current(request)['id'], page_size, (page - 1) * page_size)
    total = db.scalar('SELECT count(*) FROM ledger WHERE user_id=$1', current(request)['id'])
    return page_result(items, total, page, page_size)


@router.get('/topups')
def topups(request: Request, status: str = '', page: int = Query(1, ge=1, le=100000),
           page_size: int = Query(50, ge=1, le=200)):
    require(status in ('', 'pending', 'approved', 'rejected', 'cancelled'), '充值状态无效')
    where = " FROM topups WHERE user_id=$1 AND ($2='' OR status=$2)"
    items = db.all('SELECT id,amount,method,proof,status,note,created_at,updated_at' + where +
                   ' ORDER BY created_at DESC,id DESC LIMIT $3 OFFSET $4',
                   current(request)['id'], status, page_size, (page - 1) * page_size)
    total = db.scalar('SELECT count(*)' + where, current(request)['id'], status)
    return page_result(items, total, page, page_size)


@router.post('/topups')
async def create_topup(request: Request):
    prepare(request, TopupRequest, await body(request))
    old = replay(db, request, 'topup')
    return old if old else await wallet.topup(request)


@router.post('/topups/{id}/cancel')
def cancel_topup(id: str, request: Request):
    with db.transaction() as q:
        row = q.one('SELECT status FROM topups WHERE id=$1 AND user_id=$2 FOR UPDATE',
                    id, current(request)['id'])
        require(row, '充值申请不存在', 404)
        require(row['status'] in ('pending', 'cancelled'), '充值申请已审核，不能取消', 409)
        if row['status'] == 'pending':
            q.execute("UPDATE topups SET status='cancelled',updated_at=now() WHERE id=$1", id)
    return {'ok': True}
