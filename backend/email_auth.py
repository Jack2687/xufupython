"""Email delivery and one-time credentials. Never persist plaintext codes or mail bodies."""
import hashlib
import hmac
import os
import re
import secrets
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from starlette.concurrency import run_in_threadpool
from . import db
from .common import (administrator, body, config, current, decrypt, digest, email_valid,
                     ip, limit, now, require, uid)

router = APIRouter(prefix='/api')
DOMAIN = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+')
CODE_SECONDS = 900
MAX_ATTEMPTS = 5


def security_config(q=None):
    result = config('security', q)
    return dict(result, registration_open=config('site', q).get('registration_open') is True)


def normalize_domains(value):
    if isinstance(value, str): value = re.split(r'[\s,，]+', value.strip()) if value.strip() else []
    require(isinstance(value, list) and len(value) <= 100, '邮箱域名白名单最多 100 项')
    result = []
    for raw in value:
        require(isinstance(raw, str), '邮箱域名格式无效')
        item = raw.strip().lower().lstrip('@')
        base = item[2:] if item.startswith('*.') else item
        require(len(base) <= 253 and DOMAIN.fullmatch(base), '邮箱域名格式无效：' + raw[:100])
        if item not in result: result.append(item)
    return result


def email_allowed(email, cfg):
    domain = email.rsplit('@', 1)[-1].lower()
    rules = cfg.get('email_domain_whitelist', [])
    return not rules or any(domain == rule or rule.startswith('*.') and
                           (domain == rule[2:] or domain.endswith('.' + rule[2:])) for rule in rules)


def mail_ready(value=None):
    value = value if value is not None else config('mail')
    return bool(value.get('enabled') is True and value.get('host') and value.get('from_email') and
                (not value.get('username') or value.get('password_enc')))


def public_mail(value):
    return {**{k: v for k, v in value.items() if k != 'password_enc'},
            'password': '', 'password_configured': bool(value.get('password_enc')),
            'ready': mail_ready(value)}


def code_hash(email, purpose, code):
    return hmac.new(os.environ['MASTER_KEY'].encode(), f'{purpose}\0{email}\0{code}'.encode(), hashlib.sha256).hexdigest()


def consume(q, email, purpose, code, user=None):
    """Return a message instead of raising: failed-attempt increments must commit."""
    row = q.one('SELECT * FROM email_challenges WHERE email=$1 AND purpose=$2 FOR UPDATE', email, purpose)
    invalid = '验证码不正确或已失效，请重新获取'
    if not row or not row['delivered'] or row['expires_at'] <= now() or row['attempts'] >= MAX_ATTEMPTS:
        return invalid
    q.execute('UPDATE email_challenges SET attempts=attempts+1 WHERE email=$1 AND purpose=$2', email, purpose)
    if not isinstance(code, str) or not re.fullmatch(r'\d{6}', code) or not hmac.compare_digest(row['secret_hash'], code_hash(email, purpose, code)):
        return invalid
    if purpose == 'reset' and (not user or user['status'] != 'active' or row['user_id'] != user['id'] or row['credential_hash'] != digest(user['password_hash'])):
        return invalid
    q.execute('DELETE FROM email_challenges WHERE email=$1 AND purpose=$2', email, purpose)
    return ''


def smtp_send(recipient, subject, text):
    cfg = config('mail')
    require(mail_ready(cfg), '请先在邮件设置中配置并启用邮件服务', 503)
    message = EmailMessage()
    message['From'] = formataddr((cfg['from_name'] or config('site')['name'], cfg['from_email']))
    message['To'] = recipient
    message['Subject'] = subject
    message.set_content(text)
    context = ssl.create_default_context()
    transport = smtplib.SMTP_SSL if cfg['encryption'] == 'ssl' else smtplib.SMTP
    kwargs = {'context': context} if cfg['encryption'] == 'ssl' else {}
    with transport(cfg['host'], cfg['port'], timeout=15, **kwargs) as client:
        client.ehlo()
        if cfg['encryption'] == 'starttls':
            client.starttls(context=context)
            client.ehlo()
        if cfg['username']: client.login(cfg['username'], decrypt(cfg['password_enc']))
        refused = client.send_message(message)
        require(not refused, '收件地址被邮件服务器拒绝', 502)


def failure_reason(error):
    if isinstance(error, smtplib.SMTPAuthenticationError): return 'SMTP 认证失败，请检查账号与授权码'
    if isinstance(error, (ssl.SSLError, smtplib.SMTPNotSupportedError)): return '加密连接失败，请检查端口与加密方式'
    if isinstance(error, smtplib.SMTPRecipientsRefused): return '收件地址被邮件服务器拒绝'
    if isinstance(error, (TimeoutError, OSError)): return '连接邮件服务器失败或超时'
    return '邮件服务器未接受发送，请检查邮件配置'


def deliver(delivery_id, recipient, purpose, code=None, fingerprint=None):
    label = {'register': '注册验证码', 'reset': '密码重置验证码', 'test': '邮件发送测试'}[purpose]
    text = (f'您的{label}是：{code}\n\n验证码 15 分钟内有效，仅可使用一次。请勿将验证码提供给他人。'
            if code else '这是一封测试邮件。您的站点已成功连接邮件服务器。')
    text += '\n\n如果这不是您本人的操作，请忽略此邮件。'
    try:
        smtp_send(recipient, config('site')['name'] + ' · ' + label, text)
    except Exception as error:
        reason = failure_reason(error)
        db.execute("UPDATE mail_deliveries SET status='failed',detail=$2,completed_at=now() WHERE id=$1", delivery_id, reason)
        return reason
    with db.transaction() as q:
        q.execute("UPDATE mail_deliveries SET status='sent',detail='邮件服务器已接受发送',completed_at=now() WHERE id=$1", delivery_id)
        if code:
            q.execute('UPDATE email_challenges SET delivered=true WHERE email=$1 AND purpose=$2 AND secret_hash=$3', recipient, purpose, fingerprint)
    return ''


def daily_limit(key, maximum):
    return db.scalar("INSERT INTO rate_buckets(key,hits,expires_at) VALUES($1,1,now()+interval '1 day') ON CONFLICT(key) DO UPDATE SET hits=CASE WHEN rate_buckets.expires_at<now() THEN 1 ELSE rate_buckets.hits+1 END,expires_at=CASE WHEN rate_buckets.expires_at<now() THEN now()+interval '1 day' ELSE rate_buckets.expires_at END RETURNING hits", key) <= maximum


@router.post('/auth/email-code')
async def send_code(request: Request, background: BackgroundTasks):
    value = await body(request)
    purpose = value.get('purpose')
    email = str(value.get('email', '')).strip().lower()
    require(purpose in ('register', 'reset') and email_valid(email), '请填写有效邮箱')
    cfg = security_config()
    if purpose == 'register':
        require(cfg['registration_open'], '暂未开放注册', 403)
        require(cfg.get('email_verify_enabled') is True, '当前注册无需邮箱验证码', 403)
        require(email_allowed(email, cfg), '此邮箱域名暂不支持注册')
    require(mail_ready(), '邮件服务暂未开放，请联系管理员', 503)
    require(limit('email-code:' + ip(request), 5) and daily_limit('email-day-ip:' + ip(request), 100), '发送过于频繁，请稍后再试', 429)
    code = f'{secrets.randbelow(1000000):06d}'
    fingerprint = code_hash(email, purpose, code)
    delivery_id = None
    with db.transaction() as q:
        q.execute('SELECT pg_advisory_xact_lock(1886030,hashtext($1))', purpose + ':' + email)
        previous = q.one('SELECT created_at FROM email_challenges WHERE email=$1 AND purpose=$2', email, purpose)
        require(not previous or (now() - previous['created_at']).total_seconds() >= 60, '请在 60 秒后重新获取验证码', 429)
        require(daily_limit('email-day-recipient:' + digest(email), 20), '此邮箱今日发送次数已达上限，请明天再试', 429)
        user = q.one('SELECT id,password_hash,status FROM users WHERE email=$1 AND deleted_at IS NULL', email)
        eligible = not user if purpose == 'register' else bool(user and user['status'] == 'active')
        q.execute("INSERT INTO email_challenges(email,purpose,secret_hash,user_id,credential_hash,expires_at) VALUES($1,$2,$3,$4,$5,now()+interval '15 minutes') ON CONFLICT(email,purpose) DO UPDATE SET secret_hash=EXCLUDED.secret_hash,user_id=EXCLUDED.user_id,credential_hash=EXCLUDED.credential_hash,expires_at=EXCLUDED.expires_at,created_at=now(),attempts=0,delivered=false",
                  email, purpose, fingerprint, user['id'] if user else None, digest(user['password_hash']) if user else '')
        if eligible:
            delivery_id = uid()
            q.execute('INSERT INTO mail_deliveries(id,recipient,purpose) VALUES($1,$2,$3)', delivery_id, email, purpose)
    if delivery_id: background.add_task(deliver, delivery_id, email, purpose, code, fingerprint)
    # Same response for unknown/disabled/existing accounts to avoid account enumeration.
    return {'ok': True, 'retry_after': 60, 'expires_in': CODE_SECONDS,
            'message': '若邮箱符合条件，验证码将发送到该邮箱，请检查收件箱或垃圾邮件。'}


@router.get('/admin/mail/logs', dependencies=[Depends(administrator)])
def delivery_logs(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    return {'items': db.all('SELECT * FROM mail_deliveries ORDER BY created_at DESC,id DESC LIMIT $1 OFFSET $2', page_size, (page-1)*page_size),
            'total': db.scalar('SELECT count(*) FROM mail_deliveries'), 'page': page, 'page_size': page_size}


@router.post('/admin/mail/test', dependencies=[Depends(administrator)])
async def test_delivery(request: Request):
    value = await body(request)
    recipient = str(value.get('email', '')).strip().lower()
    require(email_valid(recipient), '请输入有效的测试收件邮箱')
    require(mail_ready(), '请先保存并启用邮件配置')
    require(limit('mail-test:' + current(request)['id'], 3), '测试过于频繁，请稍后再试', 429)
    delivery_id = uid()
    db.execute("INSERT INTO mail_deliveries(id,recipient,purpose) VALUES($1,$2,'test')", delivery_id, recipient)
    error = await run_in_threadpool(deliver, delivery_id, recipient, 'test')
    require(not error, error, 502)
    return {'ok': True, 'message': '邮件服务器已接受发送，请检查收件箱'}
