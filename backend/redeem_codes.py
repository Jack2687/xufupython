"""Redemption transactions run in a worker thread with bounded database waits."""
from contextlib import contextmanager

from psycopg2.errors import DeadlockDetected, LockNotAvailable, QueryCanceled

from . import db
from .common import BusinessError, audit, current, decrypt, digest, encrypt, integer, now, require, uid

LOCK_TIMEOUT = '3s'
STATEMENT_TIMEOUT = '10s'
EFFECTIVE_STATUS = "CASE WHEN c.status='used' THEN 'used' WHEN c.status='disabled' THEN 'disabled' WHEN c.expires_at<=now() THEN 'expired' ELSE 'active' END"


@contextmanager
def transaction(readonly=False):
    try:
        with db.transaction() as q:
            if readonly:
                q.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            q.execute("SELECT set_config('lock_timeout',$1,true),set_config('statement_timeout',$2,true)", LOCK_TIMEOUT, STATEMENT_TIMEOUT)
            yield q
    except (LockNotAvailable, QueryCanceled, DeadlockDetected):
        raise BusinessError('兑换码操作繁忙，请稍后重试', 503) from None


def redeem(request, value):
    from .wallet import credit
    user = current(request)
    code = value.get('code', '')
    require(isinstance(code, str) and 1 <= len(code.strip()) <= 100, '兑换码无效')
    code = code.strip().upper()
    # Keep rejected attempts counted even when the redemption transaction rolls back.
    with transaction() as q:
        hits = q.scalar("INSERT INTO rate_buckets(key,hits,expires_at) VALUES($1,1,now()+interval '1 minute') ON CONFLICT(key) DO UPDATE SET hits=CASE WHEN rate_buckets.expires_at<now() THEN 1 ELSE rate_buckets.hits+1 END,expires_at=CASE WHEN rate_buckets.expires_at<now() THEN now()+interval '1 minute' ELSE rate_buckets.expires_at END RETURNING hits", 'redeem:'+user['id'])
    require(hits <= 10, '兑换过于频繁', 429)
    with transaction() as q:
        row = q.one('SELECT id,amount,status,expires_at FROM redeem_codes WHERE code_hash=$1 FOR UPDATE', digest(code))
        require(row is not None and row['status'] == 'active' and row['expires_at'] > now(), '兑换码无效、已过期或已使用')
        credit(q, request, user['id'], 'redeem', 'redeem:'+row['id'], '兑换码入账', row['amount'], require_active=True)
        q.execute("UPDATE redeem_codes SET status='used',used_by=$2,used_at=now() WHERE id=$1", row['id'], user['id'])
    return {'ok': True, 'amount': row['amount']}


def listing(page, page_size, search, status):
    integer(page, 1, 1000000); integer(page_size, 1, 100)
    require(isinstance(search, str) and len(search) <= 200, '搜索内容过长')
    require(status in ('', 'active', 'used', 'disabled', 'expired'), '兑换码状态无效')
    # Escape wildcard characters so searching an identifier means a literal match.
    pattern = '%'+search.strip().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')+'%'
    source = f"FROM redeem_codes c LEFT JOIN users u ON c.used_by=u.id WHERE ($1='' OR {EFFECTIVE_STATUS}=$1) AND (c.prefix ILIKE $2 OR c.batch ILIKE $2 OR COALESCE(u.email,'') ILIKE $2)"
    # Count and page share a snapshot, including during concurrent code issuance.
    with transaction(readonly=True) as q:
        total = q.scalar('SELECT count(*) '+source, status, pattern)
        items = q.all(f'SELECT c.id,c.prefix,c.amount,c.batch,{EFFECTIVE_STATUS} AS status,c.used_at,c.expires_at,c.created_at,u.email AS used_by '+source+' ORDER BY c.created_at DESC,c.id DESC LIMIT $3 OFFSET $4', status, pattern, page_size, (page-1)*page_size)
    return {'items': items, 'total': total, 'page': page, 'page_size': page_size, 'has_more': page*page_size < total}


def create(request, value):
    count = integer(value.get('count'), 1, 200)
    amount = integer(value.get('amount'), 100, 10000000)
    days = integer(value.get('days'), 1, 3660)
    batch = now().astimezone().strftime('%Y%m%d')+'-'+uid()[:6]
    codes = []
    with transaction() as q:
        for _ in range(count):
            code = 'XP-'+uid()[:24].upper(); codes.append(code)
            q.execute("INSERT INTO redeem_codes(id,code_hash,code_enc,prefix,amount,batch,expires_at) VALUES($1,$2,$3,$4,$5,$6,now()+($7*interval '1 day'))", uid(), digest(code), encrypt(code), code[:7]+'…'+code[-4:], amount, batch, days)
        audit(q, request, 'codes.created', batch, f'生成 {count} 个兑换码')
    return {'codes': codes, 'batch': batch}


def batch(request, value):
    ids = value.get('ids', [])
    action = value.get('action')
    require(isinstance(ids, list) and 1 <= len(ids) <= 1000 and all(isinstance(x, str) and 1 <= len(x) <= 80 for x in ids), '请选择兑换码')
    ids = list(dict.fromkeys(ids))
    with transaction() as q:
        if action in ('export', 'copy'):
            extra = " AND c.status='active' AND c.expires_at>statement_timestamp()" if action == 'export' else ''
            rows = q.all('SELECT c.code_enc FROM redeem_codes c WHERE c.id=ANY($1)'+extra+' ORDER BY array_position($1::text[],c.id) FOR SHARE', ids)
            result = {'codes': [decrypt(row['code_enc']) for row in rows]}
            changed = len(rows)
        elif action == 'disable':
            changed = q.execute("UPDATE redeem_codes SET status='disabled' WHERE id=ANY($1) AND status='active'", ids)
            result = {'updated': changed}
        elif action == 'delete':
            changed = q.execute("DELETE FROM redeem_codes WHERE id=ANY($1) AND status<>'used'", ids)
            result = {'deleted': changed}
        else:
            raise BusinessError('不支持的批量操作')
        result.update(changed=changed, skipped=len(ids)-changed)
        audit(q, request, 'codes.'+action, 'batch', f'处理 {changed} 个，跳过 {len(ids)-changed} 个；ID：'+','.join(ids))
    return result
