"""Managed subscription regions, with immutable per-order currency snapshots."""
import re
from fastapi import APIRouter, Depends, Request
from . import db
from .common import administrator,audit,body,config,integer,require

DEFAULT_REGIONS = [
    {'code':code,'name':name,'currency':currency,'enabled':True,'sort':index}
    for index,(code,name,currency) in enumerate([
        ('PH','菲律宾','PHP'),('US','美国','USD'),('JP','日本','JPY'),('SG','新加坡','SGD'),
        ('EG','埃及','EGP'),('TR','土耳其','TRY'),('GB','英国','GBP')])
]
LEGACY_CODES = frozenset(r['code'] for r in DEFAULT_REGIONS)
router = APIRouter(prefix='/api/admin', dependencies=[Depends(administrator)])


def items(q=None):
    return sorted(config('regions',q).get('items',DEFAULT_REGIONS),key=lambda r:(r['sort'],r['code']))


def enabled(flow=None,q=None):
    if flow is None:flow=config('payment',q).get('subscription_flow')
    rows=[{k:r[k] for k in ('code','name','currency')} for r in items(q)
          if r['enabled'] and (flow=='cslive_go_direct_pay' or r['code'] in LEGACY_CODES)]
    if flow!='cslive_go_direct_pay':
        original={r['code']:r['currency'] for r in DEFAULT_REGIONS}
        for row in rows:row['currency']=original[row['code']]
    return rows


def selected(code,flow=None,q=None):
    row=next((r for r in enabled(flow,q) if r['code']==code),None)
    require(row is not None,'订阅地区不存在、已停用或当前流程不支持，请重新选择',400)
    return dict(row)


def default_country(flow=None,q=None):
    choices=enabled(flow,q);preferred=config('scheduler',q).get('default_country','PH')
    return preferred if any(r['code']==preferred for r in choices) else (choices[0]['code'] if choices else '')


def order_region(country,snapshot):
    if snapshot:
        require(snapshot.get('code')==country and re.fullmatch('[A-Z]{3}',snapshot.get('currency','')),'订单地区快照无效',500)
        return snapshot
    row=next((r for r in DEFAULT_REGIONS if r['code']==country),None)
    require(row is not None,'订单缺少地区快照',500)
    return row


def validate(value):
    require(isinstance(value,dict),'地区参数无效')
    code=value.get('code');name=value.get('name');currency=value.get('currency')
    require(isinstance(code,str) and re.fullmatch('[A-Z]{2}',code),'地区代码须为两位大写国家代码')
    require(isinstance(name,str) and 1<=len(name.strip())<=60,'请填写地区名称')
    require(isinstance(currency,str) and re.fullmatch('[A-Z]{3}',currency),'币种须为三位大写代码')
    require(type(value.get('enabled')) is bool,'地区状态无效')
    integer(value.get('sort',0),-10000,10000,'排序超出范围')
    return dict(code=code,name=name.strip(),currency=currency,enabled=value['enabled'],sort=value.get('sort',0))


@router.get('/regions')
def listing():return {'items':items()}


@router.post('/regions')
@router.put('/regions/{code}')
async def save(request:Request,code:str=''):
    row=validate(await body(request))
    require(not code or row['code']==code,'地区代码创建后不能修改，请新增地区')
    with db.transaction() as q:
        q.execute("SELECT value FROM settings WHERE key='regions' FOR UPDATE")
        rows=items(q);exists=any(r['code']==row['code'] for r in rows)
        require(exists if code else not exists,'地区不存在' if code else '地区代码已存在',404 if code else 409)
        require(bool(code) or len(rows)<250,'地区数量已达上限')
        rows=[row if r['code']==code else r for r in rows] if code else rows+[row]
        q.execute("UPDATE settings SET value=$1 WHERE key='regions'",{'items':rows})
        audit(q,request,'region.updated' if code else 'region.created',row['code'],'保存订阅地区')
    return {'code':row['code']}


@router.delete('/regions/{code}')
def delete(code:str,request:Request):
    with db.transaction() as q:
        q.execute("SELECT value FROM settings WHERE key='regions' FOR UPDATE")
        rows=items(q);remaining=[r for r in rows if r['code']!=code]
        require(len(remaining)!=len(rows),'地区不存在',404)
        q.execute("UPDATE settings SET value=$1 WHERE key='regions'",{'items':remaining})
        audit(q,request,'region.deleted',code,'删除订阅地区；保留原订单和代理资源')
    return {'ok':True}
