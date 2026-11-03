"""requests sessions using the same optional local hop as proxy checks."""
from urllib.parse import urlsplit

import httpcore
import requests
from requests.adapters import HTTPAdapter
from requests.utils import select_proxy
from urllib3 import ProxyManager
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import ConnectTimeoutError, NewConnectionError

from .egress import connect_via_local, local_proxy


class _LocalHopConnection:
    def _new_conn(self):
        # urllib3 supplies the remote proxy host here. Its existing CONNECT,
        # proxy authentication and TLS verification then operate on this socket.
        timeout = self.timeout if isinstance(self.timeout, (int, float)) else 15
        try:
            stream = connect_via_local(self._dns_host, self.port, timeout,
                                       socket_options=self.socket_options)
            sock = stream.get_extra_info('socket')
            if sock is None:
                stream.close()
                raise httpcore.ConnectError('Local proxy did not return a socket')
            return sock
        except (httpcore.TimeoutException, TimeoutError) as exc:
            raise ConnectTimeoutError(self, 'Local proxy tunnel timed out') from exc
        except Exception as exc:
            raise NewConnectionError(self, 'Local proxy tunnel failed') from exc


class _HTTPConnection(_LocalHopConnection, HTTPConnection):
    pass


class _HTTPSConnection(_LocalHopConnection, HTTPSConnection):
    pass


class _HTTPPool(HTTPConnectionPool):
    ConnectionCls = _HTTPConnection


class _HTTPSPool(HTTPSConnectionPool):
    ConnectionCls = _HTTPSConnection


class LocalHopAdapter(HTTPAdapter):
    def __init__(self, remote_proxy):
        self.remote_proxy = remote_proxy
        super().__init__(max_retries=0)

    def send(self, request, **kwargs):
        if select_proxy(request.url, kwargs.get('proxies') or {}) != self.remote_proxy:
            raise requests.exceptions.ProxyError('Order proxy must not be bypassed or changed')
        return super().send(request, **kwargs)

    def proxy_manager_for(self, proxy, **proxy_kwargs):
        if proxy != self.remote_proxy or urlsplit(proxy).scheme not in ('http', 'https'):
            raise requests.exceptions.ProxyError('Local chaining requires the configured HTTP/HTTPS proxy')
        if proxy not in self.proxy_manager:
            manager = ProxyManager(proxy, proxy_headers=self.proxy_headers(proxy),
                                   num_pools=self._pool_connections, maxsize=self._pool_maxsize,
                                   block=self._pool_block, **proxy_kwargs)
            # Never mutate urllib3's global pool classes or another session.
            manager.pool_classes_by_scheme = {'http': _HTTPPool, 'https': _HTTPSPool}
            self.proxy_manager[proxy] = manager
        return self.proxy_manager[proxy]


def payment_session(proxy=None):
    session = requests.Session()
    # Explicit order routing wins over ALL_PROXY / HTTP(S)_PROXY / NO_PROXY.
    session.trust_env = False
    if proxy:
        session.proxies.update({'http': proxy, 'https': proxy})
        if local_proxy():
            if urlsplit(proxy).scheme not in ('http', 'https'):
                session.close()
                raise ValueError('本机付款代理链需要远端 HTTP/HTTPS 代理')
            session.mount('http://', LocalHopAdapter(proxy))
            session.mount('https://', LocalHopAdapter(proxy))
    return session
