"""Wait from durable payment confirmation before starting the Go follow-up."""
from datetime import timedelta
import time

from . import payment_trace as trace
from .common import now

DEFAULT_SECONDS = 5
MAX_SECONDS = 300


def wait_before_refresh(order_id, paid_at, seconds, proxy_expires_at):
    """Return False if the order's original proxy expires before the next step."""
    ready_at = paid_at + timedelta(seconds=seconds)
    current = now()
    remaining = max(0, (ready_at - current).total_seconds())
    if proxy_expires_at <= max(ready_at, current):
        return False
    if remaining == 0:
        return True
    diagnostics = {'post_payment_delay_seconds': seconds,
                   'post_payment_wait_ms': round(remaining * 1000)}
    trace.record('session_refresh', 'waiting', 'post_payment_wait',
                 order_id=order_id, diagnostics=diagnostics)
    started = time.monotonic()
    time.sleep(remaining)
    if proxy_expires_at <= now():
        return False
    trace.record('session_refresh', 'completed', 'post_payment_wait_completed',
                 order_id=order_id, elapsed_ms=round((time.monotonic()-started)*1000),
                 diagnostics=diagnostics)
    return True
