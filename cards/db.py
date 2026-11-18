"""Postgres access: a small connection pool (a TLS connection per request was the slowest part of every page), autocommit
off, dict rows. `with connect() as conn` commits on a clean exit and rolls back on an error, like psycopg's own."""
import os
import queue
import time
from contextlib import contextmanager
from importlib import resources

import psycopg
from psycopg.rows import dict_row

POOL_SIZE = int(os.environ.get("PGPOOL", "6"))
FRESH = 30   # seconds: a connection back in the pool for less than this is used without the SELECT 1 check
_pool = queue.LifoQueue()   # (connection, when it was put back)


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
            c, back = _pool.get_nowait()
        except queue.Empty:
            conn = _open()
            break
        # clicking through the pages reuses a connection every few seconds: checking it each time cost a round trip
        # per page. One that sat longer may have died with a database restart or failover, so that one is checked.
        if not c.closed and (time.monotonic() - back < FRESH or _alive(c)):
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
            _pool.put((conn, time.monotonic()))


def _close(conn):
    try:
        conn.close()
    except Exception:
        pass


STARTUP_LOCK = 0x5C4A4D53   # advisory lock key: one process at a time migrates and seeds


@contextmanager
def startup_lock(conn):
    """Hold the start-up lock: with two copies of the app starting at once, the second waits for the first to finish
    the schema and the seed instead of running the same statements beside it. A session lock, so it survives the
    commits made inside; it ends with the block (or with the connection)."""
    conn.execute("SELECT pg_advisory_lock(%s)", (STARTUP_LOCK,))
    conn.commit()
    ok = False
    try:
        yield conn
        ok = True
    finally:
        try:
            conn.commit() if ok else conn.rollback()   # what the block did is kept (or dropped) before letting go
            conn.execute("SELECT pg_advisory_unlock(%s)", (STARTUP_LOCK,))
            conn.commit()
        except Exception:
            _close(conn)   # a closed session gives the lock up too


def migrate():
    sql = resources.files("cards").joinpath("schema.sql").read_text()
    with connect() as conn, startup_lock(conn):
        conn.execute(sql)
        conn.commit()
        return conn.execute("SELECT max(version) AS v FROM schema_version").fetchone()["v"]
