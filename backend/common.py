import base64
import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timezone

import bcrypt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from . import db


class BusinessError(Exception):
    def __init__(self, message, status=400):
        self.message, self.status = message, status


def require(condition, message, status=400):
    if not condition:
        raise BusinessError(message, status)


def uid():
    return secrets.token_hex(16)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def now():
    return datetime.now(timezone.utc)


def parsedate(value):
    if value in (None, ''):
        return None
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        require(result.tzinfo is not None, '时间需要包含时区')
        return result
    except (ValueError, TypeError, AttributeError):
        raise BusinessError('时间格式不正确')


def encrypt(value):
    nonce = secrets.token_bytes(12)
    cipher = AESGCM(base64.b64decode(os.environ['MASTER_KEY']))
    return base64.b64encode(nonce + cipher.encrypt(nonce, value.encode(), None)).decode()


def decrypt(value):
    data = base64.b64decode(value)
    return AESGCM(base64.b64decode(os.environ['MASTER_KEY'])).decrypt(data[:12], data[12:], None).decode()


def password_hash(value):
    return bcrypt.hashpw(value.encode(), bcrypt.gensalt(rounds=12)).decode()


def password_matches(value, hashed):
    try:
        return bcrypt.checkpw(value.encode(), hashed.encode())
    except (ValueError, TypeError, AttributeError):
        return False


def length(value):
    return len(value.encode('utf-8')) if isinstance(value, str) else -1


def email_valid(value):
    return isinstance(value, str) and len(value) <= 254 and bool(re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+', value))


def integer(value, low, high, message='数字超出范围'):
    require(type(value) is int and low <= value <= high, message)
    return value


def config(key, q=None):
    return (q or db).scalar('SELECT value FROM settings WHERE key=$1', key, default={})


def ip(request):
    return request.client.host if request.client else '127.0.0.1'


def current(request):
    return request.state.user


def audit(q, request, action, target, detail):
    q.execute('INSERT INTO audit_events(actor_id,action,target,detail,ip) VALUES($1,$2,$3,$4,$5)', current(request)['id'], action, target, detail, ip(request))


def event(request, category, severity, detail):
    owner = getattr(request.state, 'user', {}).get('id')
    with db.transaction() as q:
        q.execute('SELECT pg_advisory_xact_lock(1886020,hashtext($1))',category+ip(request)+(owner or ''))
        row=q.one("SELECT id FROM risk_events WHERE category=$1 AND ip=$2 AND user_id IS NOT DISTINCT FROM $3 AND NOT resolved AND created_at>now()-interval '1 minute' ORDER BY id DESC LIMIT 1 FOR UPDATE",category,ip(request),owner)
        if row: q.execute('UPDATE risk_events SET occurrences=occurrences+1,last_seen=now() WHERE id=$1',row['id'])
        else: q.execute('INSERT INTO risk_events(user_id,category,severity,ip,detail,last_seen) VALUES($1,$2,$3,$4,$5,now())',owner,category,severity,ip(request),detail)


def limit(key, count):
    return db.scalar("INSERT INTO rate_buckets(key,hits,expires_at) VALUES($1,1,now()+interval '1 minute') ON CONFLICT(key) DO UPDATE SET hits=CASE WHEN rate_buckets.expires_at<now() THEN 1 ELSE rate_buckets.hits+1 END,expires_at=CASE WHEN rate_buckets.expires_at<now() THEN now()+interval '1 minute' ELSE rate_buckets.expires_at END RETURNING hits", key) <= count


async def body(request: Request):
    raw = await request.body()
    require(len(raw) <= 1048576, '请求内容过大', 413)
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError):
        raise BusinessError('请求格式不正确')
    require(isinstance(result, dict), '请求格式不正确')
    return result


def response(value, status=200):
    return JSONResponse(jsonable_encoder(value), status_code=status)


USER_FIELDS = 'u.id,u.email,u.name,u.role,u.status,u.balance,u.frozen,u.invite_code,u.fee_override'


def authenticate(request: Request, admin=False, apply_limit=True):
    authorization = request.headers.get('authorization')
    if authorization:
        require(not admin and request.url.path.startswith('/api/v1/'), '此接口不接受 API 密钥', 403)
        token = authorization.removeprefix('Bearer ')
        user = db.one('SELECT '+USER_FIELDS+' FROM users u JOIN api_keys k ON k.user_id=u.id WHERE k.hash=$1 AND NOT k.revoked', digest(token))
        if user:
            db.execute('UPDATE api_keys SET last_used=now() WHERE hash=$1', digest(token))
    else:
        token = request.cookies.get('xupay_session')
        require(bool(token), '请先登录', 401)
        user = db.one('SELECT '+USER_FIELDS+' FROM users u JOIN sessions s ON s.user_id=u.id WHERE s.hash=$1 AND s.expires_at>now()', digest(token))
    require(user is not None and user['status'] == 'active', '登录已失效或账户已停用', 401)
    request.state.user = user
    if admin and user['role'] != 'admin':
        event(request, 'permission_denied', 'high', '尝试访问运营接口')
        raise BusinessError('仅管理员可访问', 403)
    if not admin:
        from .features import enforce
        enforce(request, user)
    if apply_limit and not limit('api:'+user['id'], config('risk').get('api_per_minute',120)):
        event(request, 'rate_limit', 'medium', '账户请求频率超限')
        raise BusinessError('操作频繁，请稍后再试', 429)
    return user


async def member(request: Request):
    from starlette.concurrency import run_in_threadpool
    from .features import enforce_payload
    user = await run_in_threadpool(authenticate, request)
    await enforce_payload(request, user)
    return user


def administrator(request: Request):
    return authenticate(request, True)
