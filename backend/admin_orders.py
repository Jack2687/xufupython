"""Administrative closure and shared order visibility, retaining accounting evidence."""
from fastapi import APIRouter, Depends, Request
from . import db, payment_trace
from .common import administrator, audit, body, current, length, require

router = APIRouter(prefix='/api/admin', dependencies=[Depends(administrator)])


def lock_queues(q):
    for key in (1886005, 1886006, 1886008):
        q.execute('SELECT pg_advisory_xact_lock($1)', key)


def held_fee(q, order):
    rows = q.all('SELECT kind FROM ledger WHERE order_id=$1 AND user_id=$2', order['id'], order['user_id'])
    kinds = {row['kind'] for row in rows}
    return order['fee'] if 'freeze' in kinds and not kinds.intersection({'charge','unfreeze'}) else 0


@router.post('/orders/{id}/close')
async def close_order(id: str, request: Request):
    value = await body(request)
    reason = value.get('reason', '')
    require(isinstance(reason, str) and 2 <= len(reason.strip()) <= 300 and length(reason) <= 1200,
            '请填写 2～300 字的关闭原因')
    require(value.get('accepted') is True, '请确认关闭订单并释放冻结服务费')
    with db.transaction() as q:
        lock_queues(q)
        order = q.one('SELECT id,user_id,status,workflow,fee,deleted_at FROM orders WHERE id=$1 FOR UPDATE', id)
        require(order and order['deleted_at'] is None, '订单不存在', 404)
        previous = q.one('SELECT order_id FROM order_closures WHERE order_id=$1', id)
        if previous:
            return {'ok': True, 'replayed': True, 'status': 'cancelled'}
        require(order['status'] != 'processing', '订单正在执行，请等待本次处理结束后再关闭', 409)
        require(order['status'] in ('draft','queued','awaiting_payment','review'), '当前订单状态不能人工关闭', 409)
        run = q.one('SELECT paid_at,payment_submit_started_at FROM subscription_runs WHERE order_id=$1 FOR UPDATE', id) or {}
        require(not run.get('paid_at') and not q.scalar("SELECT count(*) FROM ledger WHERE order_id=$1 AND kind='charge'", id),
                '订单已付款或服务费已结算，不能按未完成订单关闭', 409)
        release = held_fee(q, order)
        if release:
            require(q.execute('UPDATE users SET frozen=frozen-$2,updated_at=now() WHERE id=$1 AND frozen>=$2',
                              order['user_id'], release) == 1, '账户冻结金额异常，请先核对账本', 409)
            q.execute("INSERT INTO ledger(user_id,kind,amount,balance_after,frozen_after,reference,description,actor_id,order_id) "
                      "SELECT id,'unfreeze',0,balance,frozen,$2,'人工关闭订单，释放冻结服务费',$3,$4 FROM users WHERE id=$1",
                      order['user_id'], 'unfreeze:'+id, current(request)['id'], id)
            payment_trace.record('funds','completed','release',order_id=id,q=q)
        q.execute('INSERT INTO order_closures(order_id,actor_id,reason,prior_status,prior_payment_state,payment_submitted) '
                  'VALUES($1,$2,$3,$4,$5,$6)', id,current(request)['id'],reason.strip(),order['status'],
                  'review' if order['status']=='review' else 'preparing', bool(run.get('payment_submit_started_at')))
        q.execute("UPDATE orders SET status='cancelled',renewal='not_started',updated_at=now() WHERE id=$1", id)
        q.execute("UPDATE subscription_runs SET credentials_enc='',card_enc='',checkout_checkpoint_enc='',updated_at=now() WHERE order_id=$1", id)
        q.execute("INSERT INTO order_events(order_id,stage,message) VALUES($1,'admin_closed',$2)",id,'管理员已关闭订单并释放冻结服务费。')
        audit(q,request,'order.closed',id,reason.strip())
    return {'ok': True, 'status': 'cancelled', 'released': release}


@router.post('/orders/batch-delete')
async def delete_orders(request: Request):
    value = await body(request); ids = value.get('ids')
    require(isinstance(ids,list) and 1 <= len(ids) <= 500 and all(isinstance(x,str) and 1 <= len(x) <= 80 for x in ids),
            '请选择 1～500 笔订单')
    require(value.get('accepted') is True, '请确认同时删除管理员和客户的订单记录')
    ids = sorted(set(ids))
    with db.transaction() as q:
        lock_queues(q)
        rows = q.all('SELECT id,user_id,status,workflow,fee,activation,renewal,deleted_at FROM orders WHERE id=ANY($1) ORDER BY id FOR UPDATE',ids)
        require(len(rows)==len(ids),'部分订单不存在，请刷新后重试',404)
        pending = [row for row in rows if row['deleted_at'] is None]
        for row in pending:
            for key in (1886011, 1886012):
                require(q.scalar('SELECT pg_try_advisory_xact_lock($1,hashtext($2))', key, row['id']),
                        '订单仍在处理后续结果，请稍后再删除', 409)
            settled = row['status'] in ('failed','cancelled','draft') or (
                row['status']=='succeeded' and row['activation']!='pending' and row['renewal'] not in ('pending','retrying'))
            require(settled and not held_fee(q,row), '包含执行中、待核实或未结束的订单，请先完成或人工关闭后再删除',409)
        count=q.execute('UPDATE orders SET deleted_at=now() WHERE id=ANY($1) AND deleted_at IS NULL', ids)
        if count:audit(q,request,'orders.deleted','batch', '从管理员及客户视图删除订单：'+','.join(row['id'] for row in pending))
    return {'ok': True, 'deleted': count}
