import pytest

from cards import config


@pytest.fixture(autouse=True)
def fresh_settings(monkeypatch):
    """No test reaches a database: the app-wide settings come from the environment and are re-read for each test."""
    for var in ("DATABASE_URL", "PGPASSWORD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(config, "TTL", 0.0)
    config.invalidate()
    yield
    config.invalidate()


@pytest.fixture(scope="session")
def _pgserver(tmp_path_factory):
    """A throwaway Postgres for the tests that need a real one (pip install pgserver). Skipped where it is not installed."""
    pgserver = pytest.importorskip("pgserver")
    srv = pgserver.get_server(str(tmp_path_factory.mktemp("pg")))
    yield srv
    srv.cleanup()


@pytest.fixture
def pg(_pgserver, monkeypatch):
    """An empty, migrated and seeded database of its own for one test: (connection, the catalogue's card ids)."""
    import base64
    import os
    import uuid
    import psycopg
    from cards import cache, crypto, db, seed
    name = "t" + uuid.uuid4().hex[:12]
    base = _pgserver.get_uri()
    with psycopg.connect(base, autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {name}")
    for var in list(os.environ):
        if var.startswith(("CARDS_", "LLM_", "VAULT_")):
            monkeypatch.delenv(var)
    monkeypatch.setenv("DATABASE_URL", base.replace("/postgres?", f"/{name}?"))
    monkeypatch.setenv("CARDS_LOCAL_KEY", base64.b64encode(os.urandom(32)).decode())
    crypto._keys.clear()
    crypto._raw.clear()
    while not db._pool.empty():
        db._close(db._pool.get_nowait()[0])
    cache._cat.update(stamp=None, at=0.0, data=None)
    cache._lines.clear()
    cache._memo.clear()
    db.migrate()
    with db.connect() as conn:
        seed.seed(conn)
        ids = [r["id"] for r in conn.execute("SELECT id FROM cards ORDER BY id").fetchall()]
        conn.execute("INSERT INTO people (id, display, held_cards) VALUES ('alice', 'Alice', %s)", (ids[:4],))
        conn.commit()
        yield conn, ids
    while not db._pool.empty():
        db._close(db._pool.get_nowait()[0])
    crypto._keys.clear()
    crypto._raw.clear()
