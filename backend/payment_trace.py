"""Order diagnostics: fixed metadata only, never request bodies, URLs or credentials."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import json
import logging
import re
import time
from urllib.parse import urlsplit

from . import db, http_evidence
from .checkout_errors import CheckoutError

log = logging.getLogger('xufu.payment_trace')
_current = ContextVar('payment_trace', default=None)
PHASES = {
    'accepted': '申请受理与资料校验', 'queue': '等待办理资源', 'funds': '服务费处理',
    'proxy': '分配办理网络', 'network': '检查办理网络', 'checkout': '生成结账信息',
    'opening': '初始化付款页面', 'billing': '填写账单资料', 'pricing': '核对金额与税费',
    'card_prepare': '准备银行卡付款', 'confirming': '提交付款', 'checking': '核实付款结果',
    'result': '付款处理结果', 'session_refresh': '刷新账号授权', 'account_check': '在线核对账号',
    'activation': '核实订阅生效', 'renewal': '自动续费设置', 'complete': '办理流程结果',
}
CODES = {
    '': '', 'completed': '步骤已完成', 'request_started': '开始请求，等待连接或返回响应',
    'response_received': '请求已返回；接口返回不代表付款或订阅成功',
    'timeout': '请求超时', 'proxy_connect': '代理隧道连接失败', 'dns': '域名解析失败',
    'tls': '安全握手失败', 'connection': '网络连接中断或不可用',
    'invalid_response': '上游响应格式或必要字段无效', 'request_rejected': '上游拒绝请求',
    'access_denied': '上游拒绝访问', 'upstream_busy': '上游限流或暂时繁忙',
    'invalid_session': '账号授权失效', 'account_mismatch': '订阅账号与授权不一致',
    'proxy_session_expired': '本单代理会话已过期', 'proxy_ip_missing': '缺少本单出口记录',
    'proxy_ip_changed': '当前出口与本单初始出口不一致', 'proxy_ip_check_failed': '代理出口检查未通过',
    'billing_incomplete': '本单账单资料不完整', 'billing_selected': '已从本单快照读取账单资料',
    'hold': '服务费已冻结', 'release': '服务费已解冻', 'charge': '服务费已结算',
    'link_ready': '结账链接已生成，等待用户付款', 'payment_succeeded': '已确认付款成功',
    'activation_confirmed': '已确认订阅生效', 'activation_pending': '等待订阅生效核实',
    'renewal_disabled': '已确认关闭自动续费', 'renewal_kept': '按要求保留自动续费',
    'renewal_pending': '等待续费处理', 'review': '需要核实处理结果',
    'payment_result_unconfirmed': '付款结果尚未确认', 'payment_declined': '付款被拒绝',
    'approval_blocked': '商户批准未通过', 'internal_error': '步骤执行异常，请结合前后技术记录核实',
    'subscription_absent': '当前查询尚无订阅，等待后续生效核实',
    'proxy_unavailable': '当前地区没有可用代理资源',
    'proxy_ip_matched': '当前出口与本单付款时保存的出口一致',
    'proxy_session_changed': '本单原代理会话记录已变化，已停止收尾',
    'proxy_session_missing': '缺少本单已保存的代理会话',
    'invalid_return_url': '回跳地址不符合本单校验要求，未访问该地址',
    'session_token_required': '缺少刷新登录态所需的 Session Cookie',
    'session_expired': '刷新接口未返回有效登录态',
    'post_payment_wait': '付款已成功，按配置等待后强制刷新 Session',
    'post_payment_wait_completed': '付款后等待结束，即将强制刷新 Session',
    'renewal_state_unknown': '自动续费状态缺失或冲突，未提交取消请求',
    'cancellation_pending': '已请求关闭续费，结果仍待核实',
    'checkpoint_missing': '本单没有可恢复的付款后收尾记录',
    'checkpoint_mismatch': '付款后收尾记录与本单结账记录不一致',
    'payment_evidence_missing': '本单缺少已付款凭据，未执行付款后流程',
    'unsupported_plan': '无法匹配本单订阅套餐',
    'network_error': '付款后请求网络异常',
    'request_retry_wait': '查询遇到短暂故障，等待后重试',
    'network_retry_scheduled': '网络故障，已按配置安排重试',
    'subscription_not_found': '订阅查询尚无结果，不能据此确认续费已关闭',
    'go_followup_unavailable': 'Go 付款后程序未安装，请检查本地构建或部署镜像',
    'go_followup_protocol_error': 'Go 付款后程序通信异常，已保存收尾进度',
    'go_local_proxy_unsupported': '本机代理链暂不支持此代理协议，已停止收尾',
    'deadline_exceeded': '本轮付款后处理达到时间上限',
    'checkpoint_failed': '收尾进度保存失败，已停止后续操作',
    'invalid_json_response': '上游未返回有效 JSON，请结合 HTTP 状态查看响应摘要',
    'network_timeout': '付款后网络请求超时',
    'checkout_failed': '提链失败，请查看原始错误码',
    'checkout_existing_subscription': '账号已有订阅，无法继续提链',
    'checkout_account_unavailable': '账号不存在或不符合套餐要求',
    'checkout_verification_failed': '提链验证未通过，请查看原始错误码',
    'checkout_program_unavailable': '提链程序不可用，请检查部署',
}
REQUEST_PHASES = {
    'exit_probe': 'network', 'direct_exit_probe': 'network', 'checkout_create': 'checkout', 'checkout_initialize': 'opening',
    'billing_update': 'pricing', 'payment_method_create': 'card_prepare',
    'payment_confirm': 'confirming', 'payment_approve': 'confirming', 'payment_read': 'checking',
    'session_read': 'session_refresh', 'account_read': 'account_check',
    'subscription_read': 'activation', 'renewal_update': 'renewal',
    'merchant_return': 'activation', 'renewal_read': 'renewal',
}
_REQUEST_ID = re.compile(r'(?:req_[A-Za-z0-9]{6,64}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})\Z')
_DIAGNOSTIC_ENUMS = {
    'request_kind': set(REQUEST_PHASES),
    'network_reason': {'timeout', 'proxy_connect', 'dns', 'tls', 'connection'},
    'response_kind': {'json', 'html', 'empty', 'other'},
    'email_source': {'system_billing', 'subscription_account'},
    'network_stage': {'connection', 'request_write', 'response_headers', 'response_body'},
}
_dropped = 0


def error_code(exc):
    if isinstance(exc, CheckoutError):
        return exc.category
    # Error text is used only to select a fixed category; it is never persisted.
    followup = getattr(exc, 'post_payment_code', None)
    if followup in CODES:
        return followup
    if isinstance(followup, str) and re.fullmatch(r'http_[1-5]\d\d', followup):
        return {'http_401': 'invalid_session', 'http_403': 'access_denied',
                'http_429': 'upstream_busy'}.get(followup, 'request_rejected')
    text = (type(exc).__name__ + ' ' + str(exc)).lower()
    for code in ('proxy_session_expired', 'proxy_ip_missing', 'proxy_ip_changed',
                 'proxy_ip_check_failed', 'invalid_session', 'account_mismatch',
                 'payment_result_unconfirmed', 'payment_declined', 'approval_blocked', 'invalid_response'):
        if code in text:
            return code
    if '账单' in text:
        return 'billing_incomplete'
    if '无可用代理' in text:
        return 'proxy_unavailable'
    if 'timeout' in text or 'timed out' in text or 'code=28' in text:
        return 'timeout'
    if 'proxy' in text and ('connect' in text or 'error' in text):
        return 'proxy_connect'
    if 'resolve' in text or 'dns' in text or re.search(r'code=[56]\)', text):
        return 'dns'
    if 'ssl' in text or 'tls' in text or 'certificate' in text:
        return 'tls'
    if 'connect' in text or 'curl' in text:
        return 'connection'
    if 'json' in text or '未返回' in text:
        return 'invalid_response'
    return 'internal_error'


def _diagnostics(values):
    result = {}
    for key, value in (values or {}).items():
        if key in _DIAGNOSTIC_ENUMS and isinstance(value, str) and value in _DIAGNOSTIC_ENUMS[key]:
            result[key] = value
        elif key == 'request_attempt' and type(value) is int and 1 <= value <= 10000:
            result[key] = value
        elif key == 'checkout_exit_code' and type(value) is int and -65535 <= value <= 65535:
            result[key] = value
        elif key in {'retry_number', 'retry_limit'} and type(value) is int and 0 <= value <= 5:
            result[key] = value
        elif key == 'retry_delay_seconds' and type(value) is int and 5 <= value <= 3600:
            result[key] = value
        elif key == 'checkout_reused' and type(value) is bool:
            result[key] = value
        elif key == 'post_payment_delay_seconds' and type(value) is int and 0 <= value <= 300:
            result[key] = value
        elif key == 'post_payment_wait_ms' and type(value) is int and 0 <= value <= 300000:
            result[key] = value
        elif key in {'connection_ready_ms', 'request_write_ms', 'first_byte_ms',
                     'response_wait_ms', 'network_stage_elapsed_ms'} and type(value) in (int, float) and 0 <= value <= 240000:
            result[key] = round(value, 2)
        else:
            safe = http_evidence.filter_field(key, value)
            if safe is not None and safe != '':
                result[key] = safe
    return result


def record(phase, state, code='', *, order_id=None, round=None, elapsed_ms=0,
           http_status=0, request_id='', diagnostics=None, q=None):
    ctx = _current.get()
    order_id = order_id or (ctx and ctx['order_id'])
    if not order_id or phase not in PHASES or state not in {'running', 'completed', 'failed', 'review', 'waiting'}:
        return
    if round is None and ctx and ctx['order_id'] == order_id:
        round = ctx['round']
    values = (order_id, phase, state, code if code in CODES else 'internal_error',
              round if type(round) is int and round > 0 else None, max(0, int(elapsed_ms)),
              http_status if type(http_status) is int and 100 <= http_status <= 599 else 0,
              request_id if isinstance(request_id, str) and _REQUEST_ID.fullmatch(request_id) else '',
              _diagnostics(diagnostics))
    sql = ('INSERT INTO payment_trace_events(order_id,phase,state,code,round,elapsed_ms,http_status,request_id,diagnostics) '
           'VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)')
    if q is not None:
        # Critical milestones commit or roll back with their business state.
        q.execute(sql, *values)
        return
    try:
        with db.transaction() as tx:
            tx.execute("SET LOCAL statement_timeout='500ms'")
            tx.execute("SET LOCAL lock_timeout='300ms'")
            tx.execute(sql, *values)
    except Exception:
        global _dropped
        _dropped += 1
        log.warning('Diagnostic write failed; dropped=%s', _dropped)


@contextmanager
def scope(order_id, round=None):
    token = _current.set({'order_id': order_id, 'round': round, 'attempts': {}})
    try:
        yield
    finally:
        _current.reset(token)


def order_run(fn):
    @wraps(fn)
    def wrapped(job):
        with scope(job['id'], job.get('trace_round')):
            return fn(job)
    return wrapped


@contextmanager
def step(phase):
    started = time.monotonic()
    record(phase, 'running')
    try:
        yield
    except Exception as exc:
        record(phase, 'failed', error_code(exc), elapsed_ms=round_ms(started))
        raise
    else:
        record(phase, 'completed', 'completed', elapsed_ms=round_ms(started))


def phase(name):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            with step(name):
                return fn(*args, **kwargs)
        return wrapped
    return decorate


def round_ms(started):
    return max(0, round((time.monotonic() - started) * 1000))


class RequestObservation:
    def __init__(self, kind):
        self.kind = kind
        self.status = 0
        self.request_id = ''
        self.code = ''
        self.diagnostics = {'request_kind': kind}

    def response(self, status, headers=None, body=None, *, secrets=(), failure_details=False):
        self.status = status
        headers = {str(k).lower(): v for k, v in (headers or {}).items()}
        self.request_id = headers.get('request-id') or headers.get('x-request-id') or ''
        if status >= 400:
            self.code = {401: 'invalid_session', 403: 'access_denied', 429: 'upstream_busy'}.get(status, 'request_rejected')
        if isinstance(body, str):
            try:
                json.loads(body)
                kind = 'json'
            except (ValueError, TypeError):
                kind = 'empty' if not body.strip() else 'html' if body.lstrip().startswith('<') else 'other'
            self.diagnostics['response_kind'] = kind
        if failure_details:
            self.diagnostics.update(http_evidence.failure_evidence(status, headers, body, secrets))


@contextmanager
def request(kind):
    observation = RequestObservation(kind)
    ctx = _current.get()
    if not ctx or kind not in REQUEST_PHASES:
        yield observation
        return
    counts = ctx['attempts']
    counts[kind] = counts.get(kind, 0) + 1
    observation.diagnostics['request_attempt'] = counts[kind]
    phase = REQUEST_PHASES[kind]
    started = time.monotonic()
    record(phase, 'running', 'request_started', diagnostics=observation.diagnostics)
    try:
        yield observation
    except Exception as exc:
        if isinstance(exc, CheckoutError):
            observation.diagnostics.update(response_error_code=exc.code, response_error_type='go_checkout')
            if exc.exit_code is not None:
                observation.diagnostics['checkout_exit_code'] = exc.exit_code
            if re.fullmatch(r'http_[1-5]\d\d', exc.code):
                observation.status = int(exc.code[5:])
            log.warning('[%s] checkout failure: code=%s category=%s exit=%s',
                        ctx['order_id'], exc.code, exc.category, exc.exit_code)
        observation.code = observation.code or error_code(exc)
        raise
    finally:
        code = observation.code
        if code in _DIAGNOSTIC_ENUMS['network_reason']:
            observation.diagnostics['network_reason'] = code
        record(phase, 'failed' if code and code != 'subscription_absent' else 'completed', code or 'response_received',
               elapsed_ms=round_ms(started), http_status=observation.status,
               request_id=observation.request_id, diagnostics=observation.diagnostics)


def request_kind(url):
    parsed = urlsplit(url)
    path = parsed.path
    if parsed.hostname == 'ipinfo.io': return 'exit_probe'
    if parsed.hostname == 'chatgpt.com':
        if path == '/backend-api/payments/checkout': return 'checkout_create'
        if path.endswith('/approve'): return 'payment_approve'
        if path == '/api/auth/session': return 'session_read'
        if path.startswith('/backend-api/accounts/'): return 'account_read'
        if path == '/backend-api/subscriptions/cancel': return 'renewal_update'
        if path == '/backend-api/subscriptions': return 'subscription_read'
    if parsed.hostname == 'api.stripe.com' and path.endswith('/init'): return 'checkout_initialize'
    return ''


def curl_call(fn):
    @wraps(fn)
    def wrapped(method, url, **kwargs):
        with request(request_kind(url)) as observation:
            status, body = fn(method, url, **kwargs)
            observation.response(status, body=body)
            return status, body
    return wrapped


def http_call(kind, call, *args, **kwargs):
    with request(kind) as observation:
        response = call(*args, **kwargs)
        observation.response(response.status_code, response.headers)
        return response


def details(order_id):
    rows = db.all('SELECT id,phase,state,code,round,elapsed_ms,http_status,request_id,diagnostics,created_at '
                  'FROM payment_trace_events WHERE order_id=$1 ORDER BY created_at,id', order_id)
    for row in rows:
        row.update(id='technical:' + str(row['id']), source='technical',
                   label=PHASES.get(row['phase'], '执行步骤'), description=CODES.get(row['code'], ''))
    return rows
