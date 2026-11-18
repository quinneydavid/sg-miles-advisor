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
