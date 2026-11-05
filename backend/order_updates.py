"""Commit-driven order notifications. Payloads carry identifiers, never diagnostics."""
import asyncio
import json
import logging
import os
import select
import threading
import psycopg2
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from .common import authenticate, BusinessError, require

router = APIRouter(prefix='/api')
log = logging.getLogger('xufu.updates')


class OrderUpdates:
    def __init__(self):
        self.clients = set()
        self.loop = None
        self.stop = threading.Event()

    def start(self):
        self.loop = asyncio.get_running_loop()
        self.stop.clear()
        self.thread = threading.Thread(target=self.listen, daemon=True, name='order-updates')
        self.thread.start()

    def publish(self, event):
        for queue in tuple(self.clients):
            if queue.full():
                while not queue.empty(): queue.get_nowait()
                queue.put_nowait({'resync': True})
            else: queue.put_nowait(event)

    def listen(self):
        while not self.stop.is_set():
            conn = None
            try:
                conn = psycopg2.connect(os.environ['DATABASE_URL'], connect_timeout=5)
                conn.autocommit = True
                with conn.cursor() as cur: cur.execute('LISTEN xufu_order_updates')
                self.loop.call_soon_threadsafe(self.publish, {'resync': True})
                while not self.stop.is_set():
                    if not select.select([conn], [], [], 1)[0]: continue
                    conn.poll()
                    while conn.notifies:
                        event = json.loads(conn.notifies.pop(0).payload)
                        self.loop.call_soon_threadsafe(self.publish, event)
            except Exception:
                if not self.stop.is_set():
                    log.warning('Order update connection interrupted; reconnecting')
                    self.stop.wait(3)
            finally:
                if conn: conn.close()

    async def close(self):
        self.stop.set()
        await asyncio.to_thread(self.thread.join, 7)


updates = OrderUpdates()


def visible(event, user):
    from .features import capabilities
    if user['role']=='admin': return True
    flags=capabilities(user)
    return flags['orders'] and (event.get('resync') or event.get('user_id') == user['id'])


@router.get('/order-updates')
async def order_updates(request: Request):
    user = await asyncio.to_thread(authenticate, request)
    require(len(updates.clients) < 256, '实时连接繁忙，请稍后重试', 503)
    queue = asyncio.Queue(maxsize=64)
    updates.clients.add(queue)

    async def events():
        try:
            yield 'event: ready\ndata: {}\n\n'
            while not await request.is_disconnected():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    event = None
                # Revalidate on every update/heartbeat; revocation takes effect
                # without waiting for a long-lived stream to reconnect.
                try: active_user = await asyncio.to_thread(authenticate, request, False, False)
                except BusinessError:
                    yield 'event: revoked\ndata: {}\n\n'
                    return
                if event is None:
                    yield ': keepalive\n\n'
                elif visible(event, active_user):
                    data = {'id': event['id']} if event.get('id') else {}
                    yield 'event: order\ndata: ' + json.dumps(data) + '\n\n'
        finally: updates.clients.discard(queue)

    return StreamingResponse(events(), media_type='text/event-stream',
                             headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
