"""Optional local proxy hop for connecting to configured public proxy servers."""
import ipaddress
import os
import time
from urllib.parse import urlsplit

import httpcore
import httpx


def local_proxy(name='LOCAL_EGRESS_PROXY'):
    value=os.getenv(name,'').strip()
    if not value:
        return ''
    parsed=urlsplit(value)
    schemes=('http',) if name=='LOCAL_EGRESS_PROXY' else ('socks5','socks5h')
    if (parsed.scheme not in schemes or not parsed.port or parsed.username or parsed.password
            or parsed.path not in ('','/') or parsed.query or parsed.fragment
            or not ipaddress.ip_address(parsed.hostname).is_loopback):
        raise ValueError('本机上游代理必须是有效的回环地址和端口')
    return value


def resolve_public_proxy(host,port,upstream,timeout):
    try:
        addresses=[str(ipaddress.ip_address(host))]
    except ValueError:
        with httpx.Client(proxy=upstream,trust_env=False,timeout=timeout,follow_redirects=False) as client:
            response=client.get('https://dns.google/resolve',params={'name':host,'type':'A'})
            response.raise_for_status()
            if len(response.content)>32768:
                raise httpcore.ConnectError('代理 DNS 响应过大')
            data=response.json()
            addresses=[a['data'] for a in data.get('Answer',[]) if a.get('type')==1]
    if not addresses or not all(ipaddress.ip_address(a).is_global for a in addresses):
        raise httpcore.ConnectError('代理需解析为公网地址')
    return addresses[0]


def connect_via_local(host,port,timeout,local_address=None,socket_options=None):
    upstream=local_proxy()
    deadline=time.monotonic()+(timeout if timeout is not None else 15)
    def remaining():
        value=deadline-time.monotonic()
        if value<=0:
            raise httpcore.ConnectTimeout('连接本机上游代理超时')
        return value
    address=resolve_public_proxy(host,port,upstream,remaining())
    local=urlsplit(upstream)
    stream=httpcore.SyncBackend().connect_tcp(local.hostname,local.port,remaining(),local_address,socket_options)
    try:
        authority=f'[{address}]:{port}' if ':' in address else f'{address}:{port}'
        stream.write(f'CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n'.encode('ascii'),remaining())
        headers=bytearray()
        while not headers.endswith(b'\r\n\r\n'):
            chunk=stream.read(1,remaining())
            if not chunk or len(headers)>=16384:
                raise httpcore.ConnectError('本机代理隧道响应不完整')
            headers.extend(chunk)
        status=bytes(headers).split(b'\r\n',1)[0].split()
        if len(status)<2 or status[1]!=b'200':
            raise httpcore.ConnectError('本机代理拒绝连接配置的代理服务器')
        return stream
    except BaseException:
        stream.close()
        raise


def curl_preproxy_args(proxy_type):
    if not local_proxy():
        return []
    upstream=local_proxy('LOCAL_EGRESS_SOCKS_PROXY')
    if not upstream:
        raise ValueError('提链需要配置本机代理的 SOCKS5 入口')
    if proxy_type!='http':
        raise ValueError('提链的本机代理链目前需要远端 HTTP/HTTPS 代理')
    return ['--preproxy',upstream]
