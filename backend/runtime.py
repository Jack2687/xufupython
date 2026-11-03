"""Explicit background task availability, shared by admission and scheduling."""
import os


def worker_mode():
    if os.getenv('APP_TEST_MODE') == '1':
        return 'off'
    mode = os.getenv('APP_WORKER_MODE', 'links')
    return mode if mode in ('off', 'links', 'all') else 'off'


def order_workflows():
    return {'off': (), 'links': ('link',), 'all': ('link', 'subscription')}[worker_mode()]


def queue_message(workflow):
    if worker_mode() == 'off':
        return '后台任务未启动，当前请求不会执行，请联系管理员。'
    if workflow not in order_workflows():
        return '当前仅开放生成结账链接，自动付款尚未启用。'
    return ''
