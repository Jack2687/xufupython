"""Role-filtered custom navigation. External pages never receive platform tokens."""
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit
from fastapi import APIRouter, Depends, Request
from .common import config, current, member, require

router = APIRouter(prefix='/api', dependencies=[Depends(member)])
FIELDS = {'id','label','url','icon_svg','visibility','enabled','open_new_tab','hide_open_button'}
SVG_TAGS = set('svg g path rect circle ellipse line polyline polygon defs linearGradient radialGradient stop clipPath mask symbol use title desc'.split())


def valid_icon(value):
    require(isinstance(value,str) and len(value.encode())<=20000, 'SVG 图标不能超过 20 KB')
    if not value: return
    require('<!DOCTYPE' not in value.upper() and '<!ENTITY' not in value.upper(), 'SVG 图标包含不支持的内容')
    try: root=ET.fromstring(value)
    except ET.ParseError: require(False, 'SVG 图标格式无效')
    require(root.tag in ('svg','{http://www.w3.org/2000/svg}svg'), '请上传 SVG 图标')
    for node in root.iter():
        tag=node.tag.removeprefix('{http://www.w3.org/2000/svg}')
        require(tag in SVG_TAGS, 'SVG 图标仅支持静态图形')
        for key,text in node.attrib.items():
            key=key.removeprefix('{http://www.w3.org/1999/xlink}')
            require(not key.lower().startswith('on') and key not in ('style','src') and '{' not in key, 'SVG 图标包含不支持的属性')
            require(key!='href' or re.fullmatch(r'#[\w-]+',text), 'SVG 图标不能引用外部内容')
            require(not re.search(r'javascript:|data:|https?:|//|@import|expression\s*\(',text,re.I), 'SVG 图标不能引用外部内容')
            require('url' not in text.lower() or re.fullmatch(r'url\(#[\w-]+\)',text), 'SVG 图标仅允许引用内部图形')


def validate(value):
    require(set(value)=={'items'} and isinstance(value['items'],list) and len(value['items'])<=30, '最多可设置 30 个自定义菜单')
    ids=set()
    for item in value['items']:
        require(isinstance(item,dict) and set(item)==FIELDS, '自定义菜单字段无效')
        id=item['id']
        require(isinstance(id,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}',id) and id not in ids, '菜单标识无效或重复')
        ids.add(id)
        require(isinstance(item['label'],str) and 1<=len(item['label'].strip())<=60, '菜单名称需为 1～60 字')
        item['label']=item['label'].strip()
        require(item['visibility'] in ('user','admin','all'), '请选择菜单可见角色')
        require(all(type(item[k]) is bool for k in ('enabled','open_new_tab','hide_open_button')), '菜单开关格式无效')
        url=item['url']
        require(isinstance(url,str) and 1<=len(url)<=2048 and not re.search(r'[\s\\\x00-\x1f\x7f]',url), '请输入完整的 HTTP 或 HTTPS 页面 URL')
        try:
            parsed=urlsplit(url)
            valid=parsed.scheme in ('http','https') and bool(parsed.hostname) and not parsed.username and not parsed.password
            if parsed.port is not None: valid=valid and 1<=parsed.port<=65535
        except ValueError: valid=False
        require(valid, '请输入完整的 HTTP 或 HTTPS 页面 URL，不能包含账号密码')
        valid_icon(item['icon_svg'])


def visible(user):
    roles=('user','admin','all') if user.get('role')=='admin' else ('user','all')
    return [{k:v for k,v in item.items() if k not in ('visibility','enabled')} for item in config('custom_menus').get('items',[])
            if item.get('enabled') is True and item.get('visibility') in roles]


@router.get('/custom-pages/{id}')
def page(id:str, request:Request):
    item=next((item for item in visible(current(request)) if item['id']==id),None)
    require(item is not None, '此页面不存在或暂未开放', 404)
    return item
