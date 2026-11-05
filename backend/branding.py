"""Versioned site icons stored with site settings; defaults ship with the app."""
import base64
import binascii
import hashlib
import re
import struct
from pathlib import Path
from fastapi import APIRouter
from fastapi.responses import Response
from .common import config, require
from .custom_menus import valid_icon

router=APIRouter()
DEFAULT_ICON=(Path(__file__).parent/'assets/xupay-icon.svg').read_bytes()
MAX_BYTES=256*1024


def decode_icon(value):
    require(isinstance(value,str) and len(value)<=MAX_BYTES*4//3+100, '图标不能超过 256 KB')
    if not value: return DEFAULT_ICON,'image/svg+xml'
    match=re.fullmatch(r'data:(image/(?:png|svg\+xml));base64,([A-Za-z0-9+/=]+)',value)
    require(match is not None, '请上传 PNG 或 SVG 图标')
    try: data=base64.b64decode(match[2],validate=True)
    except (ValueError,binascii.Error): require(False,'图标文件格式无效')
    require(0<len(data)<=MAX_BYTES,'图标不能超过 256 KB')
    if match[1]=='image/svg+xml':
        try: text=data.decode('utf-8-sig')
        except UnicodeDecodeError: require(False,'SVG 图标需要 UTF-8 编码')
        valid_icon(text)
        data=text.encode()
    else:
        require(data.startswith(b'\x89PNG\r\n\x1a\n') and len(data)>=57,'PNG 图标格式无效')
        offset=8;chunks=[]
        while offset+12<=len(data):
            size=struct.unpack('>I',data[offset:offset+4])[0]
            require(offset+12+size<=len(data),'PNG 图标文件不完整')
            kind=data[offset+4:offset+8];payload=data[offset+8:offset+8+size]
            checksum=struct.unpack('>I',data[offset+8+size:offset+12+size])[0]
            require(binascii.crc32(kind+payload)&0xffffffff==checksum,'PNG 图标文件损坏')
            if not chunks:
                require(kind==b'IHDR' and size==13,'PNG 图标格式无效')
                width,height=struct.unpack('>II',payload[:8])
                require(16<=width<=1024 and 16<=height<=1024,'PNG 图标尺寸需为 16～1024 像素')
            chunks.append(kind);offset+=size+12
            if kind==b'IEND':
                require(size==0 and offset==len(data),'PNG 图标格式无效')
                break
        require(chunks[-1]==b'IEND' and b'IDAT' in chunks,'PNG 图标文件不完整')
    return data,match[1]


def icon_url(site=None):
    data,_=decode_icon((site if site is not None else config('site')).get('icon',''))
    return '/site-icon?v='+hashlib.sha256(data).hexdigest()[:16]


def public_site():
    site=config('site')
    return {**{k:v for k,v in site.items() if k!='icon'},'icon_url':icon_url(site)}


@router.api_route('/site-icon',methods=['GET','HEAD'])
@router.api_route('/favicon.ico',methods=['GET','HEAD'],include_in_schema=False)
def site_icon():
    data,mime=decode_icon(config('site').get('icon',''))
    return Response(data,media_type=mime)


@router.api_route('/site-icon/default',methods=['GET','HEAD'])
def default_icon():
    return Response(DEFAULT_ICON,media_type='image/svg+xml')
