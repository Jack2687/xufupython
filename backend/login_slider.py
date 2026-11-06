"""One-use, browser-bound login slider challenges (independent of upstream services)."""
import base64
import secrets
from fastapi import APIRouter, Request
from . import db
from .common import config, digest, limit, ip, require, response, body
from .request_source import cookie_secure

router=APIRouter(prefix='/api/auth/slider')
COOKIE='login_slider'


@router.get('')
def challenge(request:Request):
    require(limit('slider:'+ip(request),30),'验证请求过多，请稍后重试',429)
    if not config('security').get('login_slider_enabled',True):return {'enabled':False}
    binding=request.cookies.get(COOKIE) or secrets.token_urlsafe(32)
    ident=secrets.token_hex(24);x=secrets.randbelow(171)+90
    db.execute('DELETE FROM auth_slider_challenges WHERE expires_at<now()')
    db.execute("INSERT INTO auth_slider_challenges(id,binding,target,expires_at) VALUES($1,$2,$3,now()+interval '2 minutes')",ident,digest(binding),x)
    svg=f'<svg xmlns="http://www.w3.org/2000/svg" width="320" height="90"><rect width="320" height="90" rx="10" fill="#edf5f2"/><path d="M0 68 Q80 0 160 66 T320 40" fill="none" stroke="#bdd7ce" stroke-width="12"/><rect x="{x}" y="25" width="36" height="40" rx="7" fill="#315f50" stroke="#174a3b" stroke-dasharray="4 3"/></svg>'
    out=response({'enabled':True,'id':ident,'image':'data:image/svg+xml;base64,'+base64.b64encode(svg.encode()).decode()})
    out.set_cookie(COOKIE,binding,max_age=600,httponly=True,secure=cookie_secure(request),samesite='strict')
    return out


@router.post('/verify')
async def verify(request:Request):
    require(limit('slider-verify:'+ip(request),60),'验证请求过多，请稍后重试',429)
    v=await body(request);x=v.get('x');ident=v.get('id','')
    require(type(x) in (int,float) and 0<=x<=284 and isinstance(ident,str) and len(ident)<=64,'请重新完成滑动验证')
    # Consume the challenge even on an incorrect attempt; failures cannot be brute-forced.
    row=db.one("DELETE FROM auth_slider_challenges WHERE id=$1 AND binding=$2 AND proof_hash IS NULL RETURNING target,created_at,expires_at",ident,digest(request.cookies.get(COOKIE,'')))
    from .common import now
    require(row and row['expires_at']>now() and (now()-row['created_at']).total_seconds()>=.3 and abs(x-row['target'])<=5,'滑动位置不正确或验证已过期，请重试')
    proof=secrets.token_urlsafe(32)
    db.execute("INSERT INTO auth_slider_challenges(id,binding,target,proof_hash,expires_at) VALUES($1,$2,0,$3,now()+interval '1 minute')",ident,digest(request.cookies.get(COOKIE,'')),digest(proof))
    return {'proof':proof,'expires_in':60}


def consume(request,proof):
    if not config('security').get('login_slider_enabled',True):return
    require(isinstance(proof,str) and 1<=len(proof)<=128,'请先完成滑动验证',400)
    row=db.one('DELETE FROM auth_slider_challenges WHERE proof_hash=$1 AND binding=$2 AND expires_at>now() RETURNING id',digest(proof),digest(request.cookies.get(COOKIE,'')))
    require(row,'滑动验证已失效，请重新验证',400)
