"""Postgres access: a small connection pool (a TLS connection per request was the slowest part of every page), autocommit
off, dict rows. `with connect() as conn` commits on a clean exit and rolls back on an error, like psycopg's own."""
import os
import queue
from contextlib import contextmanager
from importlib import resources

import psycopg
from psycopg.rows import dict_row

POOL_SIZE = int(os.environ.get("PGPOOL", "6"))
_pool = queue.LifoQueue()


def dsn():
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    return (f"host={os.environ.get('PGHOST', 'localhost')} port=5432 "
            f"dbname={os.environ.get('PGDATABASE', 'cards')} user={os.environ.get('PGUSER', 'cards')} "
            f"password={os.environ['PGPASSWORD']} sslmode={os.environ.get('PGSSLMODE', 'require')}")


def _open():
    return psycopg.connect(dsn(), row_factory=dict_row, connect_timeout=10)


def _alive(conn):
    """A pooled connection can die with a database failover or restart: one cheap round trip tells."""
    try:
        conn.execute("SELECT 1")
        conn.rollback()
        return True
    except Exception:
        return False


@contextmanager
def connect():
    conn = None
    while conn is None:
        try:
            c = _pool.get_nowait()
        except queue.Empty:
            conn = _open()
            break
        if not c.closed and _alive(c):
            conn = c
        else:
            _close(c)
    try:
        yield conn
        conn.commit()
    except BaseException:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        if conn.closed or conn.broken or _pool.qsize() >= POOL_SIZE:
            _close(conn)
        else:
            _pool.put(conn)


def _close(conn):
    try:
        conn.close()
    except Exception:
        pass


def migrate():
    sql = resources.files("cards").joinpath("schema.sql").read_text()
    with connect() as conn:
        conn.execute(sql)
        conn.commit()
        return conn.execute("SELECT max(version) AS v FROM schema_version").fetchone()["v"]
