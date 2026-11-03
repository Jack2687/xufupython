import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import logging
import os
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit
from fastapi import FastAPI,Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse
from . import db,auth,membership,wallet,admin_accounts,settings,proxies,records,admin_content,renewals,subscription
from .common import *
from .seed import seed
from .runtime import worker_mode
from .request_origin import configured_origin, origin_allowed

PUBLIC_ORIGIN = configured_origin(os.getenv('PUBLIC_ORIGIN', ''))

log=logging.getLogger('xufu')
WEB=db.ROOT/'webui'
HEADERS={
 'X-Content-Type-Options':'nosniff','X-Frame-Options':'DENY','Referrer-Policy':'no-referrer',
 'Permissions-Policy':'camera=(), microphone=(), geolocation=()',
 'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
 'Cache-Control':'no-store'
}


def maintenance():
    with db.transaction() as q:
        for table in ('sessions','rate_buckets','blocked_ips','proxy_leases'):
            q.execute('DELETE FROM '+table+' WHERE expires_at<now()')
        q.execute("DELETE FROM risk_events WHERE resolved AND created_at<now()-$1*interval '1 day'",config('risk',q)['event_retention_days'])
        q.execute("UPDATE renewal_tasks SET credentials_enc='' WHERE credentials_expires_at<now() AND credentials_enc<>''")


def worker_loop(stop):
    executor=ThreadPoolExecutor(max_workers=22,thread_name_prefix='xufu-worker')
    proxy_jobs=set();renewal_jobs=set();subscription_jobs=set();last_maintenance=0
    try:
        while not stop.wait(1):
            for group in (proxy_jobs,renewal_jobs,subscription_jobs):
                for future in list(group):
                    if future.done():
                        try:future.result()
                        except Exception as e:log.error('Background operation failed (%s)',type(e).__name__)
                        group.remove(future)
            try:
                if time.monotonic()-last_maintenance>60:maintenance();last_maintenance=time.monotonic()
                if len(proxy_jobs)<2:proxy_jobs.add(executor.submit(proxies.run_check))
                if worker_mode()=='all' and len(renewal_jobs)<20:
                    job=renewals.claim()
                    if job:renewal_jobs.add(executor.submit(renewals.run_task,job))
                if len(subscription_jobs)<4:
                    job=subscription.claim_subscription()
                    if job:subscription_jobs.add(executor.submit(subscription.run_subscription,job))
            except Exception as e:log.error('Worker scheduling failed (%s)',type(e).__name__)
    finally:executor.shutdown(wait=True,cancel_futures=True)


@asynccontextmanager
async def lifespan(app):
    db.initialize();seed();stop=threading.Event();thread=None
    if worker_mode()!='off':
        thread=threading.Thread(target=worker_loop,args=(stop,),daemon=True,name='xufu-scheduler');thread.start()
    yield
    stop.set()
    if thread:await asyncio.to_thread(thread.join,115)
    db.close()


app=FastAPI(title='XUFU Python API',version='1.0.0',docs_url=None,redoc_url=None,openapi_url=None,lifespan=lifespan)


@app.exception_handler(BusinessError)
async def business_error(request,error):return response({'error':error.message},error.status)


@app.exception_handler(RequestValidationError)
async def validation_error(request,error):return response({'error':'请求参数格式不正确'},400)


@app.exception_handler(Exception)
async def unexpected_error(request,error):
    log.error('Request failed (%s)',type(error).__name__)
    return response({'error':'操作未完成，请稍后重试'},500)


@app.middleware('http')
async def security(request:Request,call_next):
    try:
        if request.method not in ('GET','HEAD','OPTIONS'):
            require(request.headers.get('sec-fetch-site')!='cross-site','请求来源不受信任',403)
            require(origin_allowed(request, PUBLIC_ORIGIN),'请求来源不受信任',403)
        size=request.headers.get('content-length')
        require(not size or size.isdigit() and int(size)<=1048576,'请求内容过大',413)
        if not request.url.path.startswith('/health/'):
            require(not db.scalar('SELECT ip FROM blocked_ips WHERE ip=$1 AND expires_at>now()',ip(request)),'当前来源暂不可访问',403)
            require(limit('edge:'+ip(request),600),'请求过于频繁',429)
        result=await call_next(request)
    except BusinessError as e:result=response({'error':e.message},e.status)
    except Exception as e:
        log.error('Request failed (%s)',type(e).__name__);result=response({'error':'操作未完成，请稍后重试'},500)
    for k,v in HEADERS.items():result.headers[k]=v
    return result


for module in (auth,membership,wallet,admin_accounts,settings,proxies,admin_content,renewals,subscription):app.include_router(module.router)
for router in (records.public_router,records.router,records.admin_router):app.include_router(router)


@app.get('/health/live')
def live():return {'ok':True}


@app.get('/health/ready')
def ready():
    require(db.scalar('SELECT 1')==1,'数据库暂不可用',503)
    return {'ok':True}


@app.get('/api/openapi.json')
def openapi_document():
    return FileResponse(db.ROOT/'backend/openapi.json',media_type='application/json')


@app.api_route('/{path:path}',methods=['GET','HEAD','POST','PUT','PATCH','DELETE','OPTIONS'],include_in_schema=False)
def static(path:str,request:Request):
    if path=='api' or path.startswith('api/'):return response({'error':'接口不存在'},404)
    if request.method not in ('GET','HEAD'):return response({'error':'请求方法不支持'},405)
    target=(WEB/path).resolve()
    require(target.is_relative_to(WEB.resolve()),'文件不存在',404)
    if target.is_file():
        if target.name.startswith('Admin-') and target.suffix=='.js':authenticate(request,True)
        return FileResponse(target)
    if '.' in Path(path).name:return response({'error':'文件不存在'},404)
    return FileResponse(WEB/'index.html')
