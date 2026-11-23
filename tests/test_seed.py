"""`seed` runs at every start: a database seeded by an older version keeps one row per offer, with today's wording."""
from cards import seed


def test_seeding_again_over_an_older_versions_offers(pg):
    conn, _ = pg
    n = conn.execute("SELECT count(*) n FROM signup_offers").fetchone()["n"]
    assert n == len(seed.data("recommended-cards.json")["cards"])
    # the row as 0.10.1 seeded it: the same card, bonus and end date, other wording
    conn.execute("UPDATE signup_offers SET eligibility = 'yes (old wording)', cardup = 'UNCLEAR: old wording', cents_per_mile = 1.61 "
                 "WHERE card_name = 'Citi Prestige'")
    conn.commit()
    seed.seed(conn)
    seed.seed(conn)
    assert conn.execute("SELECT count(*) n FROM signup_offers").fetchone()["n"] == n
    row = conn.execute("SELECT * FROM signup_offers WHERE card_name = 'Citi Prestige'").fetchone()
    assert row["eligibility"] == "new and existing Citi cardholders" and row["cardup"] == "not confirmed" and float(row["cents_per_mile"]) == 2.21


def test_two_copies_starting_at_once_take_turns(pg):
    """The start-up lock: while one process migrates or seeds, another waits for it."""
    import threading
    import time
    from cards import db, seed
    order = []

    def start(name, hold):
        with db.connect() as conn, db.startup_lock(conn):
            order.append(name + " in")
            time.sleep(hold)
            if name == "b":
                seed.seed(conn)
            order.append(name + " out")

    a = threading.Thread(target=start, args=("a", 0.5))
    a.start()
    time.sleep(0.1)
    b = threading.Thread(target=start, args=("b", 0))
    b.start()
    a.join()
    b.join()
    assert order == ["a in", "a out", "b in", "b out"]
    assert db.migrate() >= 1   # the lock was given back


def test_a_correction_mends_the_rule_for_past_purchases_too(pg):
    """A bucket marked "correction" is changed in place: no new version from today, so earlier purchases get it too.
    A change without it still starts a new version (a bank's real change)."""
    from psycopg.types.json import Jsonb
    conn, _ = pg
    q = "SELECT id, match, valid_from, valid_to FROM card_rules WHERE card_id = %s AND bucket = %s ORDER BY id"
    old = conn.execute(q, ("uob_krisflyer_cc", "sia")).fetchall()[-1]
    stale = dict(old["match"], merchant=["SINGAPORE\\s*AIR|\\bSIA\\b|SCOOT|KRISSHOP|KRIS\\+|KRISPLUS|PELAGO"])
    conn.execute("UPDATE card_rules SET match = %s, valid_from = '2026-01-01' WHERE id = %s", (Jsonb(stale), old["id"]))
    conn.commit()
    seed.seed(conn)
    rows = conn.execute(q, ("uob_krisflyer_cc", "sia")).fetchall()
    assert [r["id"] for r in rows] == [old["id"]] and rows[0]["valid_to"] is None
    assert "KRIS\\s*PAY" in rows[0]["match"]["merchant"][0] and str(rows[0]["valid_from"]) == "2026-01-01"
    # the same kind of change on a bucket without "correction": a new version from today
    old = conn.execute(q, ("uob_krisflyer_cc", "accelerated")).fetchall()[-1]
    conn.execute("UPDATE card_rules SET rate = 2.0 WHERE id = %s", (old["id"],))
    conn.commit()
    seed.seed(conn)
    rows = conn.execute(q, ("uob_krisflyer_cc", "accelerated")).fetchall()
    assert len(rows) == 2 and rows[0]["valid_to"] is not None and rows[1]["valid_to"] is None
