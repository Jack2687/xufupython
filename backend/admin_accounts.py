from fastapi import APIRouter,Depends,Request
from psycopg2.errors import UniqueViolation
from . import db
from .common import *
from .wallet import credit
from .membership import membership_from,SPEND
from .product_ids import internal_id

router=APIRouter(prefix='/api/admin',dependencies=[Depends(administrator)])


@router.get('/users')
def users(q: str=''):
    rows=db.all("SELECT u.id,u.email,u.name,u.role,u.status,u.balance,u.frozen,u.fee_override,u.membership_code,u.membership_until,u.invite_code,u.created_at,(SELECT count(*) FROM orders o WHERE o.user_id=u.id AND o.deleted_at IS NULL) AS orders,COALESCE(s.spent,0)::bigint AS spent FROM users u LEFT JOIN (SELECT user_id,sum(spent) AS spent FROM ("+SPEND+") s GROUP BY user_id) s ON s.user_id=u.id WHERE ($1='' OR u.email ILIKE '%'||$1||'%' OR u.name ILIKE '%'||$1||'%') ORDER BY u.created_at DESC LIMIT 500",q)
    prices={}
    for row in db.all('SELECT user_id,product_id,fee FROM user_product_prices'):
        prices.setdefault(row['user_id'],{})[row['product_id']]=row['fee']
    for row in rows:
        row['membership']=membership_from(row,row['spent']);row['product_prices']=prices.get(row['id'],{})
    return {'items':rows,'tiers':config('tiers')['items'],'products':db.all('SELECT id,name,fee FROM products WHERE enabled ORDER BY sort,id')}


def account_options(q,request,id,value):
    if 'membership_code' in value or 'membership_until' in value:
        old=q.one('SELECT membership_code,membership_until FROM users WHERE id=$1',id)
        code=value.get('membership_code',old['membership_code'])
        until=parsedate(value.get('membership_until',old['membership_until'].isoformat() if old['membership_until'] else None))
        require(isinstance(code,str) and (code=='' or any(t['code']==code for t in config('tiers',q)['items'])),'会员等级不存在')
        if not code:until=None
        require(until is None or until>now() or (code==old['membership_code'] and until==old['membership_until']),'会员到期时间必须晚于现在')
        q.execute('UPDATE users SET membership_code=$2,membership_until=$3 WHERE id=$1',id,code,until)
    if 'product_prices' in value:
        prices=value['product_prices'];require(isinstance(prices,dict) and len(prices)<=200,'套餐专属价格格式无效')
        enabled={p['id'] for p in q.all('SELECT id FROM products WHERE enabled')}
        normalized={}
        for product,fee in prices.items():
            require(isinstance(product,str) and internal_id(product) in enabled,'只能设置已开放套餐的价格')
            product=internal_id(product);require(product not in normalized,'套餐价格重复')
            normalized[product]=integer(fee,0,100000000,'套餐价格须为有效的非负整数金额')
        q.execute('DELETE FROM user_product_prices WHERE user_id=$1 AND product_id IN (SELECT id FROM products WHERE enabled)',id)
        for product,fee in normalized.items():
            q.execute('INSERT INTO user_product_prices(user_id,product_id,fee) VALUES($1,$2,$3)',id,product,fee)
        audit(q,request,'user.product_pricing',id,'更新开放套餐专属价格：'+','.join(normalized))


@router.post('/users')
async def create_user(request: Request):
    v=await body(request);email=str(v.get('email','')).strip().lower();name=v.get('name','');pw=v.get('password','');role=v.get('role')
    require(email_valid(email) and 1<=length(name)<=80 and 10<=length(pw)<=72 and role in ('member','admin'),'请检查邮箱、称呼、角色和密码（至少 10 位）')
    id=uid();hashed=password_hash(pw)
    try:
        with db.transaction() as q:
            q.execute('INSERT INTO users(id,email,name,password_hash,role,invite_code) VALUES($1,$2,$3,$4,$5,$6)',id,email,name,hashed,role,uid()[:10])
            account_options(q,request,id,v)
            audit(q,request,'user.created',id,'新建账户 / '+role)
    except UniqueViolation:
        raise BusinessError('邮箱已存在或不可用')
    return {'id':id}


@router.patch('/users/{id}')
async def update_user(id: str,request: Request):
    v=await body(request);email=str(v.get('email','')).strip().lower();name=v.get('name','');role=v.get('role');status=v.get('status');fee=v.get('fee_override');pw=v.get('password','')
    require((email_valid(email) or (email=='admin' and db.scalar("SELECT email='admin' FROM users WHERE id=$1",id))) and 1<=length(name)<=80 and role in ('member','admin') and status in ('active','disabled'),'账户参数无效')
    if fee is not None:integer(fee,0,100000000,'账户参数无效')
    if pw:require(10<=length(pw)<=72,'密码须为 10～72 位')
    require(id!=current(request)['id'] or (role=='admin' and status=='active'),'不能停用或降级当前管理员')
    try:
        with db.transaction() as q:
            q.execute('SELECT pg_advisory_xact_lock(1886001)')
            old=q.one('SELECT role,status FROM users WHERE id=$1 FOR UPDATE',id)
            require(old is not None,'用户不存在')
            if old['role']=='admin' and old['status']=='active' and (role!='admin' or status!='active'):
                require(q.scalar("SELECT count(*) FROM users WHERE role='admin' AND status='active'")>1,'至少保留一个可用管理员')
            q.execute('UPDATE users SET email=$2,name=$3,role=$4,status=$5,fee_override=$6,updated_at=now() WHERE id=$1',id,email,name,role,status,fee)
            account_options(q,request,id,v)
            if pw:q.execute('UPDATE users SET password_hash=$2 WHERE id=$1',id,password_hash(pw))
            if pw or status!='active' or role!=old['role']:
                q.execute('DELETE FROM sessions WHERE user_id=$1',id);q.execute('UPDATE api_keys SET revoked=true WHERE user_id=$1',id)
            audit(q,request,'user.updated',id,'资料、角色或单价修改')
    except UniqueViolation:
        raise BusinessError('邮箱已存在或修改失败')
    return {'ok':True}


@router.post('/users/pricing')
async def pricing(request: Request):
    v=await body(request);ids=v.get('ids',[]);fee=v.get('fee_override')
    require(isinstance(ids,list) and 1<=len(ids)<=500 and all(isinstance(x,str) for x in ids),'请选择账号并输入有效单价')
    if fee is not None:integer(fee,0,100000000)
    with db.transaction() as q:
        n=q.execute('UPDATE users SET fee_override=$2,updated_at=now() WHERE id=ANY($1)',sorted(ids),fee)
        audit(q,request,'user.pricing','batch',f'批量调整 {n} 个账户单价')
    return {'updated':n}


@router.post('/users/{id}/balance')
async def adjust_balance(id: str,request: Request):
    v=await body(request);amount=v.get('amount');reason=v.get('reason','');key=request.headers.get('Idempotency-Key','')
    integer(amount,-100000000000,100000000000)
    require(amount!=0 and 2<=length(reason)<=300 and 8<=len(key)<=120,'需要非零金额、调整原因和请求标识')
    rh=digest(json.dumps([id,amount,reason],ensure_ascii=False));lock=current(request)['id']+':'+key
    with db.transaction() as q:
        q.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))',lock)
        prior=q.one('SELECT request_hash FROM mutation_keys WHERE key=$1',lock)
        if prior:
            require(prior['request_hash']==rh,'重复请求内容不一致',409)
            return {'ok':True,'replayed':True}
        credit(q,request,id,'adjustment','adjust:'+lock,reason,amount)
        audit(q,request,'balance.adjusted',id,f'调整 {amount} 分；{reason}')
        q.execute('INSERT INTO mutation_keys(key,actor_id,request_hash,result) VALUES($1,$2,$3,$4)',lock,current(request)['id'],rh,{'ok':True})
    return {'ok':True}


@router.get('/membership')
def members():
    rows=db.all('SELECT u.id,u.email,u.name,u.role,u.status,u.fee_override,u.membership_code,u.membership_until,COALESCE(s.spent,0)::bigint AS spent FROM users u LEFT JOIN (SELECT user_id,sum(spent)::bigint AS spent FROM ('+SPEND+') s GROUP BY user_id) s ON s.user_id=u.id ORDER BY u.created_at DESC')
    distribution={};total=0
    for u in rows:
        m=membership_from(u,u['spent']);u['membership']=m
        if u['role']=='member':
            code=m['current']['code'];distribution[code]=distribution.get(code,0)+1;total+=1
    return {'items':rows,'config':config('tiers'),'distribution':distribution,'customers':total}


@router.put('/users/{id}/membership')
async def set_membership(id: str,request: Request):
    v=await body(request);reason=str(v.get('reason','')).strip();code=v.get('code','');until=parsedate(v.get('until'))
    require(2<=length(reason)<=500,'请填写调整原因（2～500 字节）')
    require(until is None or until>now(),'到期时间必须晚于现在')
    with db.transaction() as q:
        q.execute('SELECT pg_advisory_xact_lock(782518860)')
        require(code=='' or any(t['code']==code for t in config('tiers',q)['items']),'等级不存在')
        if not code:until=None
        require(q.execute('UPDATE users SET membership_code=$2,membership_until=$3,updated_at=now() WHERE id=$1',id,code,until)==1,'账户不存在',404)
        audit(q,request,'membership.adjusted',id,json.dumps({'code':code,'until':until.isoformat() if until else None,'reason':reason},ensure_ascii=False))
    return {'ok':True}


@router.get('/topups')
def topups():
    return {'items':db.all("SELECT t.*,u.email FROM topups t JOIN users u ON u.id=t.user_id ORDER BY (t.status='pending') DESC,t.created_at DESC LIMIT 500")}


@router.post('/topups/{id}/review')
async def review_topup(id: str,request: Request):
    v=await body(request);action=v.get('action');note=v.get('note','')
    require(action in ('approve','reject') and 2<=length(note)<=300,'请选择审核结果并填写审核依据')
    desired='approved' if action=='approve' else 'rejected'
    with db.transaction() as q:
        row=q.one('SELECT user_id,status,amount FROM topups WHERE id=$1 FOR UPDATE',id)
        require(row is not None,'申请不存在')
        if row['status']==desired:return {'ok':True,'replayed':True}
        require(row['status']=='pending','此申请已处理',409)
        if desired=='approved':credit(q,request,row['user_id'],'topup','topup:'+id,'人工核实充值到账',row['amount'])
        q.execute('UPDATE topups SET status=$2,note=$3,reviewed_by=$4,updated_at=now() WHERE id=$1',id,desired,note,current(request)['id'])
        audit(q,request,'topup.'+desired,id,note)
    return {'ok':True}


@router.get('/codes')
def codes():
    return {'items':db.all('SELECT c.id,c.prefix,c.amount,c.batch,c.status,c.used_at,c.expires_at,c.created_at,u.email AS used_by FROM redeem_codes c LEFT JOIN users u ON c.used_by=u.id ORDER BY c.created_at DESC LIMIT 1000')}


@router.post('/codes')
async def create_codes(request: Request):
    v=await body(request);count=integer(v.get('count'),1,200);amount=integer(v.get('amount'),100,10000000);days=integer(v.get('days'),1,3660)
    batch=now().astimezone().strftime('%Y%m%d')+'-'+uid()[:6];codes=[]
    with db.transaction() as q:
        for _ in range(count):
            code='XP-'+uid()[:24].upper();codes.append(code)
            q.execute("INSERT INTO redeem_codes(id,code_hash,code_enc,prefix,amount,batch,expires_at) VALUES($1,$2,$3,$4,$5,$6,now()+($7*interval '1 day'))",uid(),digest(code),encrypt(code),code[:7]+'…'+code[-4:],amount,batch,days)
        audit(q,request,'codes.created',batch,f'生成 {count} 个兑换码')
    return {'codes':codes,'batch':batch}


@router.post('/codes/batch')
async def batch_codes(request: Request):
    v=await body(request);ids=v.get('ids',[]);action=v.get('action')
    require(isinstance(ids,list) and 1<=len(ids)<=1000 and all(isinstance(x,str) for x in ids),'请选择兑换码')
    with db.transaction() as q:
        result={'ok':True}
        if action=='export':
            result={'codes':[decrypt(x['code_enc']) for x in q.all("SELECT code_enc FROM redeem_codes WHERE id=ANY($1) AND status='active' AND expires_at>now() ORDER BY created_at,id",ids)]}
        elif action=='copy':
            ids=list(dict.fromkeys(ids))
            result={'codes':[decrypt(x['code_enc']) for x in q.all("SELECT code_enc FROM redeem_codes WHERE id=ANY($1) ORDER BY array_position($1::text[],id)",ids)]}
        elif action=='disable':
            result={'updated':q.execute("UPDATE redeem_codes SET status='disabled' WHERE id=ANY($1) AND status='active'",ids)}
        elif action=='delete':
            result={'deleted':q.execute("DELETE FROM redeem_codes WHERE id=ANY($1) AND status<>'used'",ids)}
        else:raise BusinessError('不支持的批量操作')
        audit(q,request,'codes.'+action,'batch',f'处理 {len(ids)} 个兑换码')
    return result
