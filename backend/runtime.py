"""Admission and scheduling follow the admin payment toggle."""

from . import db


def worker_mode():
    try:
        cfg = db.scalar("SELECT value FROM settings WHERE key='payment'", default={}) or {}
    except Exception:
        return 'off'
    return 'all' if cfg.get('enabled') is True else 'off'


def order_workflows():
    return {'off': (), 'all': ('link', 'subscription')}[worker_mode()]


def queue_message(workflow):
    if worker_mode() == 'off':
        return '站内付款尚未开启，请在后台开启“开放站内付款”后再试。'
    if workflow not in order_workflows():
        return '当前工作流未启用，请联系管理员。'
    return ''
