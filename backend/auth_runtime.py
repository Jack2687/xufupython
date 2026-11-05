"""Bound authentication CPU/database work without blocking the ASGI loop."""
from functools import partial
import anyio
from psycopg2.errors import DeadlockDetected, LockNotAvailable, QueryCanceled
from psycopg2.pool import PoolError
from . import db
from .common import BusinessError

_limiter = anyio.CapacityLimiter(8)


def bounded_call(function, *args):
    try:
        with db.request_timeouts():
            return function(*args)
    except (DeadlockDetected, LockNotAvailable, QueryCanceled, PoolError):
        raise BusinessError('认证服务繁忙，请稍后重试', 503) from None


async def run_auth(function, *args):
    return await anyio.to_thread.run_sync(partial(bounded_call, function, *args), limiter=_limiter)
