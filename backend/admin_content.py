import csv
import io
import ipaddress
from datetime import date,timedelta,timezone,datetime
from fastapi import APIRouter,Depends,Request,Query
from fastapi.responses import Response
from . import db
from .common import *

router=APIRouter(prefix='/api/admin',dependencies=[Depends(administrator)])


@router.get('/products')
def products(): return {'items':db.all('SELECT * FROM products ORDER BY sort')}


@router.put('/products/{id}')
async def product(id:str,request:Request):
    v=await body(request)
    require(1<=length(v.get('name'))<=80 and 0<=length(v.get('description',''))<=300 and type(v.get('enabled')) is bool and 0<=length(v.get('tag',''))<=200,'套餐参数无效')
    integer(v.get('fee'),0,100000000);integer(v.get('sort',0),-2147483648,2147483647)
    features=v.get('features',[]);require(isinstance(features,list) and len(features)<=10 and all(0<=length(f)<=500 for f in features),'套餐特性无效')
    with db.transaction() as q:
        require(q.execute('UPDATE products SET name=$2,description=$3,fee=$4,enabled=$5,sort=$6,tag=$7,features=$8::jsonb WHERE id=$1',id,v['name'],v.get('description',''),v['fee'],v['enabled'],v.get('sort',0),v.get('tag',''),json.dumps(features))==1,'套餐不存在',404)
        audit(q,request,'product.updated',id,'调整套餐配置')
    return {'ok':True}


@router.get('/announcements')
def announcements(): return {'items':db.all('SELECT * FROM announcements ORDER BY sort,created_at DESC')}


@router.post('/announcements')
@router.put('/announcements/{id}')
async def announcement(request:Request,id:str=''):
    v=await body(request);id=id or uid();starts=parsedate(v.get('starts_at'));ends=parsedate(v.get('ends_at'))
    require(1<=length(v.get('title'))<=150 and 1<=length(v.get('body'))<=5000 and v.get('tone') in ('info','warning','success') and (not starts or not ends or ends>starts),'公告内容或时间无效')
    require(type(v.get('published')) is bool and type(v.get('login_visible')) is bool,'公告开关无效');integer(v.get('sort',0),-2147483648,2147483647)
    with db.transaction() as q:
        q.execute('INSERT INTO announcements(id,title,body,tone,published,login_visible,sort,starts_at,ends_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9) ON CONFLICT(id) DO UPDATE SET title=$2,body=$3,tone=$4,published=$5,login_visible=$6,sort=$7,starts_at=$8,ends_at=$9',id,v['title'],v['body'],v['tone'],v['published'],v['login_visible'],v.get('sort',0),starts,ends)
        audit(q,request,'announcement.saved',id,'保存公告')
    return {'id':id}


@router.delete('/announcements/{id}')
def delete_announcement(id:str,request:Request):
    with db.transaction() as q:
        require(q.execute('DELETE FROM announcements WHERE id=$1',id)==1,'公告不存在',404)
        audit(q,request,'announcement.deleted',id,'删除公告')
    return {'ok':True}


@router.get('/risk')
def risk(severity:str='',q:str='',category:str='',state:str='',start:str='',end:str='',page:int=Query(1,ge=1,le=100000),page_size:int=Query(50,ge=1,le=100)):
    require(severity in ('','high','medium','low') and state in ('','pending','resolved') and len(q)<=200 and len(category)<=80,'筛选参数无效')
    today=datetime.now(timezone(timedelta(hours=8))).date()
    try:
        first=date.fromisoformat(start) if start else today-timedelta(days=30)
        last=date.fromisoformat(end) if end else today
    except ValueError: raise BusinessError('日期格式不正确')
    require(0<=(last-first).days<=3660,'日期范围无效')
    where=" FROM risk_events e LEFT JOIN users u ON u.id=e.user_id WHERE ($1='' OR e.severity=$1) AND ($2='' OR e.ip ILIKE '%'||$2||'%' OR u.email ILIKE '%'||$2||'%' OR u.name ILIKE '%'||$2||'%' OR e.detail ILIKE '%'||$2||'%') AND ($3='' OR e.category=$3) AND ($4='' OR e.resolved=($4='resolved')) AND COALESCE(e.last_seen,e.created_at)>=($5::date AT TIME ZONE 'Asia/Shanghai') AND COALESCE(e.last_seen,e.created_at)<(($6::date+1) AT TIME ZONE 'Asia/Shanghai')"
    args=(severity,q,category,state,first,last)
    totals=db.one("SELECT count(*) AS total,count(*) FILTER(WHERE NOT e.resolved) AS pending,count(*) FILTER(WHERE e.severity='high' AND NOT e.resolved) AS high,COALESCE(sum(e.occurrences),0) AS occurrences"+where,*args)
    rows=db.all('SELECT e.*,u.email,u.name AS user_name,COALESCE(e.last_seen,e.created_at) AS last_seen'+where+' ORDER BY COALESCE(e.last_seen,e.created_at) DESC,e.id DESC LIMIT $7 OFFSET $8',*args,page_size,(page-1)*page_size)
    blocks=db.all('SELECT * FROM blocked_ips WHERE expires_at>now() ORDER BY created_at DESC')
    return {'items':rows,'total':totals['total'],'page':page,'page_size':page_size,'summary':dict(totals,blocks=len(blocks)),
            'blocks':blocks,'config':config('risk'),'categories':[r['category'] for r in db.all('SELECT DISTINCT category FROM risk_events ORDER BY category')],
            'generated_at':now(),'start':first,'end':last}


@router.post('/risk/{id}/resolve')
def resolve(id:int,request:Request):
    with db.transaction() as q:
        require(q.execute('UPDATE risk_events SET resolved=true WHERE id=$1',id)==1,'记录不存在',404)
        audit(q,request,'risk.resolved',str(id),'标记已核查')
    return {'ok':True}


@router.post('/blocks')
async def block(request:Request):
    v=await body(request)
    try: address=str(ipaddress.ip_address(v.get('ip','')))
    except ValueError: raise BusinessError('IP 地址无效')
    integer(v.get('minutes'),1,10080);require(address!=ip(request) and 2<=length(v.get('reason'))<=300,'IP/原因无效，不能限制当前来源')
    with db.transaction() as q:
        q.execute("INSERT INTO blocked_ips(ip,reason,expires_at) VALUES($1,$2,now()+$3*interval '1 minute') ON CONFLICT(ip) DO UPDATE SET reason=$2,expires_at=now()+$3*interval '1 minute'",address,v['reason'],v['minutes'])
        audit(q,request,'risk.blocked',address,v['reason'])
    return {'ok':True}


@router.delete('/blocks/{address}')
def unblock(address:str,request:Request):
    with db.transaction() as q:
        q.execute('DELETE FROM blocked_ips WHERE ip=$1',address);audit(q,request,'risk.unblocked',address,'解除来源限制')
    return {'ok':True}


@router.get('/audit')
def audits():return {'items':db.all('SELECT a.*,u.email AS actor FROM audit_events a LEFT JOIN users u ON a.actor_id=u.id ORDER BY a.id DESC LIMIT 300')}


TOTALS="COALESCE(sum(l.amount) FILTER(WHERE l.kind IN ('topup','redeem')),0) AS credited,COALESCE(sum(-l.amount) FILTER(WHERE l.kind='charge'),0) AS spent,COALESCE(sum(l.amount) FILTER(WHERE l.kind='adjustment'),0) AS adjusted"
TIME="l.created_at>=($1::date AT TIME ZONE 'Asia/Shanghai') AND l.created_at<(($2::date+1) AT TIME ZONE 'Asia/Shanghai')"


def finance_data(request):
    args=request.query_params;today=datetime.now(timezone(timedelta(hours=8))).date()
    start=args.get('start') or (today-timedelta(days=30)).isoformat();end=args.get('end') or today.isoformat()
    try: s=date.fromisoformat(start);e=date.fromisoformat(end)
    except ValueError: raise BusinessError('统计日期格式无效')
    require(e>=s and (e-s).days<=3660,'统计日期范围无效')
    period=args.get('period','day');period=period if period in ('day','week','month','year') else 'day';exclude=args.get('include_admin')!='true'
    where=' WHERE '+TIME+" AND (NOT $3::bool OR u.role!='admin')"
    with db.transaction() as q:
        q.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
        totals=q.one('SELECT '+TOTALS+',count(*) AS entries FROM ledger l JOIN users u ON u.id=l.user_id'+where,start,end,exclude)
        trend=q.all("SELECT to_char(date_trunc($4,l.created_at AT TIME ZONE 'Asia/Shanghai'),'YYYY-MM-DD') AS period,"+TOTALS+' FROM ledger l JOIN users u ON u.id=l.user_id'+where+' GROUP BY 1 ORDER BY 1',start,end,exclude,period)
        users=q.all("SELECT u.id,u.email,u.name,u.role,u.balance,u.frozen,u.fee_override,COALESCE(x.credited,0) AS credited,COALESCE(x.spent,0) AS spent,COALESCE(x.adjusted,0) AS adjusted,(SELECT count(*) FROM orders o WHERE o.user_id=u.id AND o.created_at>=($1::date AT TIME ZONE 'Asia/Shanghai') AND o.created_at<(($2::date+1) AT TIME ZONE 'Asia/Shanghai')) AS orders FROM users u LEFT JOIN (SELECT l.user_id,"+TOTALS+' FROM ledger l WHERE '+TIME+" GROUP BY l.user_id) x ON x.user_id=u.id WHERE (NOT $3::bool OR u.role!='admin') ORDER BY credited DESC,u.email",start,end,exclude)
        current=q.one("SELECT COALESCE(sum(balance),0) AS balance,COALESCE(sum(frozen),0) AS frozen FROM users WHERE (NOT $1::bool OR role!='admin')",exclude)
    return {'totals':totals,'current':current,'trend':trend,'users':users,'period':period,'start':start,'end':end,'timezone':'Asia/Shanghai'}


@router.get('/finance')
def finance(request:Request): return finance_data(request)


@router.get('/finance/export')
def export_finance(request:Request):
    data=finance_data(request);out=io.StringIO(newline='');writer=csv.writer(out)
    writer.writerow(['账户','称呼','充值分','使用分','调整分','当前可用分','当前冻结分','订单数'])
    for u in data['users']:
        row=[]
        for k in ('email','name','credited','spent','adjusted','balance','frozen','orders'):
            value=str(u[k]);row.append("'"+value if value.startswith(('=','+','-','@','\t','\r')) else value)
        writer.writerow(row)
    return Response(content=b'\xef\xbb\xbf'+out.getvalue().encode('utf-8'),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':'attachment; filename=xupay-finance.csv'})
