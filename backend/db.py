"""PostgreSQL transactions and parameterized SQL; no business workers here."""
import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor, Json
from psycopg2.pool import ThreadedConnectionPool

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / '.env', override=False)
_pool = None
_request_timeouts = ContextVar('request_database_timeouts', default=False)


@contextmanager
def request_timeouts():
    token = _request_timeouts.set(True)
    try:
        yield
    finally:
        _request_timeouts.reset(token)


def pool():
    global _pool
    if _pool is None:
        _pool = ThreadedConnectionPool(1, 24, os.environ['DATABASE_URL'], connect_timeout=5)
    return _pool


def sql(statement, args):
    bound = []
    def replace(match):
        value = args[int(match[1])-1]
        bound.append(Json(value) if isinstance(value, dict) else value)
        return '%s'
    statement = statement.replace('%', '%%')
    statement = re.sub(r'\$(\d+)', replace, statement)
    return statement, bound


class Query:
    def __init__(self, cursor):
        self.cursor = cursor

    def execute(self, statement, *args):
        statement, params = sql(statement, args)
        self.cursor.execute(statement, params)
        return self.cursor.rowcount

    def all(self, statement, *args):
        self.execute(statement, *args)
        return [dict(row) for row in self.cursor.fetchall()]

    def one(self, statement, *args):
        self.execute(statement, *args)
        row = self.cursor.fetchone()
        return dict(row) if row else None

    def scalar(self, statement, *args, default=None):
        row = self.one(statement, *args)
        return next(iter(row.values())) if row else default


@contextmanager
def transaction():
    conn = pool().getconn()
    try:
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                if _request_timeouts.get():
                    cursor.execute("SET LOCAL lock_timeout='3s'; SET LOCAL statement_timeout='10s'")
                yield Query(cursor)
    finally:
        pool().putconn(conn)


def all(statement, *args):
    with transaction() as q:
        return q.all(statement, *args)


def one(statement, *args):
    with transaction() as q:
        return q.one(statement, *args)


def scalar(statement, *args, default=None):
    with transaction() as q:
        return q.scalar(statement, *args, default=default)


def execute(statement, *args):
    with transaction() as q:
        return q.execute(statement, *args)


def initialize():
    with transaction() as q:
        q.cursor.execute((ROOT/'backend/schema.sql').read_text(encoding='utf-8'))


def close():
    global _pool
    if _pool:
        _pool.closeall()
        _pool = None
