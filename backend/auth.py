import secrets
from fastapi import APIRouter, Depends, Request
from . import db
from .common import *

router = APIRouter(prefix='/api')


def create_session(user_id):
    token = secrets.token_urlsafe(32)
    hours = config('risk').get('session_hours',24)
    db.execute("INSERT INTO sessions(hash,user_id,expires_at) VALUES($1,$2,now()+($3*interval '1 hour'))",digest(token),user_id,hours)
    result = response({'ok':True})
    result.set_cookie('xupay_session',token,max_age=hours*3600,httponly=True,secure=os.getenv('COOKIE_SECURE')=='true',samesite='strict')
    return result


@router.post('/auth/login')
async def login(request: Request):
    if not limit('login:'+ip(request),config('risk').get('login_per_minute',10)):
        event(request,'login_rate','high','登录尝试频繁')
        raise BusinessError('登录尝试过多，请一分钟后再试',429)
    v = await body(request)
    user = db.one('SELECT id,password_hash,status FROM users WHERE email=$1',str(v.get('email','')).strip().lower())
    dummy = '$2b$12$vuCQLWceDWoxSWQG6TLbqep2q5iYtXvwi5ok8gQ9MluCb8FH13H96'
    valid = password_matches(str(v.get('password','')),user['password_hash'] if user else dummy)
    if not user or not valid or user['status'] != 'active':
        event(request,'login_failed','medium','账号或密码校验失败')
        raise BusinessError('账号或密码不正确，或账户已停用',401)
    return create_session(user['id'])


@router.post('/auth/logout')
def logout(request: Request):
    token = request.cookies.get('xupay_session','')
    if token: db.execute('DELETE FROM sessions WHERE hash=$1',digest(token))
    result = response({'ok':True})
    result.delete_cookie('xupay_session',httponly=True,secure=os.getenv('COOKIE_SECURE')=='true',samesite='strict')
    return result


@router.post('/auth/register')
async def register(request: Request):
    require(config('site').get('registration_open') is True,'暂未开放注册',403)
    require(limit('register:'+ip(request),5),'请稍后再试',429)
    v = await body(request)
    email = str(v.get('email','')).strip().lower(); name=v.get('name',''); pw=v.get('password','')
    require(email_valid(email) and isinstance(name,str) and 1 <= length(name.strip()) <= 80 and 10 <= length(pw) <= 72,'请填写有效邮箱、称呼和 10～72 位密码')
    inviter = None
    if v.get('invite'):
        inviter = db.scalar('SELECT id FROM users WHERE invite_code=$1',v['invite'])
        require(inviter is not None,'邀请标识无效')
    user_id=uid()
    from psycopg2.errors import UniqueViolation
    try:
        db.execute('INSERT INTO users(id,email,name,password_hash,invite_code,invited_by) VALUES($1,$2,$3,$4,$5,$6)',user_id,email,name,password_hash(pw),uid()[:10],inviter)
    except UniqueViolation:
        raise BusinessError('该邮箱已注册或暂不可用',409)
    return create_session(user_id)


@router.patch('/me',dependencies=[Depends(member)])
async def profile(request: Request):
    v=await body(request); name=v.get('name',''); pw=v.get('password','')
    require(1 <= length(name) <= 80,'称呼长度须为 1～80')
    user_id=current(request)['id']
    with db.transaction() as q:
        hashed=q.scalar('SELECT password_hash FROM users WHERE id=$1 FOR UPDATE',user_id)
        if pw:
            require(10 <= length(pw) <= 72 and password_matches(v.get('old_password',''),hashed),'原密码不正确或新密码不足 10 位')
            hashed=password_hash(pw)
            q.execute('DELETE FROM sessions WHERE user_id=$1',user_id)
        q.execute('UPDATE users SET name=$2,password_hash=$3,updated_at=now() WHERE id=$1',user_id,name,hashed)
        audit(q,request,'profile.updated',user_id,'更新个人资料')
    return {'ok':True,'relogin':bool(pw)}
