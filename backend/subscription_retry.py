"""Durable retries before payment submission; never replay an uncertain charge."""
import json
import re

from . import db, payment_trace as trace
from .common import config, decrypt, encrypt
from .checkout_errors import CheckoutError

NETWORK_ERRORS = {'timeout', 'proxy_connect', 'dns', 'connection', 'network_error'}


def can_start(order_id):
    row = db.one('SELECT o.status,o.deleted_at,s.paid_at,s.payment_submit_started_at FROM orders o '
                 'JOIN subscription_runs s ON s.order_id=o.id WHERE o.id=$1', order_id)
    if not row or row['deleted_at'] is not None or row['status'] != 'processing' or row['paid_at']:
        return False
    if row['payment_submit_started_at']:
        handle_failure(order_id, RuntimeError('payment_result_unconfirmed'))
        return False
    return True


def load_checkout(job, flow):
    if not job.get('network_retry_count') or not job.get('checkout_checkpoint_enc'):
        return None
    checkpoint = json.loads(decrypt(job['checkout_checkpoint_enc']))
    result = checkpoint.get('result') or {}
    if (checkpoint.get('flow') != flow or not result.get('ok')
            or not result.get('hostedUrl') or not result.get('checkoutSessionId')):
        raise RuntimeError('checkout_checkpoint_mismatch')
    return checkpoint


def save_checkout(order_id, flow, result, context=None):
    checkpoint = {'flow': flow, 'result': result, 'request_context': context}
    db.execute('UPDATE subscription_runs SET checkout_url_enc=$2,checkout_session_id=$3,'
               'checkout_checkpoint_enc=$4 WHERE order_id=$1',
               order_id, encrypt(result['hostedUrl']), result['checkoutSessionId'],
               encrypt(json.dumps(checkpoint)))


def mark_payment_submission(order_id):
    """Persist the fence before the first potentially chargeable request."""
    with db.transaction() as q:
        order = q.one('SELECT status,deleted_at FROM orders WHERE id=$1 FOR UPDATE', order_id)
        if not order or order['deleted_at'] is not None or order['status'] != 'processing':
            raise RuntimeError('payment_result_unconfirmed')
        changed = q.execute('UPDATE subscription_runs SET payment_submit_started_at=now() '
                            'WHERE order_id=$1 AND payment_submit_started_at IS NULL AND paid_at IS NULL', order_id)
        if not changed:
            raise RuntimeError('payment_result_unconfirmed')


def handle_failure(order_id, exc):
    """True means retained/requeued: the caller must not release funds or fail it."""
    code = trace.error_code(exc)
    if re.fullmatch(r'(?:提链失败:\s*)?network_error', str(exc).strip()):
        code = 'network_error'
    with db.transaction() as q:
        row = q.one('SELECT o.status,o.deleted_at,s.paid_at,s.payment_submit_started_at,s.network_retry_count,'
                    's.checkout_checkpoint_enc FROM orders o JOIN subscription_runs s ON s.order_id=o.id '
                    'WHERE o.id=$1 FOR UPDATE OF o,s', order_id)
        if not row:
            return False
        if row['deleted_at'] is not None or row['paid_at'] or row['status'] != 'processing':
            return True
        if row['payment_submit_started_at'] and code not in {'payment_declined', 'approval_blocked'}:
            q.execute("UPDATE orders SET status='review',updated_at=now() WHERE id=$1", order_id)
            q.execute("INSERT INTO order_events(order_id,stage,message) VALUES($1,'payment_review',"
                      "'付款请求已提交，结果尚未确认；已保留原交易，请勿再次付款。')", order_id)
            trace.record('result', 'review', 'payment_result_unconfirmed', order_id=order_id, q=q)
            return True
        if code == 'pool_capacity' and not row['payment_submit_started_at']:
            delay = config('scheduler', q)['retry_delay_seconds']
            q.execute("UPDATE subscription_runs SET network_retry_at=now()+$2*interval '1 second',updated_at=now() WHERE order_id=$1", order_id, delay)
            q.execute("UPDATE orders SET status='queued',updated_at=now() WHERE id=$1", order_id)
            q.execute("INSERT INTO order_events(order_id,stage,message) VALUES($1,'queued','国家代理池会话已占满，等待资源后继续。')", order_id)
            return True
        cfg = config('payment', q)
        limit = cfg.get('network_restart_limit', 2)
        initialization_retry = (isinstance(exc, CheckoutError) and exc.code == 'device_id_missing'
                                and not row['checkout_checkpoint_enc'])
        if (code not in NETWORK_ERRORS and not initialization_retry) or row['network_retry_count'] >= limit:
            return False
        # The interval is the existing admin scheduler setting, not a blocking sleep.
        delay = config('scheduler', q)['retry_delay_seconds']
        count = row['network_retry_count'] + 1
        q.execute('UPDATE subscription_runs SET network_retry_count=$2,'
                  "network_retry_at=now()+$3*interval '1 second',updated_at=now() WHERE order_id=$1",
                  order_id, count, delay)
        q.execute("UPDATE orders SET status='queued',updated_at=now() WHERE id=$1", order_id)
        resume = '复用已生成的结账链接，从付款阶段重试' if row['checkout_checkpoint_enc'] else '重新生成结账链接'
        message = f'网络请求失败，{delay} 秒后{resume}（第 {count}/{limit} 次重试）。'
        if initialization_retry:
            message = f'未获取到设备标识，{delay} 秒后使用本单原代理会话重试初始化（第 {count}/{limit} 次重试）。'
        q.execute("INSERT INTO order_events(order_id,stage,message) VALUES($1,$2,$3)", order_id,
                  'checkout_retry' if initialization_retry else 'network_retry', message)
        trace.record('queue', 'waiting', 'checkout_retry_scheduled' if initialization_retry else 'network_retry_scheduled', order_id=order_id,
                     diagnostics={'network_reason': '' if initialization_retry else code if code in NETWORK_ERRORS - {'network_error'} else 'connection',
                                  'retry_number': count, 'retry_limit': limit, 'retry_delay_seconds': delay,
                                  'checkout_reused': bool(row['checkout_checkpoint_enc'])}, q=q)
        return True
