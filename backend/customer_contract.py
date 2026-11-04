"""Validation and replay protection for customer API mutations."""
import hashlib
import hmac
import json
import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .common import BusinessError, current, digest, require


class Payload(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Card(Payload):
    number: str = Field(pattern=r'^\d{12,19}$')
    month: str = Field(pattern=r'^(0?[1-9]|1[0-2])$')
    year: str = Field(pattern=r'^(\d{2}|\d{4})$')
    cvc: str = Field(pattern=r'^\d{3,4}$')


class OrderRequest(Payload):
    workflow: Literal['link', 'subscription']
    product_id: str = Field(min_length=1, max_length=80, pattern=r'^[a-z0-9_]+$')
    session: dict | str
    country: Literal['PH', 'US', 'JP', 'SG', 'GB', 'EG', 'TR'] = 'PH'
    seats: int = Field(default=1, ge=1, le=100)
    premium_seats: int = Field(default=1, ge=1, le=100)
    workspace_name: str = Field(default='work', min_length=1, max_length=100)
    keep_renewal: bool = False
    accepted: bool = False
    card: Card | None = None


class RenewalRequest(Payload):
    session: dict | str
    accepted: Literal[True]


class TopupRequest(Payload):
    amount: int = Field(ge=100)
    proof: str = Field(default='', max_length=500)


def prepare(request, model, payload):
    key = request.headers.get('Idempotency-Key', '')
    require(8 <= len(key) <= 120 and key.isascii() and key.isprintable(),
            '请提供 8～120 位可打印 ASCII Idempotency-Key')
    try:
        value = model.model_validate(payload).model_dump()
        if isinstance(value.get('session'), str):
            value['session'] = json.loads(value['session'])
        if 'session' in value:
            require(isinstance(value['session'], dict), 'Session 必须是 JSON 对象')
        if model is OrderRequest:
            require(value['premium_seats'] <= value['seats'], '高级席位不能超过总席位')
            if value['workflow'] == 'subscription':
                require(value['accepted'] is True, '请确认服务约定')
                require(value['card'] is not None, '请提供银行卡信息')
            else:
                require(value['card'] is None, '生成链接无需提交银行卡')
        canonical = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    except ValidationError as error:
        fields = ', '.join('.'.join(map(str, e['loc'])) for e in error.errors(include_input=False))
        raise BusinessError('请检查参数类型、必填项和取值范围：' + fields) from None
    except (ValueError, TypeError):
        raise BusinessError('Session JSON 格式不正确') from None
    request.state.customer_hash = 'api2:' + hmac.new(
        os.environ['MASTER_KEY'].encode(), canonical.encode(), hashlib.sha256).hexdigest()
    return value


def order_hash(q, request, legacy):
    fingerprint = getattr(request.state, 'customer_hash', None)
    if fingerprint:
        q.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))',
                  'api-order:' + current(request)['id'])
    return fingerprint or legacy


def mutation_key(request, operation):
    return 'customer-api:' + digest(json.dumps([
        current(request)['id'], operation, request.headers['Idempotency-Key']]))


def replay(q, request, operation, lock=False):
    key = mutation_key(request, operation)
    if lock:
        q.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))', key)
    old = q.one('SELECT request_hash,result FROM mutation_keys WHERE key=$1 AND actor_id=$2',
                key, current(request)['id'])
    if old:
        require(old['request_hash'] == request.state.customer_hash,
                '同一 Idempotency-Key 不能用于不同请求', 409)
        return dict(old['result'], replayed=True)


def remember(q, request, operation, result):
    q.execute('INSERT INTO mutation_keys(key,actor_id,request_hash,result) VALUES($1,$2,$3,$4)',
              mutation_key(request, operation), current(request)['id'], request.state.customer_hash, result)
